#!/usr/bin/env python3
"""
Package the extension for both browser families.

    python extension/build.py            # -> extension/dist/*.xpi, *.zip

One source, two manifests: Chrome and Firefox disagree about how a Manifest V3
background script is declared (service worker vs event page) and Firefox wants a
`browser_specific_settings` block while Chromium warns about it. Everything else
— the content script, the worker, the icons — is shared byte for byte.

The `.xpi` is just a zip; signing is what makes Firefox install it permanently,
see the README.
"""

import json
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
# remote.html/remote.js are the Web Bluetooth link page: a document is the only
# context that can hold the GATT connection, so it ships with every target —
# except Firefox, which has no Web Bluetooth and cannot use any of it.
SHARED = ("content.js", "background.js", "remote.html", "remote.js")
WEB_BLUETOOTH_ONLY = ("remote.html", "remote.js")
ICONS = "icons"


def build_manifest(base, target):
    manifest = json.loads(json.dumps(base))          # deep copy

    if target == "firefox":
        manifest["background"] = {"scripts": ["background.js"]}
        # Firefox has no Web Bluetooth: the permission would only produce an
        # "unknown permission" warning, the Web Bluetooth link page could never
        # work, and a toolbar button opening it would only say so. Ship none of
        # the three; the loopback bridge is the path there.
        manifest["permissions"] = [p for p in manifest.get("permissions", [])
                                   if p != "bluetooth"]
        manifest.pop("action", None)
        manifest.setdefault("browser_specific_settings", {}).setdefault(
            "gecko", {}).update({
                "id": "ytm-bridge@solhex.local",
                "strict_min_version": "140.0",
                # Required for new AMO listings. It collects nothing: the only
                # network traffic is to 127.0.0.1, i.e. the user's own bridge.
                "data_collection_permissions": {"required": ["none"]},
            })
    elif target == "chrome":
        manifest["background"] = {"service_worker": "background.js"}
        manifest.pop("browser_specific_settings", None)
        manifest["minimum_chrome_version"] = "116"
    else:
        raise SystemExit(f"unknown target {target}")
    return manifest


def package(target):
    folder = DIST / target
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)

    base = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    manifest = build_manifest(base, target)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                          encoding="utf-8")

    for name in SHARED:
        if target == "firefox" and name in WEB_BLUETOOTH_ONLY:
            continue
        shutil.copy(HERE / name, folder / name)
    shutil.copytree(HERE / ICONS, folder / ICONS)

    version = manifest["version"]
    suffix = "xpi" if target == "firefox" else "zip"
    archive = DIST / f"ytm-bridge-{version}-{target}.{suffix}"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(folder).as_posix())
    return archive, folder


def main():
    DIST.mkdir(parents=True, exist_ok=True)
    for target in ("firefox", "chrome"):
        archive, folder = package(target)
        size = archive.stat().st_size
        print(f"{target:8} {archive.relative_to(HERE)}  ({size} bytes)  "
              f"unpacked in {folder.relative_to(HERE)}")
    print("\nFirefox, permanently: sign the .xpi (see README) and then")
    print("  about:addons -> gear -> Install Add-on From File…")
    print("Chrome/Edge: chrome://extensions -> Load unpacked -> extension/dist/chrome")


if __name__ == "__main__":
    main()
