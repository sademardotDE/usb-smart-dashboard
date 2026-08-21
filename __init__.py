"""USB smart dashboard plugin — live system + Hermes status on a smart display.

Wires three behaviours:

1. ``on_session_start`` / ``on_session_end`` — flips the display between
   "hermes bereit" and "hermes idle" as agent sessions open/close.

2. ``post_tool_call`` / ``on_stream_start`` / ``on_stream_end`` — mirrors
   what the agent is doing right now (tool names, activity) onto the
   display status line.

3. A background render thread (started in :func:`register`) ticks every
   :data:`TICK_SECONDS` and writes a frame — host time, CPU/mem/load,
   thermal, and the current Hermes status — through a pluggable
   :class:`~usb_smart_dashboard.display_driver.DisplayDriver`.

Plus one agent-facing tool, ``usb_display_message`` (see
:mod:`~usb_smart_dashboard.tools`), for pushing one-shot messages onto
the display from a conversation.

The plugin is fully self-contained: no core files are touched, drivers
degrade gracefully (null/file fallback), and a broken display can never
crash the agent.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from . import display_driver, state as state_mod
from .tools import (
    USBDISPLAY_MESSAGE_SCHEMA,
    _check_usb_display_available,
    handle_usb_display_message,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
RECONNECT_EVERY = 15  # ticks between re-open attempts for hotplug

# ---------------------------------------------------------------------------
# Render thread
# ---------------------------------------------------------------------------

_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _read_stats() -> dict:
    """Pure-stdlib system stats. Never raises — every read is defensive."""
    stats: dict = {"cpu": None, "mem": None, "load": None, "temp": None}
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            parts = fh.read().split()
            stats["load"] = parts[0] if parts else None
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = int(rest.split()[0]) // 1024
        if "MemTotal" in mem and "MemAvailable" in mem:
            used = mem["MemTotal"] - mem["MemAvailable"]
            stats["mem"] = f"{used}M/{mem['MemTotal']}M"
    except OSError:
        pass
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            fields = [int(v) for v in fh.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        if hasattr(_read_stats, "_prev"):
            prev_idle, prev_total = _read_stats._prev
            delta_total = total - prev_total
            if delta_total > 0:
                stats["cpu"] = f"{100.0 * (1 - (idle - prev_idle) / delta_total):.0f}%"
        _read_stats._prev = (idle, total)
    except (OSError, ValueError, IndexError):
        pass
    try:
        # First thermal zone is the common case on Linux.
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            raw = (zone / "temp").read_text(encoding="utf-8").strip()
            stats["temp"] = f"{int(raw) / 1000:.0f}C"
            break
    except (OSError, ValueError):
        pass
    return stats


def _build_frame(snap: "state_mod.DisplayState", stats: dict) -> list[str]:
    now = time.strftime("%H:%M:%S")
    lines = [f"{now}  {state_mod.HOST_NAME}"]
    parts = []
    if stats.get("cpu") is not None:
        parts.append(f"CPU {stats['cpu']}")
    if stats.get("mem") is not None:
        parts.append(f"MEM {stats['mem']}")
    if stats.get("load") is not None:
        parts.append(f"LOAD {stats['load']}")
    if stats.get("temp") is not None:
        parts.append(stats["temp"])
    if parts:
        lines.append("  ".join(parts))
    lines.append(snap.status())
    return lines


def _render_loop(state: "state_mod.DisplayState", driver: "display_driver.DisplayDriver") -> None:
    ticks = 0
    while not _STOP.wait(TICK_SECONDS):
        ticks += 1
        if ticks % RECONNECT_EVERY == 0 and not state.driver_available:
            state.driver_available = driver.open()
            if state.driver_available:
                logger.info("usb-display: (re)connected to driver %s", driver.name)
        if not state.driver_available:
            continue
        try:
            driver.write_frame(_build_frame(state.snapshot(), _read_stats()))
        except Exception as exc:  # never let rendering kill the process
            logger.warning("usb-display: render failed: %s", exc)


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def _on_session_start(**_kwargs) -> None:
    state_mod.state.set_session(True)


def _on_session_end(**_kwargs) -> None:
    state_mod.state.set_session(False)


def _on_post_tool_call(tool_name: str = None, **_kwargs) -> None:
    state_mod.state.set_activity(tool_name=tool_name or "tool")


def _on_stream_start(**_kwargs) -> None:
    state_mod.state.set_agent_busy(True, text="schreibt ...")


def _on_stream_end(**_kwargs) -> None:
    state_mod.state.set_agent_busy(False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register hooks + tool and start the render thread. Called once."""
    global _THREAD
    try:
        hermes_home = Path(__import__("hermes_constants", fromlist=["get_hermes_home"]).get_hermes_home())
    except Exception:  # pragma: no cover - import surface varies per runtime
        hermes_home = Path.home() / ".hermes"

    driver = display_driver.build_driver(ctx.get_config, hermes_home)
    state_mod.state.driver_available = driver.open()
    if state_mod.state.driver_available:
        logger.info(
            "usb-smart-dashboard: active driver=%s", driver.name
        )
    else:
        logger.info(
            "usb-smart-dashboard: no display reachable (driver=%s); "
            "renderer idle — tool stays hidden",
            driver.name,
        )

    for hook in ("on_session_start", "on_session_end"):
        ctx.register_hook(hook, globals()[f"_{hook}"])
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_stream_start", _on_stream_start)
    ctx.register_hook("on_stream_end", _on_stream_end)

    ctx.register_tool(
        name="usb_display_message",
        toolset="usb_display",
        schema=USBDISPLAY_MESSAGE_SCHEMA,
        handler=handle_usb_display_message,
        check_fn=_check_usb_display_available,
        description="Push a one-shot message onto the USB smart display.",
        emoji="🖥️",
    )

    if _THREAD is None or not _THREAD.is_alive():
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_render_loop,
            args=(state_mod.state, driver),
            name="usb-smart-dashboard-render",
            daemon=True,
        )
        _THREAD.start()
        logger.info("usb-smart-dashboard: render thread started")