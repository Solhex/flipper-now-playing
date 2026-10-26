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

/* Sent to every tab rather than filtered by URL: only tabs running the content
 * script have the listener, and a URL pattern would miss a page whose URL differs
 * from the shipped match (a test page, a redirect, another host).
 *
 * Nothing here is awaited: `tabs.sendMessage` to a tab with no listener can leave
 * its promise unsettled, and awaiting that inside the poll loop stopped every
 * later command from ever being delivered. */
function forwardToggle() {
  chrome.tabs.query({}).then((tabs) => {
    for (const tab of tabs) {
      try {
        const sent = chrome.tabs.sendMessage(tab.id, { type: "toggle" });
        if (sent && typeof sent.catch === "function") sent.catch(() => {});
      } catch (error) {
        /* tab went away mid-send */
      }
    }
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
