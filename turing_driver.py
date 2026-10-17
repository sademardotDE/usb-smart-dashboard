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
import re
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
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

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


def _hex_to_rgb(hex_str: str, fallback: tuple = (0, 0, 0)) -> tuple:
    """Parse a hex color string like '#4ade80' to (r, g, b). Returns fallback on error."""
    s = hex_str.strip().lstrip("#")
    if len(s) != 6:
        return fallback
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return fallback


# -- default colors -----------------------------------------------------

DEFAULT_BG = "#000000"
DEFAULT_TEXT = "#4ade80"

# -- font sizes ---------------------------------------------------------

FONT_SIZE_HEADER = 16
FONT_SIZE_BODY = 22

# -- regex patterns for line detection -----------------------------------

_RE_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}\s")
_RE_HOST = re.compile(r"\S+@\S+")
_RE_CPU = re.compile(r"CPU\s+(\d+)%")
_RE_MEM = re.compile(r"MEM\s+(\d+)M/(\d+)M")
_RE_LOAD = re.compile(r"LOAD\s+([\d.]+)")
_RE_TEMP = re.compile(r"(\d+)C")
_RE_STATUS = re.compile(r"hermes:\s*(.+)")


def _parse_cpu(text: str):
    m = _RE_CPU.search(text)
    return int(m.group(1)) if m else None


def _parse_mem(text: str):
    m = _RE_MEM.search(text)
    if m:
        used, total = int(m.group(1)), int(m.group(2))
        return used, total, (100.0 * used / total if total else None)
    return None, None, None


def _bar_color(pct: float) -> tuple:
    if pct < 60:
        return (74, 222, 128)   # green
    if pct < 85:
        return (250, 204, 21)   # yellow
    return (239, 68, 68)        # red


def _draw_bar(draw, x: int, y: int, bar_w: int, h: int, pct: float, text_rgb: tuple):
    filled = int(bar_w * min(pct, 100) / 100)
    # track
    draw.rounded_rectangle([x, y, x + bar_w, y + h], radius=3, fill=(30, 30, 40))
    # fill
    if filled > 0:
        col = _bar_color(pct)
        draw.rounded_rectangle([x, y, x + filled, y + h], radius=3, fill=col)
    # percentage text right of bar
    pct_txt = f"{pct:.0f}%"
    draw.text((x + bar_w + 6, y - 1), pct_txt, fill=text_rgb)


def _draw_sep(draw, y: int, w: int, color: tuple):
    draw.line([(8, y), (w - 8, y)], fill=color, width=1)


