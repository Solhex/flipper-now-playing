#!/usr/bin/env python3
"""Start the Flipper bridge at login on Linux (systemd user service).

    python3 linux_startup.py install --name Gumi      # venv + unit + enable --now
    python3 linux_startup.py status                   # unit, state, log tail
    python3 linux_startup.py uninstall                # disable and remove the unit

The Windows twin of this is windows_startup.py. Both exist so starting the
bridge is one command instead of "read the bridge's flags, then write a unit by
hand": nothing below is required to *run* ytm_bridge.py, it just keeps it up.

The venv lives at ~/.local/share/ytm-remote/venv (or $XDG_DATA_HOME) so the
service never depends on whatever Python the desktop happens to use, and the
unit is written with absolute paths for this checkout - `systemctl --user edit`
or moving the repo means re-running `install`.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "ytm_bridge.py"
REQUIREMENTS = HERE / "requirements.txt"
UNIT_NAME = "ytm-remote.service"


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def venv_dir() -> Path:
    return data_home() / "ytm-remote" / "venv"


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def unit_path() -> Path:
    return config_home() / "systemd" / "user" / UNIT_NAME


def _systemd_quote(token: str) -> str:
    """Quote one ExecStart token the way systemd parses them."""
    if token and all(c.isalnum() or c in "-_./=:@" for c in token):
        return token
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True)


def _need_systemctl() -> None:
    if shutil.which("systemctl") is None:
        sys.exit("systemctl not found: this installer needs a systemd user session")
    probe = _systemctl("is-system-running")
    if probe.returncode != 0 and "offline" in (probe.stdout + probe.stderr).lower():
        sys.exit("no systemd user session here (headless or container?). Run the "
                 "bridge directly instead: python3 ytm_bridge.py --name Gumi")


def ensure_venv() -> Path:
    python = venv_python()
    if not python.exists():
        print(f"Creating venv in {venv_dir()} ...")
        venv_dir().parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir())],
                                capture_output=True, text=True)
        if result.returncode != 0 or not python.exists():
            sys.exit(f"could not create the venv: {result.stderr.strip()}")
    deps = ["pip", "install", "--quiet", "-r", str(REQUIREMENTS)]
    print("Installing dependencies ...")
    result = subprocess.run([str(python), "-m", *deps], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"pip install failed:\n{result.stdout}\n{result.stderr}")
    return python


def unit_text(python: Path, name: str, extra_args: str) -> str:
    command = [str(python), str(BRIDGE), "--name", name, "--wait-for-player"]
    command += shlex.split(extra_args)
    exec_start = " ".join(_systemd_quote(token) for token in command)
    return f"""[Unit]
Description=YT Music Remote bridge (Flipper now-playing over BLE)
Documentation=file://{HERE / "README.md"}
After=bluetooth.target
PartOf=graphical-session.target

[Service]
Type=simple
# playerctl talks MPRIS on the session bus, which user units do not inherit
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
# otherwise the journal gets nothing until Python's buffer happens to flush
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={HERE}
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def install(name: str, extra_args: str) -> None:
    _need_systemctl()
    python = ensure_venv()
    unit_path().parent.mkdir(parents=True, exist_ok=True)
    unit_path().write_text(unit_text(python, name, extra_args), encoding="utf-8")
    _systemctl("daemon-reload")
    enabled = _systemctl("enable", "--now", UNIT_NAME)
    if enabled.returncode != 0:
        sys.exit(f"wrote {unit_path()} but could not enable it:\n{enabled.stderr.strip()}")
    print(f"Registered: {unit_path()}")
    print(f"  runs     : {python} {BRIDGE.name} --name {name} --wait-for-player {extra_args}".rstrip())
    print(f"  log      : journalctl --user -u {UNIT_NAME} -f")
    print("\nIt starts at your next login and is running now.")


def uninstall() -> None:
    _need_systemctl()
    _systemctl("disable", "--now", UNIT_NAME)
    if unit_path().exists():
        unit_path().unlink()
        _systemctl("daemon-reload")
        print(f"Removed {unit_path()}")
    else:
        print("Nothing registered.")
    print(f"The venv is left in place ({venv_dir()}); delete it by hand if you want.")


def status() -> None:
    if not unit_path().exists():
        print(f"Not registered ({unit_path()} does not exist).")
        print("Install it with: python3 linux_startup.py install --name <your Flipper>")
        return
    active = _systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"
    enabled = _systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown"
    print(f"Registered: {unit_path()}")
    print(f"  state    : {active} ({enabled})")
    for line in unit_path().read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            print(f"  runs     : {line.split('=', 1)[1]}")
    logs = _systemctl("status", UNIT_NAME, "--no-pager", "-n", "6")
    print("  log tail :")
    for line in (logs.stdout or "").splitlines()[-6:]:
        print(f"    | {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the Flipper bridge at Linux login")
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    ap.add_argument("--name", default=os.environ.get("FLIPPER_NAME", "Flipper"),
                    help="BLE name substring of your Flipper")
    ap.add_argument("--args", default="", help="extra bridge arguments, e.g. "
                                               "'--player firefox --hz 15'")
    args = ap.parse_args()

    if os.name == "nt":
        sys.exit("linux_startup.py is Linux-only; on Windows use windows_startup.py")
    if not BRIDGE.exists():
        sys.exit(f"ytm_bridge.py not found next to this script ({BRIDGE})")

    {"install": lambda: install(args.name, args.args),
     "uninstall": uninstall,
     "status": status}[args.action]()


if __name__ == "__main__":
    main()
