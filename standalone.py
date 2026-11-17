#!/usr/bin/env python3
"""Standalone display daemon for the usb-smart-dashboard plugin.

Keeps the Turing Smart Screen alive outside a Hermes session: reads the
plugin settings from the profile config.yaml, then renders system stats
(CPU, memory, load, temperature) through the same TuringDriver the
plugin uses.

Only one renderer may drive the panel at a time; the driver's advisory
flock on the device fd arbitrates between this daemon and any running
Hermes gateway, so starting this service next to a live session is safe.

Run via the systemd user service usb-smart-dashboard.service.
"""

from __future__ import annotations

import glob
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turing_driver import TuringDriver  # noqa: E402
import imagebridge  # noqa: E402

try:
    from PIL import Image  # noqa: E402
except ImportError:  # pragma: no cover
    Image = None

logger = logging.getLogger("usb-smart-dashboard.standalone")

CONFIG_PATH = Path(
    os.environ.get(
        "USB_DASH_CONFIG",
        Path.home() / ".hermes/profiles/scout/config.yaml",
    )
)
REFRESH_SECONDS = 2.0

_running = True


def _handle_signal(signum, _frame):
    global _running
    _running = False


def load_settings() -> dict:
    """Read plugins.entries.usb-smart-dashboard.settings from config.yaml."""
    defaults = {
        "driver": "turing",
        "port": "",
        "baudrate": 115200,
        "brightness": 25,
        "display_width": 480,
        "display_height": 320,
        "font_size": 28,
        "bg_color": "#000000",
        "text_color": "#4ade80",
    }
    try:
        import yaml

        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        entry = (
            cfg.get("plugins", {})
            .get("entries", {})
            .get("usb-smart-dashboard", {})
        )
        defaults.update({k: v for k, v in entry.get("settings", {}).items() if v != ""})
    except FileNotFoundError:
        logger.info("config %s not found - using defaults", CONFIG_PATH)
    except Exception as exc:  # pragma: no cover - config is optional
        logger.warning("config read failed (%s) - using defaults", exc)
    return defaults


# -- system metrics (stdlib only) ---------------------------------------

_prev_cpu: tuple[int, int] | None = None


def read_cpu_pct() -> int | None:
    """Busy percentage from /proc/stat deltas (needs two samples)."""
    global _prev_cpu
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()[1:]
        vals = [int(p) for p in parts[:8]]
        idle = vals[3] + vals[4]
        total = sum(vals)
    except (OSError, IndexError, ValueError):
        return None
    cur = (idle, total)
    prev, _prev_cpu = _prev_cpu, cur
    if prev is None:
        return None
    d_idle = cur[0] - prev[0]
    d_total = cur[1] - prev[1]
    if d_total <= 0:
        return None
    return max(0, min(100, round(100 * (1 - d_idle / d_total))))


def read_mem() -> tuple[int, int] | None:
    """(used, total) in MiB from /proc/meminfo (MemTotal - MemAvailable)."""
    try:
        total = avail = None
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) // 1024
        if total and avail is not None:
            return total - avail, total
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_temp() -> int | None:
    """First thermal zone that looks like a CPU package, else zone 0."""
    zones = []
    for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            mtype = (Path(path).parent / "type").read_text().strip()
            millideg = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
        zones.append((mtype, millideg // 1000))
    if not zones:
        return None
    for mtype, deg in zones:
        if "x86" in mtype or "cpu" in mtype or "pkg" in mtype:
            return deg
    return zones[0][1]


def build_lines() -> list[str]:
    now = time.strftime("%H:%M:%S")
    host = socket.gethostname()
    lines = [f"{now} {host}@standalone"]

    cpu = read_cpu_pct()
    if cpu is not None:
        lines.append(f"CPU {cpu}%")
    mem = read_mem()
    if mem:
        lines.append(f"MEM {mem[0]}M/{mem[1]}M")
    try:
        lines.append(f"LOAD {os.getloadavg()[0]:.2f}")
    except OSError:
        pass
    temp = read_temp()
    if temp is not None:
        lines.append(f"TEMP {temp}C")
    lines.append("hermes: standalone-daemon")
    return lines


# -- main loop ----------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    settings = load_settings()
    driver = TuringDriver(
        port=settings.get("port", ""),
        baudrate=int(settings.get("baudrate", 115200)),
        brightness=int(settings.get("brightness", 25)),
        display_width=int(settings.get("display_width", 480)),
        display_height=int(settings.get("display_height", 320)),
        font_size=int(settings.get("font_size", 28)),
        bg_color=str(settings.get("bg_color", "#000000")),
        text_color=str(settings.get("text_color", "#4ade80")),
    )

    # Warm-up sample so the first CPU percentage is real, not a guess.
    read_cpu_pct()

    opened = False
    last_image_key = None
    logger.info("daemon starting (refresh every %.1fs)", REFRESH_SECONDS)
    while _running:
        if not opened:
            opened = driver.open()
            if not opened:
                logger.warning("display not available - retrying in 5s")
                time.sleep(5)
                continue
            logger.info("display open")
        active_image = imagebridge.active()
        if active_image:
            if Image is not None and active_image["key"] != last_image_key:
                try:
                    driver.write_image(Image.open(active_image["path"]))
                    logger.info("showing image from %s (%.0fs)",
                                active_image.get("source") or "?", active_image.get("remaining") or 0)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("image render failed: %s", exc)
                last_image_key = active_image["key"]
            if active_image:
                end = time.monotonic() + REFRESH_SECONDS
                while _running and time.monotonic() < end:
                    time.sleep(0.1)
                continue
        last_image_key = None
        driver.write_frame(build_lines())
        # write_frame self-heals transient serial errors; if it closed the
        # port for good, reopen on the next iteration.
        opened = driver._ser is not None and driver._ser.is_open
        # Sleep in small steps so SIGTERM is honoured promptly.
        end = time.monotonic() + REFRESH_SECONDS
        while _running and time.monotonic() < end:
            time.sleep(0.1)

    driver.close()
    logger.info("daemon stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