def _draw_border(draw, w: int, h: int, color: tuple):
    draw.rounded_rectangle([2, 2, w - 3, h - 3], radius=6, outline=color, width=1)


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
        bg_color: str = DEFAULT_BG,
        text_color: str = DEFAULT_TEXT,
    ) -> None:
        self._port = port or _autodetect_port() or ""
        self._baudrate = baudrate
        self._brightness = max(0, min(100, brightness))
        self._width = display_width
        self._height = display_height
        self._font_size = font_size
        self._bg = _hex_to_rgb(bg_color, (0, 0, 0))
        self._text = _hex_to_rgb(text_color, (74, 222, 128))
        self._ser = None
        self._renderer = False
        self._last_open = 0.0
        self._load_fonts()

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
            # Opening the CDC ACM port asserts DTR/RTS, which resets the
            # CH343 and with it the panel. Send HELLO too early and it is
            # swallowed by the rebooting firmware -> ack timeout -> wrong
            # (portrait) orientation and lost first frames. Wait for the
            # reboot, then retry the handshake once if the ack is missing.
            time.sleep(2.5)
            ser.reset_input_buffer()
            ser.write(_HELLO)
            resp = ser.read(6)
            if resp != _ACK_USBMONITOR_3_5:
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

    def _load_fonts(self) -> None:
        self._font_header = None
        self._font_body = None
        for cand in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        ):
            if os.path.exists(cand):
                try:
                    self._font_header = ImageFont.truetype(cand, FONT_SIZE_HEADER)
                    self._font_body = ImageFont.truetype(cand, FONT_SIZE_BODY)
                    return
                except Exception:
                    continue

    def write_frame(self, lines: List[str]) -> None:
        if self._ser is None or not self._ser.is_open:
            self._reconnect()
            if self._ser is None:
                return
        w, h = self._width, self._height
        fh = self._font_header
        fb = self._font_body
        tc = self._text
        dim = tuple(max(0, int(c * 0.55)) for c in tc)
        try:
            image = Image.new("RGB", (w, h), self._bg)
            draw = ImageDraw.Draw(image)

            # border
            _draw_border(draw, w, h, dim)

            # parse lines
            header_text = ""
            cpu_pct = None
            mem_used = mem_total = mem_pct = None
            load_val = None
            temp_val = None
            status_text = ""

            for ln in lines:
                s = str(ln)
                if _RE_TIME.search(s):
                    header_text = s
                elif _RE_STATUS.search(s):
                    status_text = s
                else:
                    cpu_pct = _parse_cpu(s)
                    mem_used, mem_total, mem_pct = _parse_mem(s)
                    m = _RE_LOAD.search(s)
                    if m:
                        load_val = m.group(1)
                    m = _RE_TEMP.search(s)
                    if m:
                        temp_val = m.group(1)

            y = 8

            # -- header line --
            if header_text and fh:
                draw.text((14, y), header_text, fill=dim, font=fh)
                y += FONT_SIZE_HEADER + 10
            elif header_text:
                draw.text((14, y), header_text, fill=dim)
                y += 22

            # separator
            _draw_sep(draw, y, w, dim)
            y += 10

            # -- metrics --
            bar_x = 14
            bar_w = w - 120

            if cpu_pct is not None and fb:
                draw.text((bar_x, y), "CPU", fill=tc, font=fb)
                _draw_bar(draw, bar_x + 80, y + 3, bar_w - 80, 16, cpu_pct, tc)
                y += FONT_SIZE_BODY + 8
            elif cpu_pct is not None:
                draw.text((bar_x, y), f"CPU  {cpu_pct}%", fill=tc)
                y += 28

            if mem_pct is not None and fb:
                draw.text((bar_x, y), "MEM", fill=tc, font=fb)
                _draw_bar(draw, bar_x + 80, y + 3, bar_w - 80, 16, mem_pct, tc)
                mem_txt = f"{mem_used}M/{mem_total}M"
                draw.text((bar_x + 80, y + 22), mem_txt, fill=dim, font=fh or fb)
                y += FONT_SIZE_BODY + 28
            elif mem_used is not None:
                draw.text((bar_x, y), f"MEM  {mem_used}M/{mem_total}M", fill=tc)
                y += 28

            # load + temp on one line
            extras = []
            if load_val:
                extras.append(f"LOAD {load_val}")
            if temp_val:
                extras.append(f"{temp_val}C")
            if extras:
                extra_txt = "   ".join(extras)
                if fb:
                    draw.text((bar_x, y), extra_txt, fill=tc, font=fb)
                    y += FONT_SIZE_BODY + 6
                else:
                    draw.text((bar_x, y), extra_txt, fill=tc)
                    y += 26

            # separator
            _draw_sep(draw, y, w, dim)
            y += 10

            # -- status line --
            if status_text:
                st = _RE_STATUS.search(status_text)
                st_body = st.group(1).strip() if st else status_text
                if "bereit" in st_body:
                    sc = (74, 222, 128)
                elif "idle" in st_body:
                    sc = dim
                elif "denkt" in st_body or "schreibt" in st_body:
                    sc = (250, 204, 21)
                else:
                    sc = (34, 211, 238)
                dot = "\u25cf "
                if fb:
                    draw.text((14, y), dot, fill=sc, font=fb)
                    draw.text((14 + int(fb.size * 0.7), y), st_body, fill=sc, font=fb)
                else:
                    draw.text((14, y), dot + st_body, fill=sc)

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
        bg_color=str(config_get("bg_color", DEFAULT_BG)),
        text_color=str(config_get("text_color", DEFAULT_TEXT)),
    )