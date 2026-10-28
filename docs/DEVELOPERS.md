# Developers

Internals for YT Music Remote. The user guide is [../README.md](../README.md);
this is the part you need only if you are changing the code, the protocol or the
packaging.

## Repo layout

| Path | Role |
|---|---|
| `ytm_remote.c`, `application.fam` | the Flipper app (frames in, screen out, buttons back) |
| `ytm_bridge.py` | PC host: media session -> BLE, buttons -> the player |
| `extension/` | the add-on: page state -> BLE, buttons -> the page |
| `extension/build.py` | packages it for Chromium and Firefox |
| `windows_startup.py` | Windows: start the bridge at logon |
| `linux_startup.py` | Linux: install the systemd user service (autostart) |
| `tests/` | unit tests - no Flipper, browser or media player needed |

## Tests

```bash
python -m unittest discover -s tests -v
```

## Extension packaging

```bash
python extension/build.py
#  extension/dist/ytm-bridge-2.0.2-chrome.zip    (+ unpacked in dist/chrome)
#  extension/dist/ytm-bridge-2.0.2-firefox.xpi   (+ unpacked in dist/firefox)
```

`extension/dist/` is generated and gitignored - only the Firefox side needs it,
because its `.xpi` has to be signed:

```bash
export AMO_JWT_ISSUER=... AMO_JWT_SECRET=...     # from addons.mozilla.org, keep private
npx web-ext sign --source-dir extension/dist/firefox --channel unlisted
```

Chromium: load `extension/dist/chrome` unpacked. Firefox: load
`extension/dist/firefox/manifest.json` via `about:debugging` (temporary), or sign
the `.xpi` and install it from `about:addons` -> gear -> *Install Add-on From
File...*.

The Bluetooth link lives in an extension page, not the worker -
`navigator.bluetooth` only exists in a document, so a background script cannot
hold it.

## Transport

The Flipper's BLE **serial profile** - service
`8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000`, chars `19ed82ae-ed21-4c9d-4145-...`:
`...228e62...` = RX (PC->Flipper, write), `...228e61...` = TX (Flipper->PC,
**indicate**), `...228e63...` = flow control, `...228e64...` = RPC status.

## Wire format

`[type:1][len:2 LE][payload]`:

| Type | Payload |
|---|---|
| `0x01` META | `[state:1][pos:2 LE][dur:2 LE][title]0[artist]0` - exactly 5 bytes = a clock tick, keep the text (only after the device sends `k`) |
| `0x02` ART | `[w:1][h:1][1-bit rows, (w+7)/8 per row, LSB-first]` |
| `0x03` VOL | `[percent:1]` |
| `0x04` TOAST | `[icon:1][text]` - icon 0 none, 1 heart, 2 broken heart |
| `0x05` LIKE | `[state:1]` - 0 no, 1 yes, 2 unknown |

Flipper -> host: one byte - `p` `n` `b` `-` `+` `u` `d`, plus `k` ("I take
clock-only META") and `l` (like). Unknown types are dropped and the parser
resyncs, so either host can be updated without breaking a build already on the
device. Each frame type is one entry in `FRAME_SPECS` in `ytm_remote.c`, carrying
both its length bound and its handler.

**Seek distance** is one constant (`SEEK_STEP`, 5 s) on every path - the Flipper
sends `-`/`+` and the host decides how far. The exception that looks like one but
isn't: Firefox on Windows has no seek API, so that path types at the tab, and it
taps YouTube Music's *1-second* shortcut (`Shift+H`/`Shift+L`) `SEEK_STEP` times
rather than carrying a distance of its own. Plain `h`/`l` would move 10 s and
disagree with the device.

**Useful bridge flags:** `--name`, `--player`, `--list`, `--hz` (default 20),
`--dump`, `--cmd`, `--pair`, `--address`, `--log PATH`, `--wait-for-player`,
`--toast "text"`, `--ext-port 0` / `--firefox-port 0` (turn the like sources off).

## Handy extras

- Test with no music playing: run a fake MPRIS player, then
  `ytm_bridge.py --player faketest --name Gumi`
  (`templates/fake-mpris-player.py` in the `flipper-zero-app-dev` skill).
- Extension end-to-end test:
  `YTM_TEST_BROWSER=/usr/bin/brave python3 extension/test/run_extension_test.py`
  - it builds its own bridge on a free port, so your setup is untouched.

## The heart, per browser

Nothing in MPRIS or the Windows media session exposes *whether a track is liked* -
only the page knows - so the host has to ask the page:

| Browser | Heart on the Flipper | Like (hold OK) | Needs the bridge? |
|---|---|---|---|
| Brave / Chrome / Edge | yes | real click on the real button | no |
| Firefox + extension | yes | real click on the real button | yes |
| Firefox, no extension | **no - blank** | blind keystroke, tab must be in front | yes |

Without the extension, like is a fallback: on Linux the bridge types YouTube
Music's `shift+=` at the focused window with `vkkey` + `swaymsg`, so the YouTube
Music tab has to be the visible one, and it cannot read the state before or after
- it fires blind and the Flipper draws no heart at all. The other no-extension
route is starting Firefox with `--remote-debugging-port=9222`, which does give
the heart, but only if Firefox is launched that way every time.

**Why Firefox cannot be extension-only:** it has no Web Bluetooth (Mozilla closed
that as WONTFIX). It does have Web Serial, but that only reaches serial ports over
a **USB cable**, and the Flipper's USB port belongs to its firmware, not to an
app, so it cannot carry this data. No add-on changes that; it is a browser
decision.

## The volume number

The app's own level in the mixer (Core Audio on Windows, the Firefox/PipeWire
stream on Linux), not the site's slider: measured here, YouTube Music reported
10% while Firefox's stream sat at 110%, so the site slider cannot make the music
quieter. The number on the screen and the Up/Down keys are that same mixer level,
so they cannot disagree, and it works while paused.

A browser owns one stream per tab, all under the same application name, so the
stream is picked by `media.name` - a stream naming YouTube Music wins over a plain
YouTube tab, which is the only way a volume press stays off the video that happens
to be on screen. Browsers that do not put the tab title in `media.name` fall back
to the newest stream of that application.

## Titles the fonts cannot draw

The Flipper's fonts cover Latin only, so `device_text()` in `ytm_bridge.py`
converts before the frame goes out: accents fold, Korean romanises mechanically,
and anything still undrawable shows `?` instead of blank space. Japanese and
Chinese need readings, which live in `requirements-cjk.txt` (optional - without
them those titles show `?`). The extension marks them too, because it cannot
carry the dictionaries. The device gets the same frame either way.

## While no PC is connected

The device shows a static "Waiting for PC" screen (`draw_waiting` in
`ytm_remote.c`): what the app is waiting for, in words. There is no screensaver
and the backlight is left to the firmware's own setting. BLE keeps advertising, so
the host can connect at any time.
