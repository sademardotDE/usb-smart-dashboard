#!/usr/bin/env python3
"""
Pre-install hook for USB Smart Dashboard plugin.

Runs before plugin installation. Checks for:
- Required system dependencies (pyserial, pillow)
- USB device availability
- Port permissions
"""

import subprocess
import sys
from pathlib import Path


def run(plugin_dir: str, plugin_name: str, config: dict) -> dict:
    errors = []
    warnings = []
    
    # Check Python dependencies
    try:
        import serial
    except ImportError:
        errors.append("pyserial not installed. Run: pip install pyserial")
    
    try:
        from PIL import Image, ImageFont, ImageDraw
    except ImportError:
        errors.append("Pillow not installed. Run: pip install pillow")
    
    # Check for numpy (optional but recommended for faster RGB565 conversion)
    try:
        import numpy
    except ImportError:
        warnings.append("numpy not installed. RGB565 conversion will use slower pure-Python fallback. Run: pip install numpy")
    
    # Check USB device
    device_path = None
    try:
        import serial.tools.list_ports as list_ports
        for port in list_ports.comports():
            if port.vid == 0x1a86 and port.pid == 0x5722:
                device_path = port.device
                break
    except Exception:
        pass
    
    if device_path:
        # Check if we have access to the device
        import os
        if not os.access(device_path, os.R_OK | os.W_OK):
            warnings.append(f"USB device {device_path} found but may need udev rule for write access")
    else:
        warnings.append("Turing Smart Screen (1a86:5722) not detected. Connect device before first use.")
    
    # Check for DejaVuSansMono font
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/local/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/home/fm/.local/share/fonts/DejaVuSansMono.ttf",
    ]
    font_found = any(Path(p).exists() for p in font_paths)
    if not font_found:
        warnings.append("DejaVuSansMono font not found. Text rendering may fail. Install: sudo apt install fonts-dejavu-core")
    
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    
    output = "Pre-install checks passed"
    if warnings:
        output += "\nWarnings:\n- " + "\n- ".join(warnings)
    
    return {"ok": True, "output": output}


if __name__ == "__main__":
    import json
    result = run("", "usb-smart-dashboard", {})
    print(json.dumps(result))