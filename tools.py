"""Agent-facing tool for the USB smart dashboard plugin.

The agent can push a one-shot status message onto the display:

* ``usb_display_message`` — show ``message`` for ``duration_seconds``,
  then return to the automatic system/agent status frame.

Tools only surface when the plugin's driver is actually available
(``check_fn``), so the model never sees a dead toolset on machines
without a display.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .state import state

USBDISPLAY_MESSAGE_SCHEMA: Dict[str, Any] = {
    "name": "usb_display_message",
    "description": (
        "Show a custom status message on the USB smart display dashboard. "
        "The message replaces the automatic system/agent status frame for a "
        "short time, then the dashboard returns to the live stats view."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Message text to display (keep it short — one line).",
            },
            "duration_seconds": {
                "type": "integer",
                "description": "How long to show the message. Default 10.",
                "minimum": 1,
                "maximum": 300,
            },
        },
        "required": ["message"],
    },
}


def _check_usb_display_available() -> bool:
    # The tool is registered with check_fn; the driver object is set once
    # by the plugin at register() time and replaced on fatal errors.
    return getattr(state, "driver_available", False)


def handle_usb_display_message(args: dict, **kwargs: Any) -> str:
    message = str(args.get("message", "")).strip()
    if not message:
        return json.dumps({"success": False, "reason": "empty message"})
    duration = int(args.get("duration_seconds", 10))
    state.show_message(message, float(duration))
    return json.dumps(
        {
            "success": True,
            "message": message,
            "duration_seconds": duration,
        }
    )