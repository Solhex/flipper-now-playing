/* Flipper link, held by this extension page.
 *
 * Web Bluetooth is not available in a service worker (nor in Web Workers), so
 * the GATT connection has to live in a real document - that is what this page
 * is. Keep the tab open (pinned is fine): closing it drops the Flipper link,
 * because the connection is bound to this document.
 *
 * The wire format is byte-identical to the Python bridge, so the same .fap
 * works with either host.
 */
"use strict";

/* ---- the Flipper's serial service (from the firmware's own UUIDs) ---- */
const SERIAL_SVC = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000";
const RX_UUID = "19ed82ae-ed21-4c9d-4145-228e62fe0000"; // PC -> Flipper (write)
const TX_UUID = "19ed82ae-ed21-4c9d-4145-228e61fe0000"; // Flipper -> PC (indicate)

/* ---- frame types / constants (mirror ytm_bridge.py) ---- */
const FRAME_META = 0x01, FRAME_ART = 0x02, FRAME_VOL = 0x03, FRAME_TOAST = 0x04, FRAME_LIKE = 0x05;
const LIKE_NO = 0, LIKE_YES = 1, LIKE_UNKNOWN = 2;
const ART_SIZE = 44;
const TITLE_MAX = 64, ARTIST_MAX = 48;
const PUSH_MS = 100;      // metadata cadence
const CHUNK = 20;         // bytes per BLE write (what the proven bridge uses)

const $ = (id) => document.getElementById(id);
const log = (msg) => {
  const el = $("log");
  if (el) {
    el.textContent += `${new Date().toLocaleTimeString()}  ${msg}\n`;
    el.scrollTop = el.scrollHeight;
  }
};

/* ------------------------------------------------------------------ state */
let device = null, server = null, rxChar = null, txChar = null;
let caps = false;                 // device said it takes clock-only META ('k')
let state = null;                 // latest report from the content script
let lastText = null, lastArt = null, lastLike = null, lastVol = null;
let textDirty = true;
let awaitingLike = false;   // a Flipper like is in flight: toast when it lands
const port = chrome.runtime.connect({ name: "remote" });

port.onMessage.addListener((msg) => {
  if (msg && msg.type === "state") onState(msg.payload);
});

/* --- What the device can actually draw ------------------------------------
 * The Flipper's fonts are Latin-only bitmap faces, so a Korean or Japanese title would
 * reach the screen as blank space. Fold accents, romanise Hangul (mechanical,
 * so it needs no dictionary) and mark anything else with '?'. Mirrors
 * device_text() in ytm_bridge.py, which also romanises Japanese and Chinese
 * when its optional dictionaries are installed; the extension cannot bundle
 * megabytes of readings, so it marks those.
 */
const HANGUL_INITIAL = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss",
  "", "j", "jj", "ch", "k", "t", "p", "h"];
const HANGUL_MEDIAL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa",
  "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"];
const HANGUL_FINAL = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l",
  "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"];

function hangulToLatin(text) {
  let out = "";
  let previousL = false;      /* a final -l doubles into a following -r onset */
  for (const ch of text) {
    const code = ch.codePointAt(0);
    if (code < 0xac00 || code > 0xd7a3) { out += ch; previousL = false; continue; }
    const offset = code - 0xac00;
    let initial = HANGUL_INITIAL[Math.floor(offset / 588)];
    const medial = HANGUL_MEDIAL[Math.floor((offset % 588) / 28)];
    const final = HANGUL_FINAL[offset % 28];
    if (previousL && initial === "r") initial = "l";
    out += initial + medial + final;
    previousL = final === "l";
  }
  return out;
}

function drawable(text) {
  if (!text) return "";
  if (/[^\x00-\x7f]/.test(text)) {
    text = hangulToLatin(text)
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^\x20-\x7e]/gu, "?");
  }
  return text.trim();
}

function onState(p) {
  const prev = state;
  /* The Flipper's fonts are Latin-only, so the device gets a drawable form
   * (mirrors device_text() in ytm_bridge.py). Everything the page reports stays
   * raw for the display below and for the change check, so a title that
   * romanises to the same letters does not resend every tick. */
  state = { ...p, title: drawable(p.title || ""), artist: drawable(p.artist || "") };
  if (!prev || prev.title !== state.title || prev.artist !== state.artist) textDirty = true;
  if (!p.art || p.art !== lastArt) maybeSendArt(p.art);
  maybeSendLike(p.liked === null ? LIKE_UNKNOWN : (p.liked ? LIKE_YES : LIKE_NO));
  if (typeof p.vol === "number" && p.vol !== lastVol) sendVol(p.vol);
  /* A press the page refused must not look like a dead button. `after` is only
   * present once the page has checked the result, so a pending ok:false (set
   * the instant the click is dispatched) does not cry wolf. */
  const click = p.diag && p.diag.click;
  if (awaitingLike && click && click.ok === false && "after" in click) {
    awaitingLike = false;
    send(toastFrame("Like failed", 2));
  }
  show(p);
}

