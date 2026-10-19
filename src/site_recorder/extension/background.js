// src/extension/background.js

import { SecretStore, redactSecrets, scrubLayoutSnapshot } from "./redact.js";

// Build stamp — bump on every change. Surfaced via get_status (popup shows it)
// and the boot debug log, so we can tell at a glance whether Chrome is running
// THIS code or a stale cached service worker.
const SW_BUILD = "2026-10-19a-capture-upgrades";

// CRITICAL: take over immediately when a new worker version loads. Extension
// service workers have install→waiting→active phases just like web SWs; without
// skipWaiting() a relaunch (even with a version bump via --load-extension) keeps
// running the PREVIOUS worker's code for the whole session — so new background.js
// changes silently don't take effect until the run AFTER. This forces the fresh
// worker to activate and claim all clients right away.
self.addEventListener("install", () => {
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

const NATIVE_HOST = 'com.sitebench.collector';
// The SITE-Bench dashboard is reachable at BOTH localhost:8501 and
// 127.0.0.1:8501 (Dash binds to 127.0.0.1; __main__.py opens localhost).
// Suppress everything from either spelling so the recorder never records
// itself.
const DASHBOARD_ORIGINS = [
  'http://localhost:8501',
  'http://127.0.0.1:8501',
];
let port = null;
let isRecording = false; // Handshake flag
let isPaused = false;    // User pause (privacy) — independent of isRecording
let currentSessionId = null; // session named by the host's "connected" message
// Response bodies above these are dropped (the record keeps its headers and
// is marked bodyOmitted): Chrome caps one native message at 64 MiB of UTF-8
// JSON, and an oversized message kills the port until the next reconnect.
const MAX_BASE64_CHARS = 56 * 1024 * 1024;
const MAX_TEXT_BYTES = 48 * 1024 * 1024;
const tabUrls = {}; // Track root URLs for deterministic grouping

// --- Privacy ------------------------------------------------------------------
// Sites never recorded at all (no screenshots, DOM, events or network): the
// web vaults of password managers by default; the recorder adds the
// participant's own list (config "skip_domains") in the handshake.
const DEFAULT_SKIP_DOMAINS = ["1password.com", "bitwarden.com", "lastpass.com",
  "keepersecurity.com", "dashlane.com", "nordpass.com"];
let skipDomains = DEFAULT_SKIP_DOMAINS.slice();
const skipNoticeSent = new Set();   // "tabId|host" already reported this session

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
}

function isSkippedUrl(url) {
  const h = hostOf(url);
  if (!h) return false;
  return skipDomains.some((d) => h === d || h.endsWith("." + d));
}

// What the participant typed into sensitive fields, in memory only (never
// sent to the host): scrubbed out of request bodies, URLs, sent WebSocket
// frames and layout snapshots. See redact.js.
const secrets = new SecretStore();

// Per tab, per frame: where that frame's sensitive fields are on screen (top
// viewport CSS px). Every screenshot of the tab blanks all of them.
const sensitiveRects = new Map();   // tabId -> Map(frameId -> {rects, unlocated})

function rectsForShot(tabId, frameId, request) {
  const rects = [];
  let unlocated = !!request.sensitive_unlocated;
  const perFrame = sensitiveRects.get(tabId);
  if (perFrame) {
    for (const [fid, st] of perFrame) {
      if (fid === frameId) continue;          // the sender's own boxes come fresh with the event
      rects.push(...(st.rects || []));
      if (st.unlocated) unlocated = true;
    }
  }
  if (Array.isArray(request.sensitive_rects)) rects.push(...request.sensitive_rects);
  return { rects, unlocated };
}

// Toolbar icon sets, swapped to reflect recording/pause state (updateActionIcon).
const ACTIVE_ICONS = { 16: "icon_16.png", 32: "icon_32.png", 48: "icon_48.png", 128: "icon_128.png" };
const PAUSED_ICONS = { 16: "icon_paused_16.png", 32: "icon_paused_32.png", 48: "icon_paused_48.png", 128: "icon_paused_128.png" };
const IDLE_ICONS   = { 16: "icon_idle_16.png", 32: "icon_idle_32.png", 48: "icon_idle_48.png", 128: "icon_idle_128.png" };

// Diagnostic logging: console + (when connected) forward to the Python host so
// it lands in extension_host.log, which IS captured per session. Lets us debug
// the service-worker lifecycle from the recording artifacts after a rerun.
function debugLog(msg) {
  try { console.log("[SITE]", msg); } catch (e) {}
  try { if (port) port.postMessage({ type: "debug_log", msg: String(msg), ts: Date.now() / 1000.0 }); } catch (e) {}
}

// Restore pause / session / banner state across MV3 service-worker restarts.
chrome.storage.session.get({ isPaused: false, sessionId: null, debuggerCanceledByUser: false }).then((s) => {
  isPaused = !!s.isPaused;
  currentSessionId = s.sessionId || null;
  debuggerCanceledByUser = !!s.debuggerCanceledByUser;
  updateActionIcon();
}).catch(() => {});

function isDashboardUrl(url) {
  if (!url) return false;
  for (const origin of DASHBOARD_ORIGINS) {
    if (url === origin
        || url.startsWith(origin + '/')
        || url.startsWith(origin + '?')
        || url.startsWith(origin + '#')) {
      return true;
    }
  }
  return false;
}

function connectToNativeHost() {
  console.log("[SITE] connectNative attempt (" + NATIVE_HOST + ")");
  port = chrome.runtime.connectNative(NATIVE_HOST);
  port.onMessage.addListener((msg) => {
    if (msg.type === "keepalive") {
      // Host pings us ~every 20s; receiving it resets the MV3 idle timer and
      // keeps the service worker (and this native port) alive for the whole
      // recording. No work to do beyond having woken up.
      return;
    }
    if (msg.type === "set_paused") {
      // App-driven pause/resume: the recorder app writes control.json into
      // the session dir; the NM host relays it here. Same code path as the
      // popup's toggle, so markers + icon + storage stay consistent.
      debugLog("host set_paused -> " + !!msg.paused);
      setPaused(!!msg.paused);
      return;
    }
    if (msg.type === "stop") {
      // End of session. Stop capturing and detach NOW, then acknowledge with
      // "stopped" and leave the port open: the host handles everything queued
      // before the ack (the port is FIFO), flushes and exits, and its exit
      // fires our onDisconnect -> teardownSession. Disconnecting from this
      // side instead would drop queued messages, and the browser kills the
      // host ~2s later, possibly mid-write. Fallback if the host never
      // closes: tear down ourselves after 10s. (Chrome gotcha: our OWN
      // port.disconnect() does not fire our onDisconnect — teardownSession
      // does the cleanup explicitly.)
      const p = port;
      if (isRecording) {
        // Responses still waiting for loadingFinished go out headers-only.
        flushPendingResponses("session_end");
        stopCapture("host requested stop");          // a real end of session
      } else {
        detachAll();   // a refused connection (no "connected" yet): keep any pause state
      }
      try { if (p) p.postMessage({ type: "stopped" }); } catch (e) {}
      setTimeout(() => { if (port === p) teardownSession("host did not close after stop"); }, 10000);
      return;
    }
    if (msg.type === "connected") {
      isRecording = true;
      // The host names the session and its pause state. A RECONNECT within the
      // same session (service-worker restart, host restart) must keep the
      // participant's pause — un-pausing silently would record what they
      // chose to hide — and keep a dismissed debugger banner dismissed.
      const sid = msg.session_id || null;
      const sameSession = !!sid && sid === currentSessionId;
      currentSessionId = sid;
      if (Array.isArray(msg.skip_domains)) {
        skipDomains = DEFAULT_SKIP_DOMAINS.concat(
          msg.skip_domains.map((d) => String(d).trim().toLowerCase().replace(/^\*?\./, "")).filter(Boolean));
      }
      if (!sameSession) {
        skipNoticeSent.clear();
        // New session: re-arm debugger attaching even if the user dismissed
        // the banner last session (one banner per session; dismissing sticks).
        debuggerCanceledByUser = false;
      }
      const localPaused = isPaused;
      // Same session: a pause on EITHER side wins (the host may have missed a
      // popup pause that happened while the port was dropping). New session:
      // the host's state (normally un-paused).
      isPaused = sameSession ? (localPaused || !!msg.paused) : !!msg.paused;
      chrome.storage.session.set({ isPaused, sessionId: sid,
                                   debuggerCanceledByUser }).catch(() => {});
      if (sameSession && localPaused && !msg.paused) {
        // Tell the host so status.json (and the app) show the pause too.
        try { port.postMessage({ type: "recording_paused", timestamp: Date.now() / 1000.0 }); } catch (e) {}
      }
      debugLog("handshake connected -> recording mode (session " + sid +
               (sameSession ? ", reconnect" : "") + ", paused=" + isPaused + ")");
      updateActionIcon();
      // Now safe to attach to any existing tabs
      chrome.tabs.query({}, (tabs) => {
        for (let tab of tabs) {
          attachDebuggerIfValid(tab);
          // Attach-mode gap: declarative content_scripts only inject on
          // navigation, so tabs that were open BEFORE this session (or before
          // an extension reload) have no live content script — and clicks,
          // inputs, scrolls AND the screenshots they trigger all originate
          // there. Inject programmatically so pre-open tabs record too
          // (content.js/main_world.js are guarded against double-injection).
          injectContentScripts(tab);
        }
      });
      // Seed the active-tab timeline with whatever tab is already focused.
      // Without this, a session where the user never switches tabs would
      // produce an empty timeline and the grouper would discard everything.
      chrome.tabs.query({ active: true }, (tabs) => {
        for (let tab of tabs) {
          emitTabActivated(tab.id);
        }
      });
    } else {
      console.log("Received from native:", msg);
    }
  });
  port.onDisconnect.addListener(() => {
    // The HOST side went away (host exited, was killed, or never existed
    // because no session manifest is installed). NOTE: this does NOT fire
    // when WE call port.disconnect() — self-initiated stops go through
    // teardownSession() directly from the stop handler.
    const why = (chrome.runtime.lastError && chrome.runtime.lastError.message) || "port closed";
    teardownSession("native host disconnected -> " + why);
  });
}

// Stop capturing and detach (end of session). The pause state is cleared
// only here — a merely DROPPED port (teardownSession without a stop) keeps
// the participant's pause for the reconnect.
function stopCapture(reason) {
  console.log("[SITE] stop capture:", reason);
  isRecording = false;
  isPaused = false;
  currentSessionId = null;
  chrome.storage.session.set({ isPaused: false, sessionId: null }).catch(() => {});
  detachAll();
  updateActionIcon();
}

function detachAll() {
  chrome.tabs.query({}, (tabs) => {
    for (let tab of tabs) {
      chrome.debugger.detach({ tabId: tab.id }).catch(() => {});
    }
  });
}

// Full local teardown of a connection — callable from BOTH end paths
// (host-requested stop, host-side disconnect). Idempotent.
function teardownSession(reason) {
  const wasRecording = isRecording;
  console.log("[SITE] teardown:", reason, "(wasRecording=" + wasRecording + ")");
  try { if (port) port.disconnect(); } catch (e) {}
  port = null;
  isRecording = false;
  updateActionIcon();
  pdfTabs.clear(); ensurePdfPoll();   // stop the PDF scroll poller
  pendingResponses.clear();
  chrome.runtime.sendMessage({ type: "recording_stopped" }).catch(() => {});
  // Detach debuggers from all tabs so the security infobar disappears during
  // the user's normal, everyday browsing.
  detachAll();
  // Fast-path reconnect probes: a new session often begins seconds after a
  // port drop — a stop→re-record, or the recorder clearing a stale host so
  // it can claim the extension. Session teardown deletes the NM manifest
  // BEFORE signalling stop, so probing right after a real stop fails
  // instantly and silently (no session to rejoin); when a new manifest IS
  // there, this connects within ~2s instead of waiting for the 30s alarm
  // or the next tab event. Gated on wasRecording: a FAILED probe also lands
  // here (as a host disconnect), and rescheduling from those would loop
  // forever — only the end of a real, handshaken session earns the burst.
  if (wasRecording) {
    for (const delayMs of [2000, 6000, 15000]) {
      setTimeout(() => ensureConnected(), delayMs);
    }
  }
}

// Idle reconnect: keep trying to connect while disconnected, so a NEW recording
// session started against an ALREADY-RUNNING Chrome connects WITHOUT a browser
// relaunch (attach mode). connectNative fails instantly + silently when no
// session manifest exists, so this costs nothing while not recording.
let lastConnectAttempt = 0;
function ensureConnected() {
  if (port) return;                                  // connected, or a connect is in flight
  const now = Date.now();
  if (now - lastConnectAttempt < 1500) return;       // throttle idle retries
  lastConnectAttempt = now;
  connectToNativeHost();
}

// Attempt connection on boot, then keep an alarm-backed retry so an evicted MV3
// worker still reconnects when a session starts (alarms are a wake source; 30s
// is Chrome's floor). The event listeners below give the fast path (<1s) when
// the app nudges Chrome (window focus) right after writing the manifest.
ensureConnected();
try {
  chrome.alarms.create("sb-reconnect", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((a) => { if (a.name === "sb-reconnect") ensureConnected(); });
} catch (e) { /* alarms unavailable — event listeners still cover the live case */ }
debugLog("service worker booted (build " + SW_BUILD + "); native port opening");

// Helper to send data to the Python daemon. Capture is GATED by !isPaused so
// that pausing stops screenshots, WARC/network, and input events at the source.
function sendToPython(data) {
  if (port && isRecording && !isPaused) {
    try {
      port.postMessage(data);
    } catch (e) {
      // Too large for one native message (Chrome caps it at 64 MiB): keep
      // the record, drop the body.
      if (data && data.type === "network_response" && data.body) {
        const slim = Object.assign({}, data, { bodyOmitted: "too_large" });
        delete slim.body; delete slim.base64Encoded;
        try { port.postMessage(slim); } catch (e2) {}
      }
    }
  }
}

function capturing() {
  return !!(port && isRecording && !isPaused);
}

// Size check against Chrome's 64 MiB native-message cap (UTF-8 JSON).
function bodyTooLarge(body, base64) {
  if (!body) return false;
  if (base64) return body.length > MAX_BASE64_CHARS;          // ASCII: 1 byte per char
  if (body.length <= MAX_TEXT_BYTES / 3) return false;        // fits even at 3 bytes/char
  try { return new TextEncoder().encode(body).length > MAX_TEXT_BYTES; } catch (e) { return true; }
}

// Control messages (pause/resume markers) must reach Python even while paused,
// so they bypass the capture gate above.
function sendControlToPython(data) {
  if (port && isRecording) {
    try { port.postMessage(data); } catch (e) { /* port closing */ }
  }
}

// Toggle the privacy pause: persist it, mark the gap for Python, swap the icon.
function setPaused(paused) {
  isPaused = !!paused;
  chrome.storage.session.set({ isPaused }).catch(() => {});
  sendControlToPython({
    type: isPaused ? "recording_paused" : "recording_resumed",
    timestamp: Date.now() / 1000.0,
  });
  updateActionIcon();
}

// Reflect connection + pause state on the toolbar icon, badge, and tooltip.
// Three distinct states: not recording (grey dot) / recording (red dot) /
// paused (bars). Previously "not recording" wrongly showed the red icon.
function updateActionIcon() {
  let icons, badge, title;
  if (!isRecording) {
    icons = IDLE_ICONS; badge = ""; title = "SITE Collector — not recording";
  } else if (isPaused) {
    icons = PAUSED_ICONS; badge = "❚❚"; title = "SITE Collector — PAUSED (click to resume)";
  } else {
    icons = ACTIVE_ICONS; badge = ""; title = "SITE Collector — recording (click to pause)";
  }
  chrome.action.setIcon({ path: icons }).catch(() => {});
  chrome.action.setBadgeText({ text: badge }).catch(() => {});
  if (badge) chrome.action.setBadgeBackgroundColor({ color: "#6B7280" }).catch(() => {});
  chrome.action.setTitle({ title }).catch(() => {});
}

// The debugger infobar's ✕ is NOT a "hide" button — Chrome treats it as
// "cancel debugging": it detaches this extension from EVERY tab and fires
// onDetach(reason="canceled_by_user"). Without tracking that, the next
// navigation re-attached and brought the banner straight back — an endless
// whack-a-mole for the user. Once the user cancels, we STOP re-attaching for
// the REST OF THE SESSION: screenshots keep flowing via the captureVisibleTab
// fallback; CDP network capture (WARC) stops, which we report to the host so
// the session log says so explicitly. The next session re-arms attaching (one
// banner per session, and dismissing it sticks).
let debuggerCanceledByUser = false;

chrome.debugger.onDetach.addListener((source, reason) => {
  if (reason !== "canceled_by_user") return;   // tab closed / navigated away etc.
  if (!debuggerCanceledByUser) {
    debuggerCanceledByUser = true;
    chrome.storage.session.set({ debuggerCanceledByUser: true }).catch(() => {});
    console.log("[SITE] user dismissed the debugger banner — CDP capture off for this session");
    debugLog("debugger canceled by user (banner ✕) — no re-attach until next session");
    // Control channel: must reach the host even while paused.
    sendControlToPython({
      type: "debugger_canceled",
      timestamp: Date.now() / 1000.0,
    });
  }
});

function attachDebuggerIfValid(tab) {
  if (!isRecording) return;
  if (debuggerCanceledByUser) return;   // the user closed the banner — honor it
  // A tab opened WITH a URL (link with target=_blank, window.open, ctrl+click)
  // reports its first "loading" update with an empty tab.url and the address
  // only in pendingUrl; checking tab.url alone skipped the attach and lost
  // that whole first page from the WARC.
  const url = tab.url || tab.pendingUrl || "";
  if (!url.startsWith('http')) return;
  if (isDashboardUrl(url)) return; // never attach to the recorder dashboard
  chrome.debugger.attach({ tabId: tab.id }, '1.3', () => {
    if (chrome.runtime.lastError) {
      // Expected if already attached
      return;
    }
    enableNetwork({ tabId: tab.id });
    // Cross-origin iframes (Stripe, YouTube embeds, OAuth popups-in-frames)
    // and workers are SEPARATE targets: the tab's own Network domain never
    // sees their traffic. Auto-attach to them (flattened sessions) and enable
    // Network on each — see Target.attachedToTarget in the event router.
    autoAttach({ tabId: tab.id });
  });
}

function cdp(target, method, params, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(method + " timed out")), timeoutMs);
    chrome.debugger.sendCommand(target, method, params || {}, (res) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(res);
    });
  });
}

