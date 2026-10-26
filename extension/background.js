/* Talks to the bridge on 127.0.0.1.
 *
 * The work sits here rather than in the content script so the requests come from
 * the extension's own origin: page CSP and Chrome's public-to-private rules then
 * cannot block the loopback call.
 *
 *   POST /state  — the page's like state, whenever it changes
 *   GET  /wait   — long poll; answers {"cmd": "toggle"} when the Flipper asks
 */

const BRIDGE = "http://127.0.0.1:8765";
const RETRY_MS = 2000;

/* Service workers are stopped when idle, so the poll loop must be restartable
 * and must never keep a stale flag saying it is already running. */
let polling = false;

async function postState(payload) {
  try {
    await fetch(`${BRIDGE}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    /* bridge not running — the next heartbeat tries again */
  }
}

/* Exactly one tab gets the toggle: two YouTube Music tabs both clicking would
 * like and then unlike in the same breath, so the state would never change.
 *
 * Tabs are tried in order of likely relevance (audible first, then the active
 * one) and the first one that accepts the message wins — a tab without the
 * content script rejects it, which is how the right tab is found without
 * trusting a URL match. Nothing waits on a rejected promise, because an
 * unsettled `sendMessage` inside the poll loop used to stall every later
 * command. */
function forwardToggle() {
  chrome.tabs.query({}).then((tabs) => {
    const ranked = [...tabs].sort((a, b) => {
      const score = (tab) => (tab.audible ? 2 : 0) + (tab.active ? 1 : 0);
      return score(b) - score(a);
    });
    const tryNext = (index) => {
      if (index >= ranked.length) return;
      const tab = ranked[index];
      let sent;
      try {
        sent = chrome.tabs.sendMessage(tab.id, { type: "toggle" });
      } catch (error) {
        tryNext(index + 1);
        return;
      }
      if (sent && typeof sent.then === "function") {
        sent.then(() => {}).catch(() => tryNext(index + 1));
      }
    };
    tryNext(0);
  }).catch(() => {});
}

async function pollLoop() {
  if (polling) return;
  polling = true;
  while (true) {
    try {
      // A hung request must not stop the loop for good: abort it and retry.
      const controller = new AbortController();
      const abort = setTimeout(() => controller.abort(), 25000);
      const response = await fetch(`${BRIDGE}/wait`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await response.json();
      clearTimeout(abort);
      if (data && data.cmd === "toggle") forwardToggle();
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
    }
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "state") return;
  postState(message.payload);
  pollLoop();
});

chrome.runtime.onStartup.addListener(pollLoop);
chrome.runtime.onInstalled.addListener(pollLoop);
pollLoop();
