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

/* Two extension installs (say Firefox and Brave) both poll the one bridge, and a
 * command is only delivered to whichever asks first. The id makes that visible in
 * the bridge log instead of a mystery. Sent as a query parameter, not a header,
 * so no CORS preflight is involved. */
const WORKER = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2))
  .slice(0, 6);
const BROWSER_NAME = /firefox/i.test(navigator.userAgent) ? "firefox" : "chromium";
const WHO = `worker=${WORKER}&browser=${BROWSER_NAME}`;
const RETRY_MS = 2000;

/* Service workers are stopped when idle, so the poll loop must be restartable
 * and must never keep a stale flag saying it is already running. */
let polling = false;

async function postState(payload) {
  try {
    await fetch(`${BRIDGE}/state?${WHO}`, {
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
/* Tabs opened before the extension was reloaded have no content script, so the
 * worker puts it there itself: one injection per tab, and content.js refuses to
 * run twice. */
const injectedTabs = new Set();

/* Which tab each reporting client lives in. The bridge cannot press a key at a
 * background tab (the keystroke would go to whatever tab is in front), so on
 * request the extension brings the YouTube Music tab forward, holds it there long
 * enough for the bridge's Enter, and puts the previous tab back. */
const clientTabs = new Map();
let restoreTab = null;

function rememberTab(client, sender) {
  if (!client || !sender || !sender.tab) return;
  clientTabs.set(client, { tabId: sender.tab.id, windowId: sender.tab.windowId });
}

async function activateClient(client) {
  const info = clientTabs.get(client) || clientTabs.values().next().value;
  if (!info) return false;
  try {
    const [active] = await chrome.tabs.query({ active: true, windowId: info.windowId });
    if (active && active.id !== info.tabId) {
      restoreTab = { tabId: active.id, windowId: info.windowId };
    }
    await chrome.tabs.update(info.tabId, { active: true });
    await chrome.windows.update(info.windowId, { focused: true });
    return true;
  } catch (error) {
    return false;
  }
}

async function restorePreviousTab() {
  const saved = restoreTab;
  restoreTab = null;
  if (!saved) return false;
  try {
    await chrome.tabs.update(saved.tabId, { active: true });
    return true;
  } catch (error) {
    return false;
  }
}

async function ensureScript(tabId) {
  if (injectedTabs.has(tabId)) return false;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    injectedTabs.add(tabId);
    return true;
  } catch (error) {
    return false;      // closed tab, or a page we are not allowed to touch
  }
}

async function sweepTabs() {
  try {
    const tabs = await chrome.tabs.query({ url: ["https://music.youtube.com/*"] });
    for (const tab of tabs) {
      if (!tab.discarded) ensureScript(tab.id);
    }
  } catch (error) { /* no tabs permission for a URL match: ignore */ }
}

function forwardToggle(title, client) {
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
        sent = chrome.tabs.sendMessage(tab.id, { type: "toggle", title, client });
      } catch (error) {
        tryNext(index + 1);
        return;
      }
      if (sent && typeof sent.then === "function") {
        sent.then(() => {}).catch(async () => {
          // No listener in this tab: it probably predates the extension reload.
          if (await ensureScript(tab.id)) {
            try {
              await chrome.tabs.sendMessage(tab.id, { type: "toggle", title, client });
              return;
            } catch (error) { /* fall through to the next tab */ }
          }
          tryNext(index + 1);
        });
      }
    };
    tryNext(0);
  }).catch(() => {});
}

async function pollLoop() {
  if (polling) return;
  polling = true;
  sweepTabs();
  while (true) {
    try {
      // A hung request must not stop the loop for good: abort it and retry.
      const controller = new AbortController();
      const abort = setTimeout(() => controller.abort(), 25000);
      const response = await fetch(`${BRIDGE}/wait?${WHO}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await response.json();
      clearTimeout(abort);
      if (data && data.cmd === "toggle") forwardToggle(data.title, data.client);
      else if (data && data.cmd === "activate") await activateClient(data.client);
      else if (data && data.cmd === "restore") await restorePreviousTab();
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
    }
  }
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message || message.type !== "state") return;
  rememberTab(message.payload && message.payload.client, sender);
  postState(message.payload);
  pollLoop();
});

chrome.runtime.onStartup.addListener(pollLoop);
chrome.runtime.onInstalled.addListener(pollLoop);

/* Chromium stops an idle service worker after ~30 s, and the long poll alone does
 * not reliably keep it alive — Brave in particular suspends it aggressively. The
 * alarm wakes the worker so the poll restarts; without it the bridge queues like
 * commands that nobody ever picks up. */
chrome.alarms.create("keep-polling", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => pollLoop());

pollLoop();