/* ------------------------------------------------------------------ framing */
function frame(type, payload) {
  return Uint8Array.from([type, payload.length & 0xff, (payload.length >> 8) & 0xff, ...payload]);
}
function metaFrame(withText) {
  const payload = [state.playing ? 1 : 0,
                   state.pos & 0xff, (state.pos >> 8) & 0xff,
                   state.dur & 0xff, (state.dur >> 8) & 0xff];
  if (withText) {
    payload.push(...enc(state.title || "", TITLE_MAX), 0, ...enc(state.artist || "", ARTIST_MAX), 0);
  }
  return frame(FRAME_META, payload);
}
const enc = (s, max) => Array.from(new TextEncoder().encode(s)).slice(0, max);

function artFrame(w, h, bits) { return frame(FRAME_ART, [w, h, ...bits]); }
function likeFrame(v) { return frame(FRAME_LIKE, [v]); }
function volFrame(v) { return frame(FRAME_VOL, [v]); }
/* One short line the device shows for a moment: icon 1 = heart, 2 = broken.
 * 23 text bytes, not 22: the Python host sends TOAST_MAX-1 and the two hosts
 * must agree byte for byte (tests/test_frames_js.py enforces it). */
function toastFrame(text, icon) { return frame(FRAME_TOAST, [icon, ...enc(text, 23)]); }

/* ------------------------------------------------------------------ BLE TX */
async function send(bytes) {
  if (!rxChar) return;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    try {
      await rxChar.writeValueWithoutResponse(bytes.slice(i, i + CHUNK));
    } catch (e) {
      log(`write failed: ${e.message}`);
      return;
    }
  }
}

function maybeSendLike(v) {
  if (v === lastLike) return;
  lastLike = v;
  send(likeFrame(v));
  /* Only a toggle the Flipper asked for gets a toast: the site changes this
   * state by itself too, and a toast for those would be noise. */
  if (awaitingLike && v !== LIKE_UNKNOWN) {
    awaitingLike = false;
    send(toastFrame(v === LIKE_YES ? "Liked" : "Unliked", v === LIKE_YES ? 1 : 2));
  }
}
function sendVol(v) { lastVol = v; send(volFrame(v)); }

/* ------------------------------------------------------------------ artwork */
async function maybeSendArt(url) {
  if (!url || url === lastArt) return;
  lastArt = url;
  try {
    /* Fetch through the extension's host permission: the page's own image is
     * cross-origin, and drawing it directly would taint the canvas so the
     * pixels could not be read. A blob URL is same-origin, so it is safe. */
    const res = await fetch(url, { cache: "force-cache" });
    const blob = await res.blob();
    const bmp = await createImageBitmap(blob);
    /* Skip two tracks quickly and the slower fetch finishes last: without this
     * the previous track's cover lands on the device wearing the new track's
     * name (the Python bridge had the same bug - commit 210fb36). */
    if (url !== lastArt) {
      bmp.close && bmp.close();
      return;
    }
    const { bits, w, h } = toXbm(bmp, ART_SIZE);
    if (w && h) {
      await send(artFrame(w, h, bits));
      log(`art ${w}x${h} (${bits.length} B)`);
    }
    bmp.close && bmp.close();
  } catch (e) {
    log(`art failed: ${e.message}`);
  }
}

/* Downscale to 1-bit, Floyd-Steinberg, dark pixels set, bits LSB-first - the
 * order canvas_draw_xbm expects. Mirrors art_to_xbm() in ytm_bridge.py. */
function toXbm(bmp, size) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0, size, size);
  const px = ctx.getImageData(0, 0, size, size).data;
  const buf = new Float32Array(size * size);
  for (let i = 0; i < size * size; i++) {
    const lum = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
    buf[i] = 255 - lum;                       // invert: dark -> high -> set bit
  }
  const rowBytes = (size + 7) >> 3;
  const bits = new Uint8Array(rowBytes * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = y * size + x;
      const old = buf[i];
      const on = old >= 128;
      const nv = on ? 255 : 0;
      if (on) bits[y * rowBytes + (x >> 3)] |= (1 << (x & 7));
      const err = old - nv;
      if (x + 1 < size) buf[i + 1] += err * 7 / 16;
      if (y + 1 < size) {
        if (x > 0) buf[i + size - 1] += err * 3 / 16;
        buf[i + size] += err * 5 / 16;
        if (x + 1 < size) buf[i + size + 1] += err * 1 / 16;
      }
    }
  }
  return { bits, w: size, h: size };
}

