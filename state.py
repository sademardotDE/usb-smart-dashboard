"""Shared display state for the USB smart dashboard plugin.

One module-level :class:`DisplayState` instance is written by the plugin
hooks (agent lifecycle) and the ``usb_display_message`` tool, and read by
the background render thread. All mutations happen under ``_lock`` so the
render thread can snapshot without tearing.
"""

from __future__ import annotations

import getpass
import socket
import threading
import time
from typing import Optional

_lock = threading.Lock()

HOST_NAME = f"{getpass.getuser()}@{socket.gethostname()}"


class DisplayState:
    def __init__(self) -> None:
        # Driver status (set by the plugin at register time)
        self.driver_available: bool = False
        # Agent lifecycle
        self.session_active: bool = False
        self.agent_busy: bool = False  # stream / llm call in flight
        self.last_tool_name: Optional[str] = None
        self.last_activity: Optional[str] = None
        self.last_activity_ts: float = 0.0
        # One-shot operator/agent message with expiry
        self.message: Optional[str] = None
        self.message_until: float = 0.0

    def snapshot(self) -> "DisplayState":
        with _lock:
            copy = DisplayState()
            copy.__dict__.update(self.__dict__)
            return copy

    # -- writers ---------------------------------------------------------
    def set_activity(self, tool_name: Optional[str] = None, text: Optional[str] = None) -> None:
        with _lock:
            if tool_name is not None:
                self.last_tool_name = tool_name
            if text is not None:
                self.last_activity = text
                self.last_activity_ts = time.monotonic()

    def set_agent_busy(self, busy: bool, text: Optional[str] = None) -> None:
        with _lock:
            self.agent_busy = busy
            if text is not None:
                self.last_activity = text
                self.last_activity_ts = time.monotonic()

    def set_session(self, active: bool) -> None:
        with _lock:
            self.session_active = active
            if active:
                self.last_activity = "Session gestartet"
                self.last_activity_ts = time.monotonic()

    def show_message(self, text: str, duration_seconds: float) -> None:
        with _lock:
            self.message = text
            self.message_until = time.monotonic() + max(1.0, duration_seconds)

    # -- readers ---------------------------------------------------------
    def active_message(self) -> Optional[str]:
        with _lock:
            if self.message and time.monotonic() < self.message_until:
                return self.message
            self.message = None
            return None

    def status(self) -> str:
        """One human-readable line describing the current hermes state."""
        with _lock:
            msg = None
            if self.message and time.monotonic() < self.message_until:
                msg = self.message
        if msg:
            return msg
        if self.agent_busy:
            if self.last_tool_name:
                return f"hermes: {self.last_tool_name} ..."
            return "hermes: denkt ..."
        if self.session_active:
            return "hermes: bereit"
        return "hermes: idle"


state = DisplayState()