function enableNetwork(target) {
  // Bodies are fetched at loadingFinished, so the inspector must still hold
  // them then: raise the buffers (defaults evict big responses) up to what
  // one native message can carry. Request bodies up to 1 MB arrive inline;
  // bigger ones are fetched with getRequestPostData.
  cdp(target, "Network.enable", {
    maxTotalBufferSize: 200 * 1024 * 1024,
    maxResourceBufferSize: 48 * 1024 * 1024,
    maxPostDataSize: 1024 * 1024,
  }).catch(() => {});
  cdp(target, "Network.setCacheDisabled", { cacheDisabled: true }).catch(() => {});
}

function autoAttach(target) {
  cdp(target, "Target.setAutoAttach",
      { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }).catch(() => {});
}

// Programmatic injection for tabs opened BEFORE the session/extension load —
// attach mode's counterpart to the manifest's declarative content_scripts
// (which only fire on navigation). Both scripts are idempotent: content.js
// guards on window.__siteBenchContentLoaded (isolated world), main_world.js
// on window.__siteBenchMainHooked (page world), so a tab that already has
// them live is a no-op.
function injectContentScripts(tab) {
  if (!tab.id || !tab.url || !tab.url.startsWith('http')) return;
  if (isDashboardUrl(tab.url)) return;
  chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    files: ["content.js"],
  }).catch(() => {});   // chrome:// subframes, sandboxed frames — fine to skip
  chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: true },
    world: "MAIN",
    files: ["main_world.js"],
  }).catch(() => {});
}

