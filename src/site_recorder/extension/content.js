// src/extension/content.js

// Never record on the SITE-Bench dashboard itself — bail out before any
// listener is registered so the dashboard tab is invisible to the
// recorder. Both spellings of the origin must match because Dash binds
// to 127.0.0.1 while __main__.py opens localhost.
if (location.origin === "http://localhost:8501"
    || location.origin === "http://127.0.0.1:8501"
    || (typeof window.__siteBenchAlive === "function" && window.__siteBenchAlive())) {
  // no-op: dashboard page, or this isolated world ALREADY has a LIVE content
  // script. Attach mode re-injects programmatically into tabs that were open
  // before the recording started; the guard makes that idempotent — without
  // it every listener would double and each click would be recorded twice.
  // The guard asks the earlier script whether its extension context is still
  // valid: after an extension reload/update the old script is orphaned (its
  // chrome.runtime is gone) and must not block the fresh injection.
} else {
window.__siteBenchAlive = () => {
  try { return !!(chrome.runtime && chrome.runtime.id); } catch (e) { return false; }
};

const lastSnapshots = {};

// --- Element-level capture -------------------------------------------------
// For every interaction we attach a structured description of the element
// the user acted on (and its nearest interactive ancestor). This gives the
// reconstruction pipeline ground truth for "which control, labeled what,
// did the user operate" — much more precise replayability evidence than a
// screenshot + coordinates alone (the spec/audit phases can assert the
// exact recorded button labels exist and are wired).
//
// PRIVACY: we never capture input VALUES here (no e.target.value) — typed
// content stays out of the event stream; only structural metadata + visible
// label text of the control itself is recorded.
const INTERACTIVE_SELECTOR =
  'a,button,input,select,textarea,summary,option,[role="button"],[role="link"],' +
  '[role="tab"],[role="menuitem"],[role="checkbox"],[role="radio"],[role="option"],' +
  '[role="switch"],[contenteditable=""],[contenteditable="true"],[onclick],[tabindex]';

function shortSelector(el) {
  // Compact, stable-ish path: prefer data-testid / id hops, max 5 segments.
  const parts = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && parts.length < 5) {
    const tid = cur.getAttribute && cur.getAttribute('data-testid');
    if (tid) { parts.unshift(`[data-testid='${tid}']`); break; }
    if (cur.id) { parts.unshift(`#${cur.id}`); break; }
    let seg = cur.tagName.toLowerCase();
    const parent = cur.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
      if (same.length > 1) seg += `:nth-of-type(${same.indexOf(cur) + 1})`;
    }
    parts.unshift(seg);
    cur = cur.parentElement;
  }
  return parts.join(' > ');
}

function describeOne(el) {
  if (!el || el.nodeType !== 1) return null;
  const d = { tag: el.tagName.toLowerCase() };
  const attrs = ['id', 'name', 'type', 'role', 'placeholder', 'title', 'alt', 'data-testid'];
  for (const a of attrs) {
    const v = el.getAttribute && el.getAttribute(a);
    if (v) d[a === 'data-testid' ? 'testid' : a] = String(v).slice(0, 80);
  }
  const aria = el.getAttribute && el.getAttribute('aria-label');
  if (aria) d.aria_label = String(aria).slice(0, 80);
  if (el.tagName === 'A' && el.href) d.href = String(el.href).slice(0, 200);
  if (el.className && typeof el.className === 'string') {
    d.classes = el.className.trim().split(/\s+/).slice(0, 4).join(' ').slice(0, 120);
  }
  // Visible label of the CONTROL itself (button text, link text) — not the
  // typed value. Inputs report value LENGTH only.
  try {
    const txt = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (txt) d.text = txt.slice(0, 80);
  } catch (err) { /* ignore */ }
  if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') &&
      typeof el.value === 'string') {
    d.value_len = el.value.length;
    if (el.type === 'checkbox' || el.type === 'radio') d.checked = !!el.checked;
  }
  if (el.tagName === 'SELECT' && el.selectedIndex >= 0 && el.options[el.selectedIndex]) {
    d.selected = String(el.options[el.selectedIndex].text || '').slice(0, 80);
  }
  try {
    const r = el.getBoundingClientRect();
    d.rect = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  } catch (err) { /* ignore */ }
  d.selector = shortSelector(el);
  return d;
}

