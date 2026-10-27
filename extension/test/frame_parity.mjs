/* Build the wire frames with the extension's OWN code and print them as hex, so
 * tests/test_frames_js.py can compare them byte for byte with ytm_bridge.py.
 *
 * remote.js is loaded exactly as shipped, plus one appended line that exposes
 * its internals (top-level `function` declarations and the `let state` the
 * builders read). Nothing in the file itself is changed for the test.
 *
 * Needs no browser: the handful of things remote.js touches at load time are
 * stubbed below, and no connection is ever opened.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "remote.js"), "utf8");

const HOOK = `
globalThis.__ytm = { setState: (s) => { state = s; },
  frame, metaFrame, artFrame, likeFrame, volFrame, toastFrame, enc };
`;

const el = () => ({ addEventListener() {}, textContent: "", className: "" });
globalThis.chrome = {
  runtime: { connect: () => ({ postMessage() {}, onMessage: { addListener() {} } }) },
};
globalThis.document = { getElementById: el };
/* navigator is not stubbed on purpose: node provides one and it has no
 * `bluetooth`, which is the same shape the real code handles (autoConnect
 * returns immediately), and node 26 makes the global getter-only anyway. */

vm.runInThisContext(src + HOOK, { filename: "remote.js" });

const y = globalThis.__ytm;
const hex = (u8) => Buffer.from(u8).toString("hex");

const out = {};

y.setState({ playing: true, pos: 61, dur: 362, title: "Tïtle", artist: "Artist" });
out.meta_full = hex(y.metaFrame(true));
out.meta_clock = hex(y.metaFrame(false));

out.like_yes = hex(y.likeFrame(1));
out.like_unknown = hex(y.likeFrame(2));
out.vol = hex(y.volFrame(100));
out.toast = hex(y.toastFrame("Liked", 1));

/* 8x2 art with a known pattern: checks the envelope and that bits land
 * LSB-first within each row, which is what the device's XBM wants. */
out.art = hex(y.artFrame(8, 2, [0b10101010, 0b01010101]));

y.setState({ playing: false, pos: 0, dur: 0, title: "x".repeat(200), artist: "y".repeat(200) });
out.meta_truncated = hex(y.metaFrame(true));

/* A multi-byte character split by the byte limit: both hosts truncate on bytes,
 * so both must produce the same (possibly invalid) tail. */
y.setState({ playing: true, pos: 5, dur: 9, title: "é".repeat(40), artist: "" });
out.meta_split_char = hex(y.metaFrame(true));

console.log(JSON.stringify(out));
