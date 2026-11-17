"""Cross-process image bridge for the USB smart dashboard.

The display can be driven by two different processes:

* the Hermes gateway (plugin render thread), or
* the standalone daemon (``standalone.py`` via ``usb-smart-dashboard.service``).

Only one of them owns the panel at a time (advisory flock). To let other
plugins — e.g. ``iwfcam4hermes`` — show an image regardless of which process
currently renders, they publish a JPEG into a small inbox directory; the
active renderer polls it and shows the image for a limited time.

Files (``<hermes_home>/usb-smart-dashboard/``):

* ``show-image.jpg`` — the image to display
* ``show-image.json`` — metadata: ``created`` (epoch), ``duration`` (s), ``source``
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

INBOX_IMAGE = "show-image.jpg"
INBOX_META = "show-image.json"
DEFAULT_DURATION = 8.0


def inbox_dir(hermes_home: str | Path | None = None) -> Path:
    """Inbox directory (created on demand by :func:`publish`)."""
    if hermes_home is None:
        hermes_home = Path.home() / ".hermes"
    return Path(hermes_home).expanduser() / "usb-smart-dashboard"


def publish(jpeg: bytes, duration: float = DEFAULT_DURATION, source: str = "",
            hermes_home: str | Path | None = None) -> Path:
    """Publish a JPEG for the display; returns the image path.

    Writes atomically (tmp + ``os.replace``) so readers never see a partial
    image, then updates the metadata last.
    """
    if not jpeg:
        raise ValueError("empty image")
    folder = inbox_dir(hermes_home)
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / INBOX_IMAGE
    meta_path = folder / INBOX_META
    tmp_image = folder / (INBOX_IMAGE + ".tmp")
    tmp_meta = folder / (INBOX_META + ".tmp")
    tmp_image.write_bytes(jpeg)
    os.replace(tmp_image, image_path)
    meta = {"created": time.time(), "duration": max(1.0, float(duration)),
            "source": str(source or "")}
    tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp_meta, meta_path)
    return image_path


def active(now: float | None = None, hermes_home: str | Path | None = None) -> dict[str, Any] | None:
    """Return the currently active image, or ``None``.

    Result: ``{"path", "key", "duration", "remaining", "source"}``.
    """
    folder = inbox_dir(hermes_home)
    image_path = folder / INBOX_IMAGE
    meta_path = folder / INBOX_META
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not image_path.is_file():
            return None
        created = float(meta.get("created") or 0)
        duration = max(1.0, float(meta.get("duration") or DEFAULT_DURATION))
        current = time.time() if now is None else float(now)
        if current >= created + duration:
            return None
        stat = image_path.stat()
        return {
            "path": image_path,
            "key": f"{stat.st_mtime_ns}:{stat.st_size}",
            "duration": duration,
            "remaining": round(created + duration - current, 1),
            "source": str(meta.get("source") or ""),
        }
    except (OSError, ValueError, TypeError):
        return None


def clear(hermes_home: str | Path | None = None) -> None:
    """Remove inbox files (best effort)."""
    folder = inbox_dir(hermes_home)
    for name in (INBOX_META, INBOX_IMAGE):
        try:
            (folder / name).unlink()
        except OSError:
            pass