// Track tab URLs and attach debuggers on navigation
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (tab.url) {
    tabUrls[tabId] = tab.url;
  }

  if (changeInfo.status === 'loading') {
    attachDebuggerIfValid(tab);
  }
});

// Update tabUrls *before* the network request for the new page fires.
// `chrome.tabs.onUpdated` only fires once Chrome has committed the new
// URL into the tab record, which lags behind CDP `Network.requestWillBeSent`
// for the top-level navigation request — causing that request to be tagged
// with the *previous* tab's URL. `webNavigation.onBeforeNavigate` fires
// before the request leaves the browser, so writing into tabUrls here
// ensures the very first request of a navigation is tagged correctly.
chrome.webNavigation.onBeforeNavigate.addListener((details) => {
  if (details.frameId !== 0) return; // main frame only
  if (isDashboardUrl(details.url)) return;
  tabUrls[details.tabId] = details.url;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  delete tabUrls[tabId];
  markPdfTab(tabId, false);
  sensitiveRects.delete(tabId);
  lastLayoutAt.delete(tabId);
});

// Page zoom changes what a CSS pixel is in the screenshots.
chrome.tabs.onZoomChange.addListener((info) => {
  if (!capturing()) return;
  sendToPython({ type: "zoom_change", tabId: info.tabId, zoom: info.newZoomFactor,
                 old_zoom: info.oldZoomFactor, timestamp: Date.now() / 1000.0 });
});