/* ------------------------------------------------------------------ RX */
function onNotify(event) {
  const v = event.target.value;
  for (let i = 0; i < v.byteLength; i++) {
    const ch = String.fromCharCode(v.getUint8(i));
    if (ch === "k") { caps = true; log("device: clock-only META ok"); }
    else if (ch === "l") { log("Flipper: like"); awaitingLike = true; port.postMessage({ type: "cmd", cmd: "like" }); }
    else {
      const map = { p: "playpause", n: "next", b: "prev", "-": "seekback", "+": "seekfwd",
                    u: "volup", d: "voldown" };
      if (map[ch]) { log(`Flipper: ${map[ch]}`); port.postMessage({ type: "cmd", cmd: map[ch] }); }
    }
  }
}

/* ------------------------------------------------------------------ link */
function setStatus(text, ok) {
  $("status").textContent = text;
  $("status").className = ok ? "ok" : "";
}

/* Registered once per device. Adding this inside connectWith() meant every
 * reconnect added another handler, so a single drop fired N reconnects at once
 * - N simultaneous attempts at the one link. */
let hookedDevice = null;

function onDisconnected() {
  setStatus("Disconnected - reconnecting...", false);
  server = rxChar = txChar = null;
  setTimeout(() => connectWith(device), 3000);
}

async function connectWith(dev) {
  device = dev;
  if (hookedDevice !== dev) {
    hookedDevice = dev;
    dev.addEventListener("gattserverdisconnected", onDisconnected);
  }
  server = await device.gatt.connect();
  const svc = await server.getPrimaryService(SERIAL_SVC);
  rxChar = await svc.getCharacteristic(RX_UUID);
  txChar = await svc.getCharacteristic(TX_UUID);
  await txChar.startNotifications();
  txChar.addEventListener("characteristicvaluechanged", onNotify);
  /* The serial profile is still coming up for a moment after connect: the first
   * write is often rejected, so give it a beat. */
  await new Promise((r) => setTimeout(r, 500));
  caps = false; lastText = lastArt = lastLike = lastVol = null; textDirty = true;
  setStatus(`Connected to ${device.name || "Flipper"}`, true);
  log(`connected ${device.name || ""}`);
  port.postMessage({ type: "mode", mode: "bluetooth" });
  pushLoop();
}

async function requestConnect() {
  try {
    if (!navigator.bluetooth) {
      setStatus("This browser has no Web Bluetooth - use Firefox with the Python bridge.", false);
      port.postMessage({ type: "mode", mode: "bridge" });
      return;
    }
    setStatus("Choose your Flipper in the browser prompt...", false);
    const dev = await navigator.bluetooth.requestDevice({
      filters: [{ services: [SERIAL_SVC] }],
      optionalServices: [SERIAL_SVC],
    });
    await connectWith(dev);
  } catch (e) {
    setStatus(`Not connected (${e.message})`, false);
    if (e.name === "NotFoundError") {
      log("No Flipper shown: make sure Bluetooth is ON, the app is open, and nothing else is connected.");
    }
  }
}

/* Reconnect without a chooser when permission was already granted (Chromium
 * remembers it), so the page only needs the user once. */
async function autoConnect() {
  if (!navigator.bluetooth || !navigator.bluetooth.getDevices) return;
  try {
    const known = await navigator.bluetooth.getDevices();
    const flipper = known.find((d) => /flipper|gumi/i.test(d.name || "")) || known[0];
    if (flipper) { log(`reconnecting to ${flipper.name || "known device"}`); await connectWith(flipper); }
  } catch (e) { /* nothing remembered yet */ }
}

/* ------------------------------------------------------------------ push */
let pushing = false;
function pushLoop() {
  if (pushing) return;
  pushing = true;
  (async () => {
    while (rxChar) {
      if (state) {
        const withText = textDirty || !caps;
        await send(metaFrame(withText));
        if (withText) textDirty = false;
      }
      await new Promise((r) => setTimeout(r, PUSH_MS));
    }
    pushing = false;
  })();
}

/* ------------------------------------------------------------------ UI */
function show(p) {
  if (!p) return;
  $("track").textContent = (p.title || "-") + (p.artist ? " - " + p.artist : "");
  const like = p.liked === null ? "?" : (p.liked ? "<3" : "--");
  const t = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  $("meta").textContent = `${like}   ${t(p.pos || 0)} / ${t(p.dur || 0)}   ${p.playing ? ">" : "||"}   vol ${p.vol ?? "?"}%`;
}

$("connect").addEventListener("click", requestConnect);
$("disconnect").addEventListener("click", () => {
  if (device && device.gatt.connected) device.gatt.disconnect();
  setStatus("Disconnected", false);
});

setStatus("Not connected", false);
autoConnect();
