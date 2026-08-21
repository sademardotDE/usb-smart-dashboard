#!/usr/bin/env python3
"""
Pre-uninstall hook for USB Smart Dashboard plugin.

Runs before plugin removal. Performs:
- Stop systemd service
- Remove udev rule (optional - ask user)
- Backup config
"""

import subprocess
import sys
from pathlib import Path


def run(plugin_dir: str, plugin_name: str, config: dict) -> dict:
    output_lines = []
    
    # 1. Stop systemd service if running
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "usb-smart-dashboard.service"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            subprocess.run(["systemctl", "--user", "stop", "usb-smart-dashboard.service"], check=False)
            output_lines.append("Stopped systemd service")
    except Exception as e:
        output_lines.append(f"Could not stop service: {e}")
    
    # 2. Disable systemd service
    try:
        subprocess.run(["systemctl", "--user", "disable", "usb-smart-dashboard.service"], check=False)
        output_lines.append("Disabled systemd service")
    except Exception as e:
        output_lines.append(f"Could not disable service: {e}")
    
    # 3. Check if we should remove udev rule
    # We keep it by default since other plugins might use it
    udev_path = Path("/etc/udev/rules.d/99-usb-smart-dashboard.rules")
    if udev_path.exists():
        output_lines.append("Udev rule kept (remove manually if desired: sudo rm /etc/udev/rules.d/99-usb-smart-dashboard.rules)")
    
    # 4. Save current config for potential reinstall
    config_yaml = Path.home() / ".hermes/profiles/scout/config.yaml"
    if config_yaml.exists():
        import yaml
        try:
            cfg = yaml.safe_load(config_yaml.read_text()) or {}
            plugins_cfg = cfg.get("plugins", {})
            entries = plugins_cfg.get("entries", {})
            if plugin_name in entries:
                # Save settings to a backup file
                backup_dir = Path.home() / ".hermes" / "plugin-backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_file = backup_dir / f"{plugin_name}-settings-backup.yaml"
                backup_file.write_text(yaml.dump({plugin_name: entries[plugin_name]}, default_flow_style=False))
                output_lines.append(f"Settings backed up to {backup_file}")
        except Exception as e:
            output_lines.append(f"Could not backup settings: {e}")
    
    return {"ok": True, "output": "\n".join(output_lines)}


if __name__ == "__main__":
    import json
    result = run("", "usb-smart-dashboard", {})
    print(json.dumps(result))