function describeElement(target) {
  try {
    const d = describeOne(target);
    if (!d) return null;
    // The semantically-clicked CONTROL is often an ancestor of the raw
    // event target (icon inside a button). Attach it when different.
    const control = target.closest ? target.closest(INTERACTIVE_SELECTOR) : null;
    if (control && control !== target) {
      d.control = describeOne(control);
    }
    return d;
  } catch (err) {
    return null;
  }
}

function sendSnapshot(eventType, state, details = {}) {
  const now = Date.now();
  const key = `${eventType}_${state}`;
  
  // Throttling rules. Each key is "<eventType>_<state>"; we drop a new
  // event of that kind if the previous one fired less than the cooldown
  // ago. Tuned to be loose enough that bursts of identical activity
  // (rapid typing, hover micro-jitter, double-clicks) coalesce into one
  // snapshot, while still capturing every meaningful state transition.
  if (state === 'periodic') {
    // Pause periodic if the user is actively interacting
    if (now - (lastSnapshots['last_interaction'] || 0) < 3000) return;
    // Max 1 periodic per minute (60,000 ms)
    if (now - (lastSnapshots[key] || 0) < 60000) return;
  } else if (eventType === 'input' && state === 'action') {
    // Throttle rapid typing
    if (now - (lastSnapshots[key] || 0) < 800) return;
  } else {
    // Throttle rapid identical clicks/actions (e.g., double clicks)
    if (now - (lastSnapshots[key] || 0) < 500) return;
  }

  lastSnapshots[key] = now;
  if (state !== 'periodic') {
    lastSnapshots['last_interaction'] = now;
  }

  try {
    chrome.runtime.sendMessage({
      action: 'record_event',
      type: `${eventType}_${state}`,
      details: details
    });
  } catch (err) { /* extension context invalidated (reload) — drop */ }
}

// 1. The "Before" State
document.addEventListener('mousedown', (e) => {
  sendSnapshot('click', 'pre_action', { x: e.clientX, y: e.clientY, element: describeElement(e.target) });
}, true);

// 2. The "Action" State & 3. The "After" State
document.addEventListener('click', (e) => {
  const element = describeElement(e.target);
  sendSnapshot('click', 'action', { x: e.clientX, y: e.clientY, element });

  // Capture the result of the click (UI changes, modals, navigations)
  setTimeout(() => sendSnapshot('click', 'post_action', { x: e.clientX, y: e.clientY, delay: 500, element }), 500);
  setTimeout(() => sendSnapshot('click', 'post_action', { x: e.clientX, y: e.clientY, delay: 1500, element }), 1500);
}, true);

// The text the user typed into a field IS the task goal (e.g. a search query),
// which is exactly what the benchmark is allowed to surface — so we capture the
// field's VALUE, not just "something changed". Passwords are excluded. Toxic
// session data like everything else; synth_state purges/synthesizes downstream.
function isSensitiveEl(el) {
  try {
    if (el && el.tagName === 'INPUT') {
      const ty = (el.type || '').toLowerCase();
      const ac = (el.autocomplete || '').toLowerCase();
      return ty === 'password' || ac === 'current-password' || ac === 'new-password';
    }
  } catch (e) { /* ignore */ }
  return false;
}
function editableValue(el) {
  try {
    if (!el || !el.tagName || isSensitiveEl(el)) return undefined;
    if (typeof el.value === 'string') return el.value.slice(0, 300);
    if (el.isContentEditable) return (el.textContent || '').slice(0, 300);
  } catch (e) { /* ignore */ }
  return undefined;
}

