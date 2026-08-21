#!/usr/bin/env python3
"""
Post-install hook for USB Smart Dashboard plugin.

Runs after plugin installation. Performs:
- Udev rule installation for Turing device
- Systemd user service setup
- Config.yaml default settings
"""

import subprocess
import os
import sys
from pathlib import Path


def run(plugin_dir: str, plugin_name: str, config: dict) -> dict:
    plugin_path = Path(plugin_dir)
    output_lines = []
    
    # 1. Install udev rule
    udev_rule = '''# Turing Smart Screen 3.5" (1a86:5722)
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="5722", MODE="0666", GROUP="dialout", SYMLINK+="usb-smart-dashboard"
'''
    udev_path = Path("/etc/udev/rules.d/99-usb-smart-dashboard.rules")
    
    try:
        # Check if rule already exists
        if udev_path.exists():
            existing = udev_path.read_text()
            if "1a86" in existing and "5722" in existing:
                output_lines.append("Udev rule already exists")
            else:
                # Backup and write
                subprocess.run(["sudo", "cp", str(udev_path), f"{udev_path}.bak"], check=False)
                subprocess.run(["sudo", "tee", str(udev_path)], input=udev_rule.encode(), check=True)
                output_lines.append("Udev rule installed")
        else:
            subprocess.run(["sudo", "tee", str(udev_path)], input=udev_rule.encode(), check=True)
            output_lines.append("Udev rule installed")
        
        # Reload udev rules
        subprocess.run(["sudo", "udevadm", "control", "--reload-rules"], check=False)
        subprocess.run(["sudo", "udevadm", "trigger"], check=False)
        output_lines.append("Udev rules reloaded")
    except subprocess.CalledProcessError as e:
        output_lines.append(f"Warning: Could not install udev rule (need sudo): {e}")
    except Exception as e:
        output_lines.append(f"Warning: Udev rule setup failed: {e}")
    
    # 2. Install systemd user service
    service_content = f"""[Unit]
Description=USB Smart Dashboard Display
After=graphical-session.target

[Service]
Type=simple
ExecStart=/home/fm/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile scout gateway run
Environment=HERMES_HOME=/home/fm/.hermes/profiles/scout
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    service_path = Path.home() / ".config/systemd/user/usb-smart-dashboard.service"
    
    try:
        service_path.parent.mkdir(parents=True, exist_ok=True)
        if not service_path.exists():
            service_path.write_text(service_content)
            output_lines.append("Systemd user service created")
        else:
            output_lines.append("Systemd user service already exists")
    except Exception as e:
        output_lines.append(f"Warning: Could not create systemd service: {e}")
    
    # 3. Ensure config.yaml has default settings
    config_yaml = Path.home() / ".hermes/profiles/scout/config.yaml"
    if config_yaml.exists():
        import yaml
        try:
            cfg = yaml.safe_load(config_yaml.read_text()) or {}
            plugins_cfg = cfg.setdefault("plugins", {})
            entries = plugins_cfg.setdefault("entries", {})
            if plugin_name not in entries:
                entries[plugin_name] = {
                    "settings": {
                        "enabled": True,
                        "driver": "turing",
                        "brightness": 25,
                        "display_width": 480,
                        "display_height": 320,
                        "font_size": 28,
                    }
                }
                config_yaml.write_text(yaml.dump(cfg, default_flow_style=False))
                output_lines.append("Config.yaml updated with default settings")
            else:
                output_lines.append("Config.yaml already has plugin settings")
        except Exception as e:
            output_lines.append(f"Warning: Could not update config.yaml: {e}")
    
    # 4. Check if device is available
    try:
        import serial.tools.list_ports as list_ports
        found = False
        for port in list_ports.comports():
            if port.vid == 0x1a86 and port.pid == 0x5722:
                output_lines.append(f"Device found at {port.device}")
                found = True
                break
        if not found:
            output_lines.append("Device not currently connected (will auto-detect on connect)")
    except Exception:
        output_lines.append("Could not check device status")
    
    return {"ok": True, "output": "\n".join(output_lines)}


if __name__ == "__main__":
    import json
    result = run("", "usb-smart-dashboard", {})
    print(json.dumps(result))