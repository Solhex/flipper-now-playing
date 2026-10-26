"""Load the extension into a real Chromium browser against the mock page.

The shipped manifest only matches music.youtube.com, so this copies the extension
to a temp folder and adds the mock page's origin — the content script and the
background worker themselves are untouched.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EXT_SRC = Path(r"C:\Users\fvern\flipper-now-playing\extension")
PAGE_PORT = 8099
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def serve_pages():
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(EXT_SRC / "test"), **k)
    server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def build_test_extension():
    folder = Path(tempfile.mkdtemp(prefix="ytm-ext-test-"))
    for name in ("manifest.json", "content.js", "background.js"):
        shutil.copy(EXT_SRC / name, folder / name)
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["host_permissions"].append(f"http://127.0.0.1:{PAGE_PORT}/*")
    manifest["content_scripts"][0]["matches"].append(f"http://127.0.0.1:{PAGE_PORT}/*")
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return folder


def main():
    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print("no Chromium browser found")
        return 2
    ext = build_test_extension()
    profile = Path(tempfile.mkdtemp(prefix="ytm-ext-profile-"))
    server = serve_pages()
    print("browser:", browser)
    print("extension copy:", ext)
    url = f"http://127.0.0.1:{PAGE_PORT}/mock-youtube-music.html"
    process = subprocess.Popen([
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--load-extension={ext}",
        f"--disable-extensions-except={ext}", f"--user-data-dir={profile}",
        "--enable-logging=stderr", "--v=0", url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("extension loaded; leaving the browser up while the bridge is driven...")
    time.sleep(int(sys.argv[1]) if len(sys.argv) > 1 else 25)
    process.terminate()
    time.sleep(2)
    server.shutdown()
    print("done")
    return 0


sys.exit(main())