// ===== PDF scroll capture (CDP screenshot diff) =====
// Content scripts do NOT run in Chrome's built-in PDF viewer, so scrolling a PDF
// (e.g. an arxiv paper, served as application/pdf with no `.pdf` extension) emits
// NO scroll events and the trace shows "0 scrolls". We detect PDF tabs by their
// `application/pdf` *document* response, then poll a screenshot of the active PDF
// tab; when the image changes (the user scrolled or zoomed) we emit a scroll
// snapshot — so PDF reading lands in the trace, captions, and video like any
// other scroll. Pause-gated and active-tab-only, so it's cheap.
const pdfTabs = new Set();
let pdfPollTimer = null;
let lastPdfSig = "";

function _shotSig(img) {
  // PNG bytes shift wholesale on any visual change, so length + head/tail tells
  // "the view moved" from "static" without hashing the whole frame.
  if (!img) return "";
  return img.length + "|" + img.slice(0, 80) + "|" + img.slice(-80);
}

function ensurePdfPoll() {
  const need = isRecording && pdfTabs.size > 0;
  if (need && !pdfPollTimer) {
    pdfPollTimer = setInterval(pollPdfScroll, 1800);
  } else if (!need && pdfPollTimer) {
    clearInterval(pdfPollTimer);
    pdfPollTimer = null;
    lastPdfSig = "";
  }
}

function markPdfTab(tabId, isPdf) {
  if (tabId == null) return;
  const before = pdfTabs.has(tabId);
  if (isPdf) pdfTabs.add(tabId); else pdfTabs.delete(tabId);
  if (before !== pdfTabs.has(tabId)) { lastPdfSig = ""; ensurePdfPoll(); }
}

async function pollPdfScroll() {
  if (!isRecording || isPaused) return;
  let tab;
  try {
    const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    tab = tabs && tabs[0];
  } catch (e) { return; }
  if (!tab || !pdfTabs.has(tab.id) || isDashboardUrl(tab.url)) return;
  let img;
  try { img = await captureScreenshotPNG(tab.id, tab.windowId); } catch (e) { return; }
  if (!img) return;
  const sig = _shotSig(img);
  if (sig === lastPdfSig) return;     // view unchanged -> not scrolling
  const first = lastPdfSig === "";
  lastPdfSig = sig;
  if (first) return;                  // skip the initial view (not a scroll)
  sendToPython({
    type: "browser_snapshot",
    timestamp: Date.now() / 1000.0,
    event_type: "scroll_post_action",
    details: { scrollSource: "pdf_cdp" },
    image_data: img,
    url: tab.url
  });
}

// Active-tab timeline — the source of truth for "which tabs the user
// actually used". The grouper consumes `tab_timeline.jsonl` to discard
// WARC traffic from background tabs that the user never focused.
//
// We emit a `tab_activated` whenever (a) the user switches tabs, (b) a
// new tab is created already-active, (c) the OS window regains focus and
// reveals an active tab, or (d) the active tab navigates to a new URL.
// Cases (a)–(c) are necessary; (d) keeps the timeline's URL accurate so
// a tab that the user opened on docs.google.com and then navigated to
// chatgpt.com produces two timeline entries, not one.
function emitTabActivated(tabId) {
  if (!isRecording) return;
  chrome.tabs.get(tabId, (tab) => {
    if (chrome.runtime.lastError || !tab) return;
    if (isDashboardUrl(tab.url)) return;
    const ts = Date.now() / 1000.0;
    // A skipped (privacy) site is reported by origin only, without its title.
    const skipped = isSkippedUrl(tab.url);
    let url = tab.url || "";
    if (skipped) { try { url = new URL(url).origin + "/"; } catch (e) { url = ""; } }
    sendToPython({
      type: "tab_activated",
      tabId,
      windowId: tab.windowId,
      url,
      title: skipped ? "" : (tab.title || ""),
      privacy_skip: skipped || undefined,
      timestamp: ts
    });
    // file:// tabs can't be debugger-attached and emit no WARC traffic,
    // but the user did view them. Tell the host so it can copy the
    // underlying file into the session as a first-class "site".
    if (tab.url && tab.url.startsWith("file://")) {
      sendToPython({
        type: "local_file_active",
        tabId,
        windowId: tab.windowId,
        url: tab.url,
        timestamp: ts
      });
    }
  });
}

chrome.tabs.onActivated.addListener(({ tabId }) => {
  ensureConnected();   // attach-mode fast path: reconnect when idle
  emitTabActivated(tabId);
  lastPdfSig = "";   // re-baseline the PDF scroll poller for the newly-active tab
});

chrome.tabs.onCreated.addListener((tab) => {
  ensureConnected();
  // Attach as early as possible: a tab created with a URL starts loading it
  // immediately (see attachDebuggerIfValid's pendingUrl note).
  if (tab) attachDebuggerIfValid(tab);
  if (tab && tab.active) emitTabActivated(tab.id);
});

// The participant closed the last recording window. On macOS the browser
// keeps running without windows, so the recorder cannot see it exit — tell
// it explicitly (it treats this like closing the browser: end of session).
chrome.windows.onRemoved.addListener(() => {
  if (!isRecording) return;
  chrome.windows.getAll({}, (wins) => {
    if (chrome.runtime.lastError) return;
    if (!wins || wins.length === 0) {
      sendControlToPython({ type: "all_windows_closed", timestamp: Date.now() / 1000.0 });
    }
  });
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  ensureConnected();   // the app's "activate Chrome" nudge lands here → connect
  if (!isRecording) return;
  const ts = Date.now() / 1000.0;
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    sendToPython({ type: "window_focus", windowId, focused: false, timestamp: ts });
    return;
  }
  sendToPython({ type: "window_focus", windowId, focused: true, timestamp: ts });
  chrome.tabs.query({ active: true, windowId }, (tabs) => {
    if (!tabs || !tabs.length) return;
    emitTabActivated(tabs[0].id);
  });
});