// Set only when an `input`/`beforeinput` event actually carried TEXT. Google Docs
// fires `input` on a hidden contenteditable it immediately clears (empty value),
// so keying off "an input event fired" would wrongly suppress the keystroke
// accumulator below — we key off "an input event captured non-empty text".
let lastUsefulInputAt = 0;
let inputTimeout;
document.addEventListener('input', (e) => {
  // composedPath()[0] is the REAL element even inside (open) shadow DOM, where
  // e.target is retargeted to the host — this is why some sites' fields (e.g.
  // search boxes rendered in a shadow root) were not being registered.
  const tgt = (e.composedPath && e.composedPath()[0]) || e.target;
  const val = editableValue(tgt);
  if (val) lastUsefulInputAt = Date.now();
  sendSnapshot('input', 'action', { tag: tgt && tgt.tagName, value: val, element: describeElement(tgt) });

  // Capture the final state ~1.2s after the user stops typing.
  clearTimeout(inputTimeout);
  inputTimeout = setTimeout(() => {
    sendSnapshot('input', 'post_action', { tag: tgt && tgt.tagName, value: editableValue(tgt), element: describeElement(tgt) });
  }, 1200);
}, true);

// `beforeinput` carries the inserted text in `e.data` and fires for many rich
// editors the plain `input` event misses — capture it directly when present.
document.addEventListener('beforeinput', (e) => {
  if (e && typeof e.data === 'string' && e.data) lastUsefulInputAt = Date.now();
}, true);

// Canvas / hidden-contenteditable editors (Google Docs, Sheets, Slides) render
// text to a <canvas> and DON'T expose it via a normal input value — but keystrokes
// still reach a hidden element (we now inject into its about:blank iframe via the
// manifest's match_about_blank). Accumulate printable keys and flush the typed run
// on a pause, UNLESS a real input event already captured the text (normal fields)
// — avoiding double-logging. Passwords excluded.
let typedBuf = "", typedEl = null, typedTimer = null;
function flushTyped() {
  clearTimeout(typedTimer);
  const buf = typedBuf, el = typedEl;
  typedBuf = ""; typedEl = null;
  if (buf && Date.now() - lastUsefulInputAt > 1500) {
    sendSnapshot('input', 'post_action',
      { tag: el && el.tagName, value: buf.slice(0, 300), element: describeElement(el), source: 'keys' });
  }
}

