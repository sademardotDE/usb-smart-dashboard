# USB Smart Dashboard (Hermes Plugin)

Renders live system stats and the current Hermes agent status on a
Turing Smart Screen 3.5" (USB smart display). Pure stdlib + Pillow +
pyserial, pluggable driver layer, zero core changes.

## What it does

- **Background render thread** (started in `register()`) writes a frame
  every 2 s: host time, CPU/MEM/LOAD/thermal, and the Hermes status line.
- **Hooks** mirror agent state onto the display:
  - `on_session_start` / `on_session_end` → "hermes: bereit" / "hermes: idle"
  - `post_tool_call` → shows the tool currently running
  - `on_stream_start` / `on_stream_end` → "hermes: schreibt …" while streaming
- **Tool** `usb_display_message` lets the agent push a one-shot message
  onto the display (for N seconds), then it returns to the live frame.
- **Image bridge (v0.5.0):** other plugins can publish a JPEG to
  `~/.hermes/usb-smart-dashboard/show-image.jpg` (metadata
  `show-image.json` with `created`/`duration`/`source`); the active renderer
  (gateway thread **or** standalone daemon) shows it scaled to fit for the
  requested time, then returns to the live frame. Used by `iwfcam4hermes` to
  show the motion snapshot. Contract/examples/troubleshooting:
  [`docs/IMAGE_BRIDGE.md`](docs/IMAGE_BRIDGE.md).

## Install / layout

```text
~/.hermes/plugins/usb-smart-dashboard/
├── plugin.yaml          # manifest (name, hooks)
├── __init__.py          # register(ctx): hooks + tool + render thread
├── display_driver.py    # DisplayDriver ABC + turing/file/null drivers
├── turing_driver.py     # Turing Smart Screen 3.5" driver (Rev A protocol)
├── state.py             # shared display state (hooks write, thread reads)
└── tools.py             # tool schema + handler
```

Enable/disable like any plugin: `hermes plugins list` / config
`plugins.entries.usb-smart-dashboard`.

## Configuration

All keys live in the plugin's own settings subtree in
`~/.hermes/config.yaml` (`plugins.entries.usb-smart-dashboard.settings.*`
— plugin-relative keys, read via `ctx.get_config`):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Master switch for the driver |
| `driver` | `turing` | `turing`, `serial`, `file`, `auto` |
| `port` | autodetect | e.g. `/dev/ttyACM0` (by serial `USB35INCHIPSV2`/VID:PID `1a86:5722`) |
| `baudrate` | `115200` | Serial baud rate (hardware RTS/CTS flow control, always on) |
| `brightness` | `25` | Backlight 0–100 |
| `display_width` | `480` | Panel width in px (landscape) |
| `display_height` | `320` | Panel height in px (landscape) |
| `font_size` | `28` | DejaVuSansMono px height for text rows |

```yaml
plugins:
  enabled:
    - usb-smart-dashboard
  entries:
    usb-smart-dashboard:
      settings:
        enabled: true
        driver: turing
        brightness: 25
```

Without hardware (or with `driver: file`) frames land in
`~/.hermes/usb-smart-dashboard/display.log` and the agent tool stays
hidden (`check_fn` gates it on a live driver).

## Display protocol

The Turing 3.5" IPS (Revision A hardware, model string
`USB35INCHIPSV2`) speaks a **bitmap protocol** — no text commands.

- **Transport:** CDC-ACM serial at 115200 baud with **RTS/CTS hardware
  flow control**. Without flow control the panel resets/EIOs — the
  device re-enumerates and the port can move (`/dev/ttyACM1` → `ttyACM0`).
- **Framing:** every packet is a 6-byte big-endian coordinator header
  `0xAB | len_hi | len_lo | cmd | 0x00 | chk` (chk = XOR of bytes 0–4),
  followed by exactly `len` payload bytes.
- **Handshake:** `HELLO` (`45` = 6×`0x45`) → panel ACKs with 6×`0x01`
  (3.5" UsbMonitor) or 6×`0x02` (3.5" IPC).
- **Orientation:** `92` with argument `2` = landscape 480×320 (the
  panel's native portrait is 320×480; setting portrait dims explicitly
  keeps the render buffer consistent).
- **Brightness:** `74` with 0–100.
- **Frames:** `98` — RGB565 little-endian pixel data, sent in chunks of
  `width * 8` bytes (480×320 → 1280-byte chunks), small sleeps between
  chunks; the final empty payload packet flushes the SPI transfer.
- Colors are 5:6:5; `_image_to_rgb565_le` converts a PIL frame row-wise
  (numpy fast path, pure-Python fallback).

Protocol reference: [mathoudebine/turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)
(`lcd_comm_rev_a.py`, GPL-3.0) — Linux renderer cross-check:
[danieltone/turzx-linux-usbmonitor](https://github.com/danieltone/turzx-linux-usbmonitor).

Wild guess display text drivers (`serial`+, `clear_sequence`) are **not**
supported by this panel; use `turing` or `file`.

## Integrationen

- **iwfcam4hermes** (≥ 0.18.0): zeigt Bewegungs-Snapshots auf dem Display
  (`relay_motion_display=true`, `relay_motion_display_duration`), Status/Test
  über `camera_display` bzw. `GET/POST /display`. Erkennt dieses Plugin über
  Manifest + `plugins.enabled` und `/dev/ttyACM*`.
- Eigene Plugins: JPEG + Meta in die Inbox schreiben, siehe
  [`docs/IMAGE_BRIDGE.md`](docs/IMAGE_BRIDGE.md).

## Notes

- Plugin runs in the Hermes process; the render thread is a daemon thread
  and dies with the process. A broken display degrades to log-only and
  never raises into the agent.
- The Turing handshake/port must survive panel resets: the driver
  re-enumerates serial ports on `open()` and recovers from `SerialException`
  (`EIO`) marking the driver unavailable until the next tick.
- System stats come from `/proc` (Linux). On other platforms the stats
  rows are simply omitted; the status line still renders.