// In-tab navigation while the tab is the active one — refresh the
// timeline so the grouper sees the new URL. `onCommitted` fires after
// the navigation actually lands, which is what we want for the timeline
// (vs. `onBeforeNavigate`, which we already use above to keep `tabUrls`
// in sync for site_url tagging on the very first request).
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) ensureConnected();
  // A navigated frame's old sensitive boxes are gone (a main-frame
  // navigation replaces every frame).
  const perFrame = sensitiveRects.get(details.tabId);
  if (perFrame) {
    if (details.frameId === 0) sensitiveRects.delete(details.tabId);
    else perFrame.delete(details.frameId);
  }
  if (details.frameId !== 0) return;
  if (isDashboardUrl(details.url)) return;
  if (isSkippedUrl(details.url) && capturing()) {
    // Say THAT a skipped site was visited (host only), so the gap in the
    // recording is explained.
    const h = hostOf(details.url);
    const k = details.tabId + "|" + h;
    if (!skipNoticeSent.has(k)) {
      skipNoticeSent.add(k);
      sendToPython({ type: "privacy_skip", host: h, tabId: details.tabId, timestamp: Date.now() / 1000.0 });
    }
  }
  chrome.tabs.get(details.tabId, (tab) => {
    if (chrome.runtime.lastError || !tab || !tab.active) return;
    emitTabActivated(details.tabId);
  });
});

// Case-insensitive header lookup. CDP gives headers as a plain object
// but server casing is unpredictable (Content-Type vs content-type).
function pickHeader(headers, name) {
  if (!headers || typeof headers !== "object") return null;
  const want = String(name).toLowerCase();
  for (const k of Object.keys(headers)) {
    if (k.toLowerCase() === want) return headers[k];
  }
  return null;
}

