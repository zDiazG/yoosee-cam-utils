#!/usr/bin/env python3
"""Install the scoped user service after configuration and TLS preparation."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--config", type=Path, required=True)
p.add_argument("--apply", action="store_true")
a = p.parse_args()
root = Path(__file__).resolve().parent.parent
config = a.config.resolve(strict=True)
if any(ch in str(root) + str(config) for ch in "\r\n"):
    raise SystemExit("Newlines are not allowed in deployment paths")
unit = Path.home() / ".config/systemd/user/yoosee-cam-web.service"
def quote(s):
    return json.dumps(str(s).replace("%", "%%").replace("$", "$$"))
text = f'''# Managed by yoosee-cam-utils
[Unit]
Description=Authenticated local camera viewer
After=network-online.target
[Service]
Type=simple
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(root / "scripts/yoosee-web")} --config {quote(config)}
Environment=PYTHONUNBUFFERED=1
RuntimeDirectory=yoosee-cam-utils
UMask=0077
Nice=10
MemoryMax=512M
KillMode=control-group
TimeoutStopSec=10
Restart=on-failure
RestartSec=3
[Install]
WantedBy=default.target
'''
if not a.apply:
    print(f"Would install/enable {unit}; config {config}")
else:
    with tempfile.TemporaryDirectory() as staging:
        candidate = Path(staging) / unit.name
        candidate.write_text(text)
        subprocess.run(["systemd-analyze", "--user", "verify", str(candidate)], check=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    if unit.exists():
        previous = unit.read_text()
        if not previous.startswith("# Managed by yoosee-cam-utils\n"):
            raise SystemExit("Refusing to replace an unmanaged service")
        if previous != text:
            unit.with_name(unit.name + f".before-{time.time_ns()}").write_text(previous)
    unit.write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
    print(f"Enabled {unit.name}. Use systemctl --user stop {unit.name} to stop it.")
