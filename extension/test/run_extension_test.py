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

# Which build to load: the working source by default, or a packaged one, e.g.
#   python extension/test/run_extension_test.py 30 extension/dist/chrome
ROOT = Path(__file__).resolve().parent
EXT_SRC = Path(sys.argv[2]) if len(sys.argv) > 2 else (ROOT.parent)
PAGE_PORT = 8099
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def serve_pages():
    pages = ROOT          # this folder holds the mock page, whatever build is tested
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(pages), **k)
    server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def build_test_extension():
    folder = Path(tempfile.mkdtemp(prefix="ytm-ext-test-"))
    for name in ("manifest.json", "content.js", "background.js"):
        shutil.copy(EXT_SRC / name, folder / name)
    if (EXT_SRC / "icons").is_dir():
        shutil.copytree(EXT_SRC / "icons", folder / "icons")
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
