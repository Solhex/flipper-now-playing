"""Load the extension into a real Chromium browser and drive it end to end.

This runs its **own** bridge on a free port and points the copied extension at it,
so it never touches the bridge you are actually using. (An earlier version talked
to the live one, and with two browsers polling the same bridge the like state
raced — it toggled the user's real track.)

The mock page in this folder carries the same like-button DOM as the site.

    python extension/test/run_extension_test.py [seconds] [build-dir]
    YTM_TEST_BROWSER="C:/path/to/brave.exe" python extension/test/run_extension_test.py

Exit code 0 means: the extension loaded, reported the mock track, and a toggle
round-trip liked the track and unliked it again.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # extension/test
EXT_SRC = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent
REPO = ROOT.parent.parent
PAGE_PORT = 8099
BRIDGE_PORT = 8765                              # the port the copy is re-pointed from

sys.path.insert(0, str(REPO))
import ytm_bridge as y                          # noqa: E402

# Any Chromium browser will do: Edge, Chrome, Brave, Vivaldi...
BROWSERS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve_pages():
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(ROOT), **k)
    server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def build_test_extension(port):
    """The build under test, aimed at this run's bridge and at the mock page."""
    folder = Path(tempfile.mkdtemp(prefix="ytm-ext-test-"))
    for name in ("content.js", "background.js"):
        shutil.copy(EXT_SRC / name, folder / name)
    if (EXT_SRC / "icons").is_dir():
        shutil.copytree(EXT_SRC / "icons", folder / "icons")

    worker = (folder / "background.js").read_text(encoding="utf-8")
    worker = worker.replace(f"127.0.0.1:{BRIDGE_PORT}", f"127.0.0.1:{port}")
    (folder / "background.js").write_text(worker, encoding="utf-8")

    manifest = json.loads((EXT_SRC / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"] = [
        url.replace(f"127.0.0.1:{BRIDGE_PORT}", f"127.0.0.1:{port}")
        for url in manifest["host_permissions"]
    ]
    manifest["host_permissions"].append(f"http://127.0.0.1:{PAGE_PORT}/*")
    manifest["content_scripts"][0]["matches"].append(f"http://127.0.0.1:{PAGE_PORT}/*")
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder


def report(failures):
    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS")
    return 0


def main():
    requested = os.environ.get("YTM_TEST_BROWSER")
    browser = requested or next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser or not Path(browser).exists():
        print("no Chromium browser found")
        return 2

    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    link = y.BrowserLink(free_port())
    if not link.start():
        print("could not start the test bridge:", link.error)
        return 2

    ext = build_test_extension(link.port)
    profile = Path(tempfile.mkdtemp(prefix="ytm-ext-profile-"))
    server = serve_pages()
    url = f"http://127.0.0.1:{PAGE_PORT}/mock-youtube-music.html"
    print(f"browser : {browser}")
    print(f"bridge  : 127.0.0.1:{link.port} (test only, the real bridge is untouched)")
    print(f"page    : {url}")

    process = subprocess.Popen([
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--load-extension={ext}",
        f"--disable-extensions-except={ext}", f"--user-data-dir={profile}", url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    failures = []
    try:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline and not link.fresh():
            time.sleep(0.5)
        if not link.fresh():
            failures.append("the extension never reported to the bridge")
            return report(failures)

        print(f"reports : {link.requests}")
        print(f"state   : {link.state()}")
        if "Mock Track" not in link.state()["title"]:
            failures.append(f"unexpected title {link.state()['title']!r}")

        first = y.link_toggle(link, timeout=6.0, attempts=2)
        print(f"toggle 1-> liked={first}")
        if first is not True:
            failures.append(f"the toggle did not like the track (got {first!r})")

        second = y.link_toggle(link, timeout=6.0, attempts=2)
        print(f"toggle 2-> liked={second}")
        if second is not False:
            failures.append(f"the toggle did not unlike the track (got {second!r})")
        print(f"polls   : {link.polls}  changes: {link.changes}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        server.shutdown()
        link.close()
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(ext, ignore_errors=True)

    return report(failures)


if __name__ == "__main__":
    sys.exit(main())
