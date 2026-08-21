"""USB Smart Dashboard – Plugin Config + Status API.

Mounted at /api/plugins/usb-smart-dashboard/ by the dashboard plugin system.
"""
from __future__ import annotations

import getpass
import socket
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from hermes_cli.config import load_config, save_config

router = APIRouter()

PLUGIN_KEY = "usb-smart-dashboard"
SETTINGS_PATH = ["plugins", "entries", PLUGIN_KEY, "settings"]


class ConfigResponse(BaseModel):
    enabled: bool = True
    driver: str = "turing"
    port: str = ""
    baudrate: int = 115200
    brightness: int = 25
    display_width: int = 480
    display_height: int = 320
    font_size: int = 28
    bg_color: str = "#000000"
    text_color: str = "#4ade80"


class StatusResponse(BaseModel):
    host: str = ""
    time: str = ""
    cpu: str | None = None
    mem: str | None = None
    mem_pct: float | None = None
    load: str | None = None
    temp: str | None = None
    uptime: str | None = None


def _get_settings() -> dict:
    cfg = load_config() or {}
    for key in SETTINGS_PATH:
        cfg = cfg.get(key, {})
    return cfg if isinstance(cfg, dict) else {}


def _set_settings(new_settings: dict) -> None:
    cfg = load_config() or {}
    current = cfg
    for key in SETTINGS_PATH[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[SETTINGS_PATH[-1]] = new_settings
    save_config(cfg)


def _read_stats() -> dict:
    """Pure-stdlib system stats. Never raises — every read is defensive."""
    stats: dict = {"cpu": None, "mem": None, "mem_pct": None, "load": None, "temp": None}
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
            if mem["MemTotal"]:
                stats["mem_pct"] = round(100.0 * used / mem["MemTotal"], 1)
    except (OSError, ValueError):
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
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            raw = (zone / "temp").read_text(encoding="utf-8").strip()
            stats["temp"] = f"{int(raw) / 1000:.0f}C"
            break
    except (OSError, ValueError):
        pass
    # Uptime
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            secs = int(float(fh.read().split()[0]))
        h, rem = divmod(secs, 3600)
        m = rem // 60
        stats["uptime"] = f"{h}h {m}m" if h else f"{m}m"
    except (OSError, ValueError, IndexError):
        pass
    return stats


@router.get("/status")
def get_status() -> StatusResponse:
    """Return live system stats for the dashboard panel."""
    stats = _read_stats()
    return StatusResponse(
        host=f"{getpass.getuser()}@{socket.gethostname()}",
        time=time.strftime("%H:%M:%S"),
        cpu=stats.get("cpu"),
        mem=stats.get("mem"),
        mem_pct=stats.get("mem_pct"),
        load=stats.get("load"),
        temp=stats.get("temp"),
        uptime=stats.get("uptime"),
    )


@router.get("/config")
def get_config() -> ConfigResponse:
    """Return current plugin settings from config.yaml."""
    settings = _get_settings()
    return ConfigResponse(**settings)


@router.put("/config")
def put_config(payload: ConfigResponse) -> ConfigResponse:
    """Persist plugin settings to config.yaml."""
    _set_settings(payload.model_dump())
    return payload
