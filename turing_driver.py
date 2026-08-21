"""Turing Smart Screen driver for the USB smart dashboard plugin.

Implements the wire protocol for Turing/UsbMonitor "Revision A" panels
(3.5" IPS USB-C, ``1a86:5722``, serial ``USB35INCHIPSV2``) based on the
community-documented protocol (mathoudebine/turing-smart-screen-python,
GPL-3.0 — surface-reimplemented here, not copied):

* USB CDC ACM serial, **115200 baud, RTS/CTS hardware flow control**
  (the device resets/garbles without RTS/CTS — the flow-control bit is
  load-bearing, this is what breaks naive serial writers).
* 6-byte command frames: 4 packed coordinate bytes + command byte.
* HELLO handshake (6x 0x45): the panel answers `01 01 01 01 01 01` for
  the 3.5" UsbMonitor, then expects portrait 320x480 unless told
  otherwise; we switch to landscape 480x320.
* Framebuffer: RGB565 little-endian row data, sent raw after the
  DISPLAY_BITMAP (0xC5) command, chunked by ``display_width * 8`` bytes.

Rendering is bitmap-based (no text protocol): each frame is drawn into a
PIL image (default bitmap font, background black) and pushed as
RGB565. PIL and pyserial are required; numpy is used when present for
the pixel conversion (pure-Python fallback otherwise).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import List, Optional

try:  # exclusive device lock — only one Hermes process renders at a time
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
    from serial import SerialException  # type: ignore

    _HAS_SERIAL = True
except ImportError:  # pragma: no cover
    _HAS_SERIAL = False
    SerialException = OSError  # type: ignore

try:
    from PIL import Image, ImageDraw  # type: ignore

    _HAS_PIL = True
except ImportError:  # pragma: no cover
    _HAS_PIL = False

try:
    import numpy as np  # type: ignore

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    _HAS_NUMPY = False

# -- protocol constants (Rev A) -----------------------------------------

_HELLO = bytes([69] * 6)  # Command.HELLO x6
_ACK_USBMONITOR_3_5 = bytes([0x01] * 6)

_CLEAR = 102
_SET_BRIGHTNESS = 110
_SET_ORIENTATION = 121
_DISPLAY_BITMAP = 197
_ORIENTATION_LANDSCAPE = 2

AUTODETECT_SERIAL = "USB35INCHIPSV2"
AUTODETECT_VID = 0x1A86
AUTODETECT_PID = 0x5722


def _autodetect_port() -> Optional[str]:
    if _HAS_SERIAL:
        try:
            for port in list_ports.comports():
                if port.serial_number == AUTODETECT_SERIAL:
                    return port.device
            for port in list_ports.comports():
                if port.vid == AUTODETECT_VID and port.pid == AUTODETECT_PID:
                    return port.device
        except Exception:
            pass
    for cand in ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"):
        if os.path.exists(cand):
            return cand
    return None


def _image_to_rgb565_le(image) -> bytes:
    """RGB(A) PIL image -> little-endian RGB565 byte stream."""
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    w, h = image.size
    px = image.load()
    n = w * h
    if _HAS_NUMPY:
        arr = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape((n, -1))
        r = arr[:, 0].astype(np.uint16) >> 3
        g = arr[:, 1].astype(np.uint16) >> 2
        b = arr[:, 2].astype(np.uint16) >> 3
        rgb565 = ((r << 11) | (g << 5) | b).astype("<u2")
        return rgb565.tobytes()
    out = bytearray(n * 2)
    for i in range(n):
        r, g, b = px[i % w, i // w][:3]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out[2 * i] = v & 0xFF
        out[2 * i + 1] = v >> 8
    return bytes(out)


def _send_command(ser, cmd: int, x: int, y: int, ex: int, ey: int) -> None:
    buf = bytearray(6)
    buf[0] = x >> 2
    buf[1] = ((x & 3) << 6) + (y >> 4)
    buf[2] = ((y & 15) << 4) + (ex >> 6)
    buf[3] = ((ex & 63) << 2) + (ey >> 8)
    buf[4] = ey & 255
    buf[5] = cmd
    ser.write(bytes(buf))


class TuringDriver:
    """Renders text frame lines onto a Turing Smart Screen 3.5" panel."""

    name = "turing"

    def __init__(
        self,
        port: str = "",
        baudrate: int = 115200,
        brightness: int = 25,
        display_width: int = 480,
        display_height: int = 320,
        font_size: int = 28,
    ) -> None:
        self._port = port or _autodetect_port() or ""
        self._baudrate = baudrate
        self._brightness = max(0, min(100, brightness))
        self._width = display_width
        self._height = display_height
        self._font_size = font_size
        self._ser = None
        self._renderer = False
        self._last_open = 0.0

    # -- lifecycle ------------------------------------------------------

    def open(self) -> bool:
        if not (_HAS_SERIAL and _HAS_PIL):
            logger.warning("usb-turing: pyserial and/or PIL not available")
            return False
        if not self._port:
            self._port = _autodetect_port() or ""
        if not self._port:
            logger.warning("usb-turing: no Turing Smart Screen found")
            return False
        try:
            ser = serial.Serial(
                self._port,
                baudrate=self._baudrate,
                timeout=1.0,
                write_timeout=2.0,
                rtscts=True,
            )
        except SerialException as exc:
            logger.warning("usb-turing: cannot open %s: %s", self._port, exc)
            return False

        # Cross-process exclusivity: several Hermes processes (gateway,
        # dashboard, serve) all load this plugin and would otherwise write
        # the same panel in parallel, tearing the framebuffer into stripes
        # and fragments. Take an advisory flock on the device fd — only the
        # process that wins the lock opens/renders; the others back off and
        # retry later. When the renderer exits its fd closes and the lock
        # is released, so the next contender takes over automatically.
        if _HAS_FCNTL:
            try:
                fcntl.flock(ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._renderer = True
            except OSError:
                logger.info(
                    "usb-turing: %s already locked by another Hermes process — "
                    "not rendering",
                    self._port,
                )
                try:
                    ser.close()
                except Exception:
                    pass
                return False
        else:  # pragma: no cover - non-POSIX fallback
            self._renderer = True

        self._ser = ser  # helpers below write through self._ser

        # HELLO handshake -> detect sub-revision (3.5" UsbMonitor)
        try:
            ser.reset_input_buffer()
            ser.write(_HELLO)
            resp = ser.read(6)
            ser.reset_input_buffer()
            width, height = self._width, self._height
            if resp == _ACK_USBMONITOR_3_5:
                width, height = 320, 480  # handed to us in portrait
            logger.info(
                "usb-turing: %s handshake ack=%s -> %dx%d",
                self._port, resp.hex() or "(none)", width, height,
            )
            self._width, self._height = width, height
            self._send_orientation(width, height)
            self._send_brightness(self._brightness)
        except (SerialException, OSError) as exc:
            logger.warning("usb-turing: handshake failed: %s", exc)
            self._ser = None
            try:
                ser.close()
            except Exception:
                pass
            return False

        return True

    def _send_orientation(self, width: int, height: int) -> None:
        buf = bytearray(11)
        buf[5] = _SET_ORIENTATION
        buf[6] = 100 + _ORIENTATION_LANDSCAPE
        buf[7] = width >> 8
        buf[8] = width & 255
        buf[9] = height >> 8
        buf[10] = height & 255
        self._ser.write(bytes(buf))

    def _send_brightness(self, level: int) -> None:
        absolute = int(255 - (min(100, max(0, level)) / 100) * 255)
        _send_command(self._ser, _SET_BRIGHTNESS, absolute, 0, 0, 0)

    # -- rendering ------------------------------------------------------

    def _font(self):
        if getattr(self, "_cached_font", None) is not None:
            return self._cached_font
        for cand in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        ):
            if os.path.exists(cand):
                try:
                    from PIL import ImageFont

                    font = ImageFont.truetype(cand, self._font_size)
                    self._cached_font = font
                    return font
                except Exception:
                    continue
        self._cached_font = None
        return None

    def write_frame(self, lines: List[str]) -> None:
        if self._ser is None or not self._ser.is_open:
            self._reconnect()
            if self._ser is None:
                return
        w, h = self._width, self._height
        try:
            image = Image.new("RGB", (w, h), (5, 5, 10))
            draw = ImageDraw.Draw(image)
            font = self._font()
            step = self._font_size + 8
            y = 4
            if font is None:
                step = 14  # tiny bitmap font
            for line in lines[: max(1, h // step)]:
                text = str(line)
                if font is not None and draw.textlength(text, font=font) > w - 16:
                    while text and draw.textlength(text + "…", font=font) > w - 16:
                        text = text[:-1]
                    text += "…"
                draw.text((8, y), text, fill=(225, 225, 225), font=font)
                y += step

            rgb565 = _image_to_rgb565_le(image)
            x1, y1 = w - 1, h - 1
            _send_command(self._ser, _DISPLAY_BITMAP, 0, 0, x1, y1)
            for off in range(0, len(rgb565), w * 8):
                self._ser.write(rgb565[off: off + w * 8])
            self._ser.flush()
        except (SerialException, OSError) as exc:
            logger.warning("usb-turing: frame write failed (%s); reconnecting", exc)
            self.close()
            self._reconnect()

    # -- maintenance ----------------------------------------------------

    def _reconnect(self) -> None:
        now = time.monotonic()
        if now - self._last_open < 10.0:
            return
        self._last_open = now
        # Port may have changed after a device reset — re-autodetect.
        self._port = _autodetect_port() or self._port
        if self.open():
            logger.info("usb-turing: reconnected on %s", self._port)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


def build_turing_driver(config_get) -> TuringDriver:
    return TuringDriver(
        port=str(config_get("port", "")),
        baudrate=int(config_get("baudrate", 115200)),
        brightness=int(config_get("brightness", 25)),
        display_width=int(config_get("display_width", 480)),
        display_height=int(config_get("display_height", 320)),
        font_size=int(config_get("font_size", 28)),
    )