#!/usr/bin/env python3
"""
windows_startup — make the bridge start by itself when you log in to Windows.

    python windows_startup.py install --name Gumi     # add it
    python windows_startup.py status                  # is it registered?
    python windows_startup.py uninstall               # remove it

It writes a shortcut into the per-user Startup folder, which needs no
administrator rights and no service wrapper. The shortcut runs `pythonw.exe`, so
nothing flashes on screen at login and no console window is left behind; the
bridge appends its usual log lines to `%LOCALAPPDATA%\\ytm-remote\\bridge.log`
instead, and `--log` is what makes that work with no stdout.

Two details worth keeping:

* `pythonw.exe` and the script are addressed by absolute path. A Startup
  shortcut inherits almost no environment, so anything that relied on the
  current directory or on PATH would quietly fail at login.
* The arguments carry `--name` and `--log` explicitly, because that shortcut is
  the only place the setting lives — unlike the Linux service there is no unit
  file to read it back from.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "YT Music Remote Bridge.lnk"
# PowerShell is a console app: launched from pythonw.exe it would open a console
# window of its own. The installer usually runs from a terminal, but keeping it
# silent costs nothing and avoids a flash when it is re-run from a scheduled task.
CREATE_NO_WINDOW = 0x08000000
HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "ytm_bridge.py"


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        sys.exit("APPDATA is not set — this only works on Windows")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path():
    return startup_dir() / SHORTCUT_NAME


def pythonw():
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def log_path():
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(local) / "ytm-remote" / "bridge.log"


def _ps_quote(text):
    """PowerShell single-quoted literal: double any quote inside it."""
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(name, extra_args, log):
    args = f'"{BRIDGE}" --name "{name}" --log "{log}" {extra_args}'.strip()
    script = f"""
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut({_ps_quote(shortcut_path())})
$lnk.TargetPath = {_ps_quote(pythonw())}
$lnk.Arguments = {_ps_quote(args)}
$lnk.WorkingDirectory = {_ps_quote(HERE)}
$lnk.WindowStyle = 7
$lnk.Description = 'Flipper YT Music Remote bridge'
$lnk.Save()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        sys.exit(f"could not create the shortcut: {result.stderr.strip()}")


def read_shortcut():
    """(target, arguments) of the existing shortcut, or None."""
    if not shortcut_path().exists():
        return None
    script = f"""
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut({_ps_quote(shortcut_path())})
Write-Output $lnk.TargetPath
Write-Output $lnk.Arguments
Write-Output $lnk.WorkingDirectory
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines()]
    while len(lines) < 3:
        lines.append("")
    return {"target": lines[0], "arguments": lines[1], "workdir": lines[2]}


def install(name, extra_args):
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    startup_dir().mkdir(parents=True, exist_ok=True)
    create_shortcut(name, extra_args, log)
    found = read_shortcut()
    if found is None:
        sys.exit("the shortcut was written but could not be read back")
    print(f"Registered: {shortcut_path()}")
    print(f"  runs     : {found['target']}")
    print(f"  arguments: {found['arguments']}")
    print(f"  log      : {log}")
    print("\nIt starts at your next logon. Start it now with:")
    print(f'  "{found["target"]}" {found["arguments"]}')


def uninstall():
    if shortcut_path().exists():
        shortcut_path().unlink()
        print(f"Removed {shortcut_path()}")
    else:
        print("Nothing registered.")


def status():
    found = read_shortcut()
    if not found:
        print(f"Not registered ({shortcut_path()} does not exist).")
        return
    print(f"Registered: {shortcut_path()}")
    print(f"  target   : {found['target']}")
    print(f"  arguments: {found['arguments']}")
    log = log_path()
    if log.exists():
        size = log.stat().st_size
        print(f"  log      : {log} ({size} bytes)")
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        for line in tail:
            print(f"    | {line}")
    else:
        print(f"  log      : {log} (not created yet)")


def main():
    ap = argparse.ArgumentParser(description="Start the Flipper bridge at Windows logon")
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    ap.add_argument("--name", default=os.environ.get("FLIPPER_NAME", "Flipper"),
                    help="BLE name substring of your Flipper")
    ap.add_argument("--args", default="", help="extra bridge arguments, e.g. "
                                              "'--player firefox --hz 15'")
    args = ap.parse_args()

    if os.name != "nt":
        sys.exit("windows_startup.py is Windows-only; on Linux use ytm-remote.service")
    if not BRIDGE.exists():
        sys.exit(f"ytm_bridge.py not found next to this script ({BRIDGE})")

    {"install": lambda: install(args.name, args.args),
     "uninstall": uninstall,
     "status": status}[args.action]()


if __name__ == "__main__":
    main()