// Decode the CDP body (possibly base64) just enough to peek at the magic
// bytes, without inflating large bodies if we don't have to.
function decodeBodyForCheck(body, base64Encoded) {
  if (!body) return null;
  try {
    if (base64Encoded) {
      // atob -> bytes -> string of first 8 chars is enough to spot %PDF.
      const head = atob(body.slice(0, 12));
      return head;
    }
    return body.slice(0, 12);
  } catch (e) {
    return null;
  }
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// Re-fetch a PDF that CDP couldn't capture properly. Uses the service
// worker's fetch with credentials so cookies (e.g. an authenticated
// scholar.google session) and any Cloudflare bot-check tokens flow
// through. Returns a base64 string of the response bytes if it really is
// a PDF, otherwise null so callers keep the (broken) CDP body rather
// than overwriting it with junk.
async function fetchAsBase64Pdf(url) {
  try {
    const r = await fetch(url, { credentials: "include", redirect: "follow" });
    if (!r.ok) return null;
    const ab = await r.arrayBuffer();
    if (ab.byteLength === 0) return null;
    const head = new Uint8Array(ab.slice(0, 4));
    if (head[0] !== 0x25 || head[1] !== 0x50 || head[2] !== 0x44 || head[3] !== 0x46) {
      return null;
    }
    return arrayBufferToBase64(ab);
  } catch (e) {
    return null;
  }
}

// Route debugger events to Python.
//
// Network records are built in two steps. requestWillBeSent goes out at once
// (with its body, redacted); the response is HELD from responseReceived until
// loadingFinished — the one moment its body is complete and still buffered —
// so large and streamed responses (chat apps, big JSON) are no longer
// captured empty or truncated. The host pairs request + response by key and
// writes them adjacently to the WARC.
//
// Events from auto-attached child targets (cross-origin iframes, workers)
// arrive with source.sessionId; every follow-up command goes to `source`, so
// it reaches the target that owns the request.
const pendingResponses = new Map();   // reqKey -> { payload, source, at }
const RESPONSE_HOLD_MS = 60000;       // longer = a stream that never finishes
const MAX_POST_CHARS = 8 * 1024 * 1024;

// Per-request promise chains: the CDP round trips for one request (post data,
// body) run in order, so the host always sees request -> response for it.
const requestChains = new Map();
function chainFor(key, fn) {
  const prev = requestChains.get(key) || Promise.resolve();
  const next = prev.then(fn, fn).catch((e) => debugLog("network handler: " + (e && e.message)));
  requestChains.set(key, next);
  next.then(() => { if (requestChains.get(key) === next) requestChains.delete(key); });
  return next;
}

function reqKey(source, requestId) {
  return source.tabId + "|" + (source.sessionId || "") + "|" + requestId;
}

function flushPendingResponses(reason) {
  for (const [key, p] of pendingResponses) {
    pendingResponses.delete(key);
    p.payload.bodyOmitted = reason;
    sendToPython(p.payload);
  }
}

setInterval(() => {
  const cutoff = Date.now() - RESPONSE_HOLD_MS;
  for (const [key, p] of pendingResponses) {
    if (p.at < cutoff) {
      pendingResponses.delete(key);
      p.payload.bodyOmitted = "unfinished";   // e.g. an SSE / long-poll stream
      sendToPython(p.payload);
    }
  }
}, 15000);

// Where a request came from: parser, script (top of the JS stack), preload…
// Lets downstream tie a request to the user action that caused it.
function trimInitiator(init) {
  if (!init || typeof init !== "object") return undefined;
  const out = { type: init.type };
  if (init.url) out.url = String(init.url).slice(0, 500);
  if (typeof init.lineNumber === "number") out.lineNumber = init.lineNumber;
  if (init.requestId) out.requestId = init.requestId;
  const frames = init.stack && init.stack.callFrames;
  if (Array.isArray(frames) && frames.length) {
    out.stack = frames.slice(0, 3).map((f) => ({
      url: String(f.url || "").slice(0, 300), fn: f.functionName || "", line: f.lineNumber,
    }));
  }
  return out;
}

function slimResponse(r) {
  if (!r) return undefined;
  return { url: r.url, status: r.status, statusText: r.statusText, headers: r.headers,
           mimeType: r.mimeType, protocol: r.protocol };
}

// postDataEntries (base64 chunks) -> text when it is UTF-8, else base64.
function decodePostEntries(entries) {
  try {
    const parts = entries.map((e) => (e && e.bytes) ? Uint8Array.from(atob(e.bytes), (c) => c.charCodeAt(0)) : new Uint8Array(0));
    const total = parts.reduce((n, p) => n + p.length, 0);
    const all = new Uint8Array(total);
    let o = 0;
    for (const p of parts) { all.set(p, o); o += p.length; }
    try {
      return { text: new TextDecoder("utf-8", { fatal: true }).decode(all) };
    } catch (e) {
      return { base64: arrayBufferToBase64(all.buffer) };
    }
  } catch (e) {
    return null;
  }
}

async function handleRequestWillBeSent(source, params, siteUrl) {
  const request = Object.assign({}, params.request);
  let postText = null, postBase64 = null;
  if (Array.isArray(request.postDataEntries) && request.postDataEntries.length) {
    const dec = decodePostEntries(request.postDataEntries);
    if (dec) { postText = dec.text != null ? dec.text : null; postBase64 = dec.base64 || null; }
  }
  if (postText == null && postBase64 == null && typeof request.postData === "string") postText = request.postData;
  if (postText == null && postBase64 == null && request.hasPostData) {
    try {
      const r = await cdp(source, "Network.getRequestPostData", { requestId: params.requestId }, 5000);
      if (r && typeof r.postData === "string") postText = r.postData;
    } catch (e) { /* body no longer available (or a streamed upload) */ }
  }
  delete request.postDataEntries;
  delete request.postData;
  const sec = secrets.values();
  request.url = redactSecrets(request.url, sec);
  const msg = {
    type: "network_request",
    requestId: params.requestId,
    request,
    timestamp: params.timestamp,
    wallTime: params.wallTime,
    typeStr: params.type,
    tabId: source.tabId,
    sessionId: source.sessionId,
    frameId: params.frameId,
    loaderId: params.loaderId,
    documentURL: params.documentURL,
    initiator: trimInitiator(params.initiator),
    site_url: siteUrl,
  };
  if (postText != null) {
    if (postText.length > MAX_POST_CHARS) msg.postDataOmitted = "too_large";
    else msg.postData = redactSecrets(postText, sec);
  } else if (postBase64 != null) {
    // Binary (multipart file upload, protobuf): kept as bytes, not scrubbable.
    if (postBase64.length > MAX_POST_CHARS) msg.postDataOmitted = "too_large";
    else msg.postDataBase64 = postBase64;
  }
  if (params.redirectResponse) {
    // Same requestId, next hop: the host writes the previous hop's request
    // with this 3xx as its response (redirect chains used to vanish).
    msg.redirectResponse = slimResponse(params.redirectResponse);
  }
  sendToPython(msg);
}

async function fillResponseBody(p) {
  const payload = p.payload;
  try {
    const bodyResult = await cdp(p.source, "Network.getResponseBody", { requestId: payload.requestId });
    payload.body = bodyResult.body;
    payload.base64Encoded = bodyResult.base64Encoded;
  } catch (e) {
    // Expected for empty bodies, 204s, preflights, main-frame documents
    // (those come from the content script's DOM snapshot instead).
  }

  // PDF body fallback. Chrome's built-in PDF viewer pipeline pulls the
  // bytes out of band from CDP, so `Network.getResponseBody` either
  // returns an empty body or a bot-check HTML interstitial instead of
  // the PDF. Detect those cases by content-type, then re-fetch the URL
  // from the service worker (which has cross-origin + cookies via
  // host_permissions) and substitute the real bytes.
  const respUrl = payload.response && payload.response.url;
  const ctHeader = pickHeader(payload.response && payload.response.headers, "content-type") || "";
  const isPdfResponse = /application\/pdf/i.test(ctHeader) || /\.pdf(\?|$|#)/i.test(respUrl || "");
  if (isPdfResponse && respUrl && /^https?:/i.test(respUrl)) {
    const cdpBody = decodeBodyForCheck(payload.body, payload.base64Encoded);
    if (!(cdpBody && cdpBody.startsWith("%PDF"))) {
      const refetched = await fetchAsBase64Pdf(respUrl);
      if (refetched) {
        payload.body = refetched;
        payload.base64Encoded = true;
        payload.bodySource = "sw_fetch";
      }
    }
  }
  if (bodyTooLarge(payload.body, payload.base64Encoded)) {
    delete payload.body;
    delete payload.base64Encoded;
    payload.bodyOmitted = "too_large";
  }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  // Child targets: enable network capture on every auto-attached iframe /
  // worker (and auto-attach below nested iframes). Setup, not capture — it
  // runs even while paused so a resume finds them ready.
  if (method === "Target.attachedToTarget") {
    const child = { tabId: source.tabId, sessionId: params.sessionId };
    const t = params.targetInfo && params.targetInfo.type;
    if (t === "iframe" || t === "worker" || t === "shared_worker") {
      enableNetwork(child);
      if (t === "iframe") autoAttach(child);
    }
    if (params.waitingForDebugger) cdp(child, "Runtime.runIfWaitingForDebugger").catch(() => {});
    return;
  }

  let siteUrl = tabUrls[source.tabId] || "";
  const key = params && params.requestId ? reqKey(source, params.requestId) : "";

  // Belt-and-suspenders: we never attach the debugger to the dashboard tab,
  // but if a request URL or tab URL belongs to the dashboard we drop it
  // anyway so nothing about the recorder ever lands in the WARC.
  const reqUrl = params && params.request && params.request.url;
  const respUrl = params && params.response && params.response.url;
  // Capture eligibility is decided when the event ARRIVES: work started
  // during a pause (a body fetch, a PDF refetch) must not be written after
  // the participant resumes.
  if (isDashboardUrl(siteUrl) || isDashboardUrl(reqUrl) || isDashboardUrl(respUrl)
      || isSkippedUrl(siteUrl) || !capturing()) {
    if (key) pendingResponses.delete(key);
    return;
  }

  if (method === "Network.requestWillBeSent") {
    chainFor(key, () => handleRequestWillBeSent(source, params, siteUrl));
  } else if (method === "Network.responseReceived") {
    // Track PDF documents so the scroll poller can capture reading — content
    // scripts can't run in Chrome's PDF viewer. A Document response with an
    // application/pdf mime marks the tab; any other Document navigation clears it.
    if (params.type === "Document" && !source.sessionId) {
      const mt = ((params.response && params.response.mimeType) || "").toLowerCase();
      markPdfTab(source.tabId, mt.indexOf("application/pdf") !== -1);
    }
    chainFor(key, () => {
      pendingResponses.set(key, {
        source, at: Date.now(),
        payload: {
          type: "network_response",
          requestId: params.requestId,
          response: params.response,
          timestamp: params.timestamp,
          typeStr: params.type,
          tabId: source.tabId,
          sessionId: source.sessionId,
          frameId: params.frameId,
          loaderId: params.loaderId,
          site_url: siteUrl,
        },
      });
    });
  } else if (method === "Network.loadingFinished") {
    chainFor(key, async () => {
      const p = pendingResponses.get(key);
      if (!p) return;
      pendingResponses.delete(key);
      p.payload.encodedDataLength = params.encodedDataLength;
      p.payload.finishedTimestamp = params.timestamp;
      await fillResponseBody(p);
      if (capturing()) sendToPython(p.payload);
    });
  } else if (method === "Network.loadingFailed") {
    chainFor(key, () => {
      const p = pendingResponses.get(key);
      if (p) {
        pendingResponses.delete(key);
        p.payload.failed = params.errorText || "failed";
        sendToPython(p.payload);
      }
      sendToPython({
        type: "network_failed", requestId: params.requestId, tabId: source.tabId,
        sessionId: source.sessionId, timestamp: params.timestamp, typeStr: params.type,
        errorText: params.errorText, canceled: !!params.canceled,
        blockedReason: params.blockedReason, site_url: siteUrl,
      });
    });
  } else if (method === "Network.webSocketCreated") {
    sendToPython({ type: "websocket_event", event: "created", requestId: params.requestId,
                   url: redactSecrets(params.url, secrets.values()),
                   initiator: trimInitiator(params.initiator),
                   timestamp: Date.now() / 1000.0, tabId: source.tabId, site_url: siteUrl });
  } else if (method === "Network.webSocketHandshakeResponseReceived") {
    sendToPython({ type: "websocket_event", event: "handshake", requestId: params.requestId,
                   timestamp: params.timestamp, status: params.response && params.response.status,
                   headers: params.response && params.response.headers,
                   tabId: source.tabId, site_url: siteUrl });
  } else if (method === "Network.webSocketFrameReceived" || method === "Network.webSocketFrameSent") {
    const sent = method === "Network.webSocketFrameSent";
    let payloadData = params.response && params.response.payloadData;
    // What the CLIENT sends may carry a typed secret (a login over a socket).
    if (sent) payloadData = redactSecrets(payloadData, secrets.values());
    sendToPython({
      type: "websocket_frame",
      direction: sent ? "sent" : "received",
      requestId: params.requestId,
      timestamp: params.timestamp,
      response: { opcode: params.response && params.response.opcode, payloadData },
      tabId: source.tabId,
      site_url: siteUrl
    });
  } else if (method === "Network.webSocketClosed") {
    sendToPython({ type: "websocket_event", event: "closed", requestId: params.requestId,
                   timestamp: params.timestamp, tabId: source.tabId, site_url: siteUrl });
  } else if (method === "Network.eventSourceMessageReceived") {
    // Server-sent events (live feeds, LLM token streams): the response body
    // of an SSE request never "finishes", so the messages are the content.
    sendToPython({ type: "eventsource_message", requestId: params.requestId,
                   timestamp: params.timestamp, eventName: params.eventName,
                   eventId: params.eventId, data: params.data,
                   tabId: source.tabId, site_url: siteUrl });
  }
});

// --- Layout snapshots ---------------------------------------------------------
// DOMSnapshot.captureSnapshot: the whole DOM (iframes + shadow DOM included)
// with every node's layout box, text boxes, paint order and a whitelist of
// computed styles — pixel-level ground truth to pair with the screenshots.
// Taken alongside the content script's load/mutation DOM snapshots, at most
// once per LAYOUT_MIN_GAP_MS per tab, gzip-compressed for the native port.
const LAYOUT_STYLES = ["display", "visibility", "opacity", "position", "z-index", "overflow",
  "color", "background-color", "background-image", "border-top-color", "border-top-width",
  "border-radius", "box-shadow", "font-family", "font-size", "font-weight", "font-style",
  "line-height", "text-align", "text-decoration-line", "text-transform", "letter-spacing",
  "cursor", "transform"];
const LAYOUT_MIN_GAP_MS = 10000;
const lastLayoutAt = new Map();

async function gzipBase64(text) {
  const stream = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
  return arrayBufferToBase64(await new Response(stream).arrayBuffer());
}

async function captureLayoutSnapshot(tab, url) {
  if (!tab || tab.id == null || !capturing() || debuggerCanceledByUser) return;
  const now = Date.now();
  if (now - (lastLayoutAt.get(tab.id) || 0) < LAYOUT_MIN_GAP_MS) return;
  lastLayoutAt.set(tab.id, now);
  const target = { tabId: tab.id };
  try {
    await cdp(target, "DOMSnapshot.enable").catch(() => {});
    const snap = await cdp(target, "DOMSnapshot.captureSnapshot", {
      computedStyles: LAYOUT_STYLES, includeDOMRects: true, includePaintOrder: true,
    }, 20000);
    cdp(target, "DOMSnapshot.disable").catch(() => {});
    if (!capturing()) return;
    scrubLayoutSnapshot(snap, secrets.values());
    const json = JSON.stringify(snap);
    const gz = await gzipBase64(json);
    if (gz.length > MAX_BASE64_CHARS) {
      debugLog("layout snapshot skipped: too large (" + gz.length + " b64 chars)");
      return;
    }
    sendToPython({ type: "layout_snapshot", timestamp: now / 1000.0, url, tabId: tab.id,
                   bytes: json.length, styles: LAYOUT_STYLES, data_gz_b64: gz });
  } catch (e) {
    debugLog("layout snapshot failed: " + (e && e.message));
  }
}

// 2. Intercept Downloads
//
// `onCreated` only knows the initial URL and an empty filename (Chrome
// hasn't picked the save path yet). The actual saved-to-disk path only
// becomes known once the download enters `state: 'complete'`, surfaced
// via `onChanged`. We forward both so the host can log progress AND
// persist the bytes by reading them off disk after completion.
// Downloads that BEGAN while not capturing (paused / no session): their
// completion must not be recorded either.
const uncapturedDownloads = new Set();

chrome.downloads.onCreated.addListener((downloadItem) => {
    // No URL in the diagnostic line: logs ship with the session.
    debugLog("download.onCreated id=" + downloadItem.id
        + " recording=" + isRecording + " paused=" + isPaused);
    if (!capturing()) {
      uncapturedDownloads.add(downloadItem.id);
      return;
    }
    sendToPython({
        type: "download_started",
        id: downloadItem.id,
        url: downloadItem.url,
        finalUrl: downloadItem.finalUrl,
        referrer: downloadItem.referrer,
        filename: downloadItem.filename,
        mime: downloadItem.mime,
        startTime: downloadItem.startTime
    });
});

chrome.downloads.onChanged.addListener((delta) => {
    // Log every transition so a "no download captured" session can be diagnosed
    // (e.g. Drive serving a blob:/data: download that never reaches disk).
    if (delta.state) debugLog("download.onChanged id=" + delta.id + " state=" + delta.state.current);
    if (!delta.state || delta.state.current !== 'complete') return;
    if (uncapturedDownloads.has(delta.id)) {
      uncapturedDownloads.delete(delta.id);
      return;
    }
    chrome.downloads.search({ id: delta.id }, (results) => {
        if (chrome.runtime.lastError) {
            debugLog("download.search error: " + chrome.runtime.lastError.message);
            return;
        }
        if (!results || !results.length) return;
        const item = results[0];
        sendToPython({
            type: "download_complete",
            id: item.id,
            url: item.url,
            finalUrl: item.finalUrl,
            referrer: item.referrer,
            filename: item.filename, // absolute local path on disk
            mime: item.mime,
            fileSize: item.fileSize,
            totalBytes: item.totalBytes,
            startTime: item.startTime,
            endTime: item.endTime
        });
    });
});

// 3. Listen for User Interactions and Periodic Snapshots from Content Script
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // --- Popup control messages ---
  if (request.action === 'get_status') {
    debugLog("popup get_status -> isRecording=" + isRecording + " isPaused=" + isPaused);
    // Opening the popup doubles as a manual "connect now" kick: if a session
    // manifest is waiting, this connects immediately (idle → recording) —
    // the most natural user action when the recorder says "waiting for the
    // extension". No-op while connected or when no session is live.
    ensureConnected();
    sendResponse({ isRecording, isPaused, build: SW_BUILD });
    return true;
  }
  if (request.action === 'toggle_pause') {
    debugLog("popup toggle_pause (was paused=" + isPaused + ", recording=" + isRecording + ")");
    setPaused(!isPaused);
    sendResponse({ isRecording, isPaused });
    return true;
  }
  if (request.action === 'set_paused') {
    setPaused(!!request.paused);
    sendResponse({ isRecording, isPaused });
    return true;
  }

  if (request.action === 'secret_value') {
    // Typed into a sensitive field: remembered in memory ONLY, for scrubbing.
    // Kept even while paused (nothing is captured then anyway).
    const tabId = sender.tab ? sender.tab.id : "x";
    secrets.set(tabId + ":" + sender.frameId + ":" + request.key, request.value);
    return;
  }

  if (request.action === 'sensitive_rects') {
    if (!sender.tab) return;
    let perFrame = sensitiveRects.get(sender.tab.id);
    if (!perFrame) { perFrame = new Map(); sensitiveRects.set(sender.tab.id, perFrame); }
    if ((request.rects && request.rects.length) || request.unlocated) {
      perFrame.set(sender.frameId, { rects: request.rects || [], unlocated: !!request.unlocated });
    } else {
      perFrame.delete(sender.frameId);
    }
    return;
  }

  if (request.action === 'record_event') {
    if (isPaused) {
      return; // Recording paused for privacy — capture nothing.
    }
    if (sender.tab && !sender.tab.active) {
      return; // Ignore background tab periodic events to prevent misattribution
    }
    // Never record interactions / screenshots from the recorder dashboard.
    const tabUrl = sender.tab ? sender.tab.url : null;
    if (isDashboardUrl(tabUrl) || isSkippedUrl(tabUrl)) {
      return;
    }

    const tabId = sender.tab ? sender.tab.id : null;
    const windowId = sender.tab ? sender.tab.windowId : chrome.windows.WINDOW_ID_CURRENT;

    // Use an IIFE so we don't block the message listener with async (which can cause issues)
    (async () => {
        const msg = {
          type: "browser_snapshot",
          timestamp: Date.now() / 1000.0,
          event_type: request.type,
          details: request.details,
          url: tabUrl,
          frameId: sender.frameId,
        };
        if (sender.frameId) msg.frame_url = sender.url;
        if (request.shot === false) {
          // Recorded, but inside the screenshot cooldown: event only.
          msg.screenshot = false;
        } else {
          const { rects, unlocated } = rectsForShot(tabId, sender.frameId, request);
          msg.image_data = await captureScreenshotPNG(tabId, windowId);
          // The host blanks these boxes (top-viewport CSS px; x devicePixelRatio
          // = image px) before the PNG touches disk; "unlocated" = a sensitive
          // field whose position is unknown, so the image is not kept at all.
          if (rects.length) msg.redact_rects = rects;
          if (unlocated) msg.redact_unlocated = true;
        }
        sendToPython(msg);
    })();
    return true; // Keep message channel open if needed
  }

  if (request.action === 'dom_snapshot') {
    // Rendered-DOM snapshot from the content script (top frame). This is how
    // the recorder captures main-document HTML at all: CDP cannot return the
    // body of a navigation, so the content script hands us the hydrated
    // outerHTML and we relay it to the Python host (no screenshot needed). The
    // host backfills the empty WARC bodies, keyed by URL. Same privacy gating
    // as record_event: nothing while paused, nothing from background tabs,
    // nothing from the dashboard.
    if (isPaused) return;
    if (sender.tab && !sender.tab.active) return;
    const tabUrl = sender.tab ? sender.tab.url : null;
    if (isDashboardUrl(tabUrl) || isDashboardUrl(request.url)) return;
    if (isSkippedUrl(tabUrl) || isSkippedUrl(request.url)) return;
    sendToPython({
      type: "dom_snapshot",
      timestamp: request.ts || (Date.now() / 1000.0),
      url: request.url || tabUrl,
      title: request.title || "",
      reason: request.reason || "",
      html: redactSecrets(request.html || "", secrets.values()),
      shadow_roots: request.shadow_roots,
      cssom_bytes: request.cssom_bytes,
      env: request.env
    });
    if (request.reason === "load" || request.reason === "mutation") {
      captureLayoutSnapshot(sender.tab, request.url || tabUrl);
    }
    return;
  }

  if (request.action === 'file_upload') {
    // A file the user attached via <input type=file>. Relayed to the Python
    // host (which decodes + stores the bytes). Same gating as record_event.
    if (isPaused) return;
    if (sender.tab && !sender.tab.active) return;
    const tabUrl = sender.tab ? sender.tab.url : null;
    if (isDashboardUrl(tabUrl) || isSkippedUrl(tabUrl)) return;
    sendToPython({
      type: "file_upload",
      timestamp: (request.file && request.file.ts) || (Date.now() / 1000.0),
      url: tabUrl,
      file: request.file || {}
    });
    return;
  }

  if (request.action === 'file_save_target') {
    // The user picked a SAVE destination via showSaveFilePicker (a download-like
    // action where Chrome's downloads API fires NO event because the page writes
    // the bytes itself). Log the chosen filename so the download is at least
    // recorded as a signal.
    if (isPaused) return;
    if (sender.tab && !sender.tab.active) return;
    const tabUrl = sender.tab ? sender.tab.url : null;
    if (isDashboardUrl(tabUrl) || isSkippedUrl(tabUrl)) return;
    debugLog("file_save_target -> " + (request.name || "(unnamed)"));
    sendToPython({
      type: "download_started",
      via: "showSaveFilePicker",
      timestamp: request.ts || (Date.now() / 1000.0),
      url: tabUrl,
      filename: request.name || null
    });
    return;
  }
});

