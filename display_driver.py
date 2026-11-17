"""Display driver layer for the USB smart dashboard plugin.

The plugin talks to the smart display through a :class:`DisplayDriver`.
Two concrete drivers ship with the skeleton:

* :class:`SerialDriver` — the primary path. Writes text frames to a USB
  serial device (``/dev/ttyUSB0``, ``/dev/ttyACM0``, ...) using pyserial
  when available. Firmware-agnostic: frames are plain UTF-8 text lines,
  one line per display row, optionally preceded by a configurable clear
  sequence.
* :class:`FileDriver` — fallback that appends frames to a log file. Keeps
  the plugin fully functional (and testable) with no hardware attached.

The active driver is chosen in :func:`build_driver` from the plugin's
own settings keys (``plugins.entries.usb-smart-dashboard.settings.*``,
read via ``ctx.get_config`` with plugin-relative keys); unknown/missing
config degrades to :class:`NullDriver` (log-only) so a broken setup can
never crash the agent process.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

try:  # pyserial is optional — degrade to FileDriver/NullDriver without it
    import serial  # type: ignore
    from serial import SerialException  # type: ignore

    _HAS_SERIAL = True
except ImportError:  # pragma: no cover - depends on install
    _HAS_SERIAL = False
    SerialException = OSError  # type: ignore


def _default_port() -> Optional[str]:
    """Best-effort autodetect of the first USB serial device."""
    for cand in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyAMA0"):
        if os.path.exists(cand):
            return cand
    return None


class DisplayDriver:
    """Minimal contract every driver implements."""

    name = "base"

    def open(self) -> bool:
        """Connect to the display. Returns True when ready."""
        return True

    def write_frame(self, lines: List[str]) -> None:
        """Render one frame as a list of display rows."""
        raise NotImplementedError

    def write_image(self, image) -> None:
        """Show an image (PIL image or JPEG/PNG bytes). Default: log only."""
        logger.info("usb-display: image frame requested (driver %s has no image support)", self.name)

    def close(self) -> None:
        pass


class NullDriver(DisplayDriver):
    """Log-only driver used when no hardware/config is available."""

    name = "null"

    def __init__(self) -> None:
        self._last_logged: Optional[str] = None
        self._last_time = 0.0

    def open(self) -> bool:
        return False

    def write_frame(self, lines: List[str]) -> None:
        # Rate-limit log spam; the render loop ticks multiple times a second.
        now = time.monotonic()
        if now - self._last_time < 5.0:
            return
        self._last_time = now
        logger.info("usb-display frame (null driver): %s", " | ".join(lines))


class FileDriver(DisplayDriver):
    """Append frames to ``<hermes_home>/usb-smart-dashboard/display.log``."""

    name = "file"

    def __init__(self, log_dir: Path, max_lines: int = 2000) -> None:
        self._log_dir = log_dir
        self._max_lines = max_lines
        self._path = log_dir / "display.log"

    def open(self) -> bool:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return True

    def write_frame(self, lines: List[str]) -> None:
        frame = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] " + " | ".join(lines)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(frame + "\n")
            self._trim()
        except OSError as exc:
            logger.warning("usb-display file driver write failed: %s", exc)

    def write_image(self, image) -> None:
        """Persist the image for debugging (driver=file)."""
        import time as _time

        folder = self._log_dir / "images"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".jpg"
        data = image
        if not isinstance(image, (bytes, bytearray)):
            suffix = ".png"
            try:
                import io

                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                data = buffer.getvalue()
            except Exception:
                return
        (folder / f"frame-{_time.strftime('%Y%m%d-%H%M%S')}{suffix}").write_bytes(bytes(data))

    def _trim(self) -> None:
        try:
            stat = self._path.stat()
            if stat.st_size < self._max_lines * 80:
                return
            lines = self._path.read_text(encoding="utf-8").splitlines()
            tail = lines[-self._max_lines:]
            self._path.write_text("\n".join(tail) + "\n", encoding="utf-8")
        except OSError:
            pass


class SerialDriver(DisplayDriver):
    """Primary driver: writes text frames to a USB serial device."""

    name = "serial"

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        clear_sequence: str = "\x1b[2J\x1b[H",
        line_sep: str = "\r\n",
        timeout: float = 0.5,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._clear_sequence = clear_sequence
        self._line_sep = line_sep
        self._timeout = timeout
        self._ser = None
        self._last_reconnect = 0.0

    def open(self) -> bool:
        if not _HAS_SERIAL:
            logger.warning(
                "usb-display: pyserial not installed; serial driver unavailable"
            )
            return False
        try:
            self._ser = serial.Serial(
                self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
                write_timeout=1.0,
            )
            logger.info("usb-display: connected to %s @ %d", self._port, self._baudrate)
            return True
        except SerialException as exc:
            logger.warning("usb-display: cannot open %s: %s", self._port, exc)
            return False

    def write_frame(self, lines: List[str]) -> None:
        if self._ser is None or not self._ser.is_open:
            self._reconnect()
            if self._ser is None:
                return
        payload = self._clear_sequence + self._line_sep.join(lines) + self._line_sep
        try:
            self._ser.write(payload.encode("utf-8", errors="replace"))
            self._ser.flush()
        except SerialException as exc:
            logger.warning("usb-display: write failed (%s); reconnecting", exc)
            self.close()
            self._reconnect()

    def _reconnect(self) -> None:
        if self._ser is not None and self._ser.is_open:
            return
        now = time.monotonic()
        if now - self._last_reconnect < 10.0:
            return
        self._last_reconnect = now
        self.open()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


def build_driver(config_get, hermes_home: Path) -> DisplayDriver:
    """Build the configured driver.

    ``config_get`` mirrors ``PluginContext.get_config`` with plugin-relative
    keys (resolved under ``plugins.entries.usb-smart-dashboard.settings``).
    Keys: ``enabled``, ``port``, ``baudrate``, ``driver`` (``"serial"`` |
    ``"file"`` | ``"auto"`` | ``"turing"``).
    """
    enabled = bool(config_get("enabled", True))
    if not enabled:
        return NullDriver()

    driver_name = str(config_get("driver", "auto")).lower()
    log_dir = hermes_home / "usb-smart-dashboard"

    if driver_name in ("file", "log"):
        return FileDriver(log_dir)

    if driver_name == "turing":
        # Relative import: the loader registers this package under an
        # alias (hermes_plugins.usb_smart_dashboard), so the absolute
        # name "usb_smart_dashboard" does not exist at runtime.
        from .turing_driver import build_turing_driver

        return build_turing_driver(config_get)

    port = str(config_get("port", "") or _default_port() or "")

    if driver_name == "serial" and not port:
        return NullDriver()

    if driver_name in ("serial", "auto"):
        return SerialDriver(port, int(config_get("baudrate", 115200)))

    logger.warning("Unknown display driver %r — falling back to NullDriver", driver_name)
    return NullDriver()