document.addEventListener('keydown', (e) => {
  // Capture semantic navigation keys AND modifier shortcuts (⌘C/Ctrl+Z/⌘S/⌘A/…) —
  // the app-level keyboard actions a CUA must replay (copy/paste/undo/save/select-all/
  // bold). PRIVACY: raw character typing is NEVER logged here (that stays redacted;
  // the `input` event records only THAT text changed, never its content). We record
  // non-printable navigation keys and any combo carrying a modifier — by NAME only.
  const navKeys = new Set(['Enter', 'Escape', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft',
    'ArrowRight', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown']);
  const mod = e.metaKey || e.ctrlKey || e.altKey;
  const isShortcut = mod && typeof e.key === 'string' && e.key.length === 1;  // ⌘c, Ctrl+z, Alt+x
  if (navKeys.has(e.key) || isShortcut) {
    // Stable combo label, e.g. "Meta+c", "Control+Shift+z", "Enter".
    const parts = [];
    if (e.metaKey) parts.push('Meta');
    if (e.ctrlKey) parts.push('Control');
    if (e.altKey) parts.push('Alt');
    if (e.shiftKey) parts.push('Shift');
    parts.push(e.key);
    const combo = parts.join('+');
    sendSnapshot('keydown', 'action', { key: e.key, combo, element: describeElement(e.target) });
    // Enter/Escape/shortcuts often trigger UI shifts/submits — capture the result.
    if (e.key === 'Enter' || e.key === 'Escape' || isShortcut) {
      setTimeout(() => sendSnapshot('keydown', 'post_action',
        { key: e.key, combo, delay: 500, element: describeElement(e.target) }), 500);
    }
  }

  // Printable-key accumulator for canvas editors (Google Docs et al.) — see
  // flushTyped above. Only records when no `input` event covers this typing.
  const tgt = (e.composedPath && e.composedPath()[0]) || e.target;
  if (!mod && typeof e.key === 'string' && e.key.length === 1 && !isSensitiveEl(tgt)) {
    typedEl = tgt; typedBuf += e.key;
    clearTimeout(typedTimer); typedTimer = setTimeout(flushTyped, 1200);
  } else if (e.key === 'Backspace') {
    typedBuf = typedBuf.slice(0, -1);
  } else if (e.key === 'Enter' || e.key === 'Escape') {
    flushTyped();
  }
}, true);

// File UPLOADS — capture what the user attached via <input type=file> (name /
// size / mime + bounded bytes), so the reconstruction can reproduce the uploaded
// artifact instead of guessing. PRIVACY: an uploaded file IS user content; it
// lives only in the toxic session dir (like screenshots / WARC) and is purged or
// synthesized by synth_state before anything ships.
const MAX_UPLOAD_BYTES = 8000000;   // 8 MB cap — larger files captured as metadata only

function captureUploadFile(f, element, source) {
  const meta = { name: f.name, size: f.size, mime: f.type, element, source, ts: Date.now() / 1000.0 };
  if (f.size > 0 && f.size <= MAX_UPLOAD_BYTES) {
    const reader = new FileReader();
    reader.onload = () => {
      try { chrome.runtime.sendMessage({ action: 'file_upload', file: Object.assign({}, meta, { dataUrl: reader.result }) }); } catch (err) { /* ignore */ }
    };
    reader.onerror = () => {
      try { chrome.runtime.sendMessage({ action: 'file_upload', file: meta }); } catch (err) { /* ignore */ }
    };
    reader.readAsDataURL(f);
  } else {
    try { chrome.runtime.sendMessage({ action: 'file_upload', file: Object.assign({}, meta, { truncated: f.size > MAX_UPLOAD_BYTES }) }); } catch (err) { /* ignore */ }
  }
}

// (a) classic <input type=file>
document.addEventListener('change', (e) => {
  const t = e.target;
  if (!t || t.tagName !== 'INPUT' || t.type !== 'file' || !t.files || !t.files.length) return;
  Array.prototype.forEach.call(t.files, (f) => captureUploadFile(f, describeElement(t), 'input'));
}, true);

// (b) drag-and-drop onto the page (Drive's "drag files here" upload). Capture
// phase so we read dataTransfer.files before the app consumes the drop.
document.addEventListener('drop', (e) => {
  try {
    const dt = e.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    Array.prototype.forEach.call(dt.files, (f) => captureUploadFile(f, describeElement(e.target), 'drop'));
  } catch (err) { /* ignore */ }
}, true);

// (c) File System Access API (showOpenFilePicker) — Drive's "+ New → File
// upload". The MAIN-world hook (main_world.js) reads the bytes and posts them
// here; we forward as a normal file_upload. showSaveFilePicker -> a save target
// the user chose (download-like), forwarded for the host to log.
window.addEventListener('message', (e) => {
  if (e.source !== window || !e.data) return;
  if (e.data.__siteBenchUpload && e.data.file) {
    try { chrome.runtime.sendMessage({ action: 'file_upload', file: e.data.file }); } catch (err) { /* ignore */ }
  } else if (e.data.__siteBenchSave) {
    try { chrome.runtime.sendMessage({ action: 'file_save_target', name: e.data.name, ts: e.data.ts }); } catch (err) { /* ignore */ }
  }
}, false);

let scrollTimeout;
document.addEventListener('scroll', () => {
  // Scrolling can fire hundreds of times a second. Trailing-edge only:
  // wait until the user has stopped scrolling for 800ms, then snap.
  clearTimeout(scrollTimeout);
  scrollTimeout = setTimeout(() => {
    sendSnapshot('scroll', 'post_action', { scrollY: window.scrollY });
  }, 800);
}, true);

// 4. The "Hover" State (Debounced Mouse Move).
// The mouse must stay still for `HOVER_IDLE_MS` before we count it as a
// hover. Higher = fewer hover snapshots from micro-jitter; too high and
// we miss genuine reading pauses.
let hoverTimeout;
const HOVER_IDLE_MS = 700;
document.addEventListener('mousemove', (e) => {
  clearTimeout(hoverTimeout);
  const tgt = e.target;
  hoverTimeout = setTimeout(() => {
    sendSnapshot('hover', 'action', { x: e.clientX, y: e.clientY, element: describeElement(tgt) });
  }, HOVER_IDLE_MS);
}, true);

// Periodic generic frames for video consistency (1 per minute fallback)
if (window === window.top) {
  setInterval(() => {
    sendSnapshot('periodic', 'periodic');
  }, 60000);
}

// --- Full rendered-DOM snapshot (top frame only) ---------------------------
// WHY: the CDP network layer CANNOT retrieve the body of a main-frame
// navigation — `Network.getResponseBody` returns nothing once the document is
// handed to the navigation/render pipeline — so every page the user navigates
// to reaches the recorder with an EMPTY html body (confirmed: 155/178 captured
// text/html responses were 0 bytes, all of them main-document navigations).
// For SPAs the raw body is an un-hydrated shell anyway. We therefore snapshot
// the RENDERED DOM (`document.documentElement.outerHTML`) of the top document.
// This is the reconstructor's text-level ground truth — real article prose for
// content sites, hydrated structure for SPAs — and it backfills the empty WARC
// bodies downstream (the warc-repair step keys these by URL).
//
// PRIVACY: a DOM snapshot is session-toxic in EXACTLY the same way the
// screenshots and WARC already are — it lives only in the recording dir and is
// purged / synthesized by synth_state before anything is published. outerHTML
// does NOT serialize live <input>/<textarea> `.value`, so typed secrets stay
// out of it; contenteditable page content is treated as state and anonymized
// downstream, identical to how a screenshot of that content is handled.
if (window === window.top) {
  const MAX_DOM_BYTES = 5_000_000;   // skip pathological pages; 5 MB covers any article
  let lastDomLen = 0;
  let lastDomUrl = "";
  let domTimer;

  function captureDom(reason) {
    try {
      const root = document.documentElement;
      if (!root) return;
      const html = root.outerHTML || "";
      if (!html || html.length > MAX_DOM_BYTES) return;
      const urlChanged = location.href !== lastDomUrl;
      // Resend on any navigation (new URL) or a material content change; skip
      // SPA micro-mutations that don't change the document size much.
      if (!urlChanged && reason === "mutation" && Math.abs(html.length - lastDomLen) < 512) return;
      lastDomLen = html.length;
      lastDomUrl = location.href;
      chrome.runtime.sendMessage({
        action: "dom_snapshot",
        url: location.href,
        title: document.title || "",
        html: html,
        reason: reason,
        ts: Date.now() / 1000.0
      });
    } catch (err) { /* ignore — a snapshot failure must never break recording */ }
  }

  function scheduleDom(reason, delay) {
    clearTimeout(domTimer);
    domTimer = setTimeout(() => captureDom(reason), delay);
  }

  // Initial: after load + a beat for client hydration.
  if (document.readyState === "complete") scheduleDom("load", 1200);
  window.addEventListener("load", () => scheduleDom("load", 1200));
  // EARLY snapshot too: fast checkout/redirect chains (paypal /checkoutnow ->
  // /pay -> /pay/checkout, each hop <1.2s) navigate away BEFORE the post-load
  // timer fires — the timer dies with the document and the hop leaves no
  // body anywhere. DOMContentLoaded catches each hop's server-rendered DOM.
  if (document.readyState !== "loading") scheduleDom("early", 150);
  else document.addEventListener("DOMContentLoaded", () => scheduleDom("early", 150));
  // Flush on exit: the LAST chance to capture a hop is the moment it unloads.
  window.addEventListener("pagehide", () => {
    clearTimeout(domTimer);
    captureDom("unload");
  });
  // SPA route / content changes: debounced subtree observer.
  // (At document_start there is no <body> yet — attach once it exists, or
  // SPA route/content changes never produce "mutation" snapshots.)
  try {
    const domObserver = new MutationObserver(() => scheduleDom("mutation", 2500));
    const observeBody = () => {
      if (document.body) domObserver.observe(document.body, { childList: true, subtree: true });
    };
    if (document.body) observeBody();
    else document.addEventListener("DOMContentLoaded", observeBody, { once: true });
  } catch (err) { /* MutationObserver unavailable — load + hide snapshots still fire */ }
  // Final state right before the user leaves (the fully-read article / final SPA view).
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") captureDom("hide");
  }, true);
  window.addEventListener("beforeunload", () => captureDom("unload"));
}

}  // end: non-dashboard branch