// Capture a screenshot as PNG and return a `data:image/png;base64,...`
// data URL. PRIMARY path is CDP `Page.captureScreenshot` over the
// already-attached debugger, because `chrome.tabs.captureVisibleTab`
// in MV3 service workers silently ignores `format: 'png'` on many
// builds and returns JPEG anyway. The CDP command honors the format
// option reliably. Fallback to captureVisibleTab is kept for tabs we
// couldn't attach to (newtab, chrome://, etc.).
async function captureScreenshotPNG(tabId, windowId) {
  if (tabId != null) {
    try {
      const result = await new Promise((resolve, reject) => {
        chrome.debugger.sendCommand(
          { tabId },
          "Page.captureScreenshot",
          { format: "png", captureBeyondViewport: false },
          (res) => {
            if (chrome.runtime.lastError) {
              reject(chrome.runtime.lastError);
            } else if (!res || !res.data) {
              reject(new Error("empty CDP screenshot"));
            } else {
              resolve(res);
            }
          }
        );
      });
      return "data:image/png;base64," + result.data;
    } catch (e) {
      // Debugger not attached yet, or the tab is a chrome:// URL we
      // never attach to. Fall through to the tabs API.
      console.warn("CDP capture failed, falling back to tabs.captureVisibleTab:", e.message);
    }
  }
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      chrome.tabs.captureVisibleTab(windowId, { format: "png" }, (url) => {
        if (chrome.runtime.lastError) {
          reject(chrome.runtime.lastError);
        } else {
          resolve(url);
        }
      });
    });
    return dataUrl;
  } catch (e) {
    console.warn("captureVisibleTab fallback also failed:", e.message);
    return null;
  }
}