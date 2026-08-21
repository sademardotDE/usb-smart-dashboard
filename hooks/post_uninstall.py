#!/usr/bin/env python3
"""
Post-uninstall hook for USB Smart Dashboard plugin.

Runs after plugin directory is removed. Performs:
- Clean up systemd service file
- Notify user of manual cleanup steps
"""

import subprocess
from pathlib import Path


def run(plugin_dir: str, plugin_name: str, config: dict) -> dict:
    output_lines = []
    
    # 1. Remove systemd service file
    service_path = Path.home() / ".config/systemd/user/usb-smart-dashboard.service"
    if service_path.exists():
        try:
            service_path.unlink()
            output_lines.append("Removed systemd user service file")
            # Reload systemd daemon
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        except Exception as e:
            output_lines.append(f"Could not remove service file: {e}")
    
    # 2. Note about udev rule
    output_lines.append("Udev rule kept at /etc/udev/rules.d/99-usb-smart-dashboard.rules")
    output_lines.append("To remove: sudo rm /etc/udev/rules.d/99-usb-smart-dashboard.rules && sudo udevadm control --reload-rules")
    
    # 3. Note about config backup
    backup_dir = Path.home() / ".hermes" / "plugin-backups"
    backup_file = backup_dir / f"usb-smart-dashboard-settings-backup.yaml"
    if backup_file.exists():
        output_lines.append(f"Settings backup available at {backup_file}")
    
    # 4. Note about Python packages
    output_lines.append("Python packages (pyserial, pillow, numpy) kept installed")
    output_lines.append("To remove: pip uninstall pyserial pillow numpy")
    
    return {"ok": True, "output": "\n".join(output_lines)}


if __name__ == "__main__":
    import json
    result = run("", "usb-smart-dashboard", {})
    print(json.dumps(result))