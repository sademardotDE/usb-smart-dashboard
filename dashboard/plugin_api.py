"""USB Smart Dashboard – Plugin Config API.

Mounted at /api/plugins/usb-smart-dashboard/ by the dashboard plugin system.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
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