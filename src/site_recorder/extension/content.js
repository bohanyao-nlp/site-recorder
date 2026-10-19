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

const IS_TOP = window === window.top;

function sendRuntime(msg) {
  try { chrome.runtime.sendMessage(msg); } catch (err) { /* extension context invalidated (reload) — drop */ }
}

// The element an event really happened on. Inside an (open) shadow root
// `e.target` is retargeted to the shadow HOST — a click on a button inside a
// web component would be recorded as a click on the component. composedPath()
// [0] is the innermost element.
function realTarget(e) {
  try {
    const p = e.composedPath && e.composedPath();
    if (p && p.length && p[0] && p[0].nodeType === 1) return p[0];
  } catch (err) { /* ignore */ }
  return e.target;
}

// Open AND closed shadow roots (content scripts may read closed ones through
// chrome.dom), depth-first, including roots nested inside other roots.
function shadowRootOf(el) {
  try {
    if (chrome.dom && chrome.dom.openOrClosedShadowRoot) return chrome.dom.openOrClosedShadowRoot(el);
  } catch (err) { /* ignore */ }
  return el.shadowRoot || null;
}

function collectShadowRoots(root, out) {
  try {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = walker.nextNode())) {
      const sr = shadowRootOf(n);
      if (sr) { out.push(sr); collectShadowRoots(sr, out); }
    }
  } catch (err) { /* ignore */ }
  return out;
}

// querySelectorAll over the document AND every shadow root.
function deepQueryAll(selector, roots) {
  const out = Array.from(document.querySelectorAll(selector));
  for (const r of (roots || collectShadowRoots(document, []))) {
    try { out.push(...r.querySelectorAll(selector)); } catch (err) { /* ignore */ }
  }
  return out;
}

// --- Element-level capture -------------------------------------------------
// For every interaction we attach a structured description of the element
// the user acted on (and its nearest interactive ancestor). This gives the
// reconstruction pipeline ground truth for "which control, labeled what,
// did the user operate" — much more precise replayability evidence than a
// screenshot + coordinates alone (the spec/audit phases can assert the
// exact recorded button labels exist and are wired).
//
// PRIVACY: no input VALUES here — only structural metadata, the visible label
// of the control itself, and value LENGTH for text inputs.
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
  // typed value. innerText forces layout of the whole subtree, so a big
  // container (a hover over <body>) uses textContent instead.
  try {
    const raw = el.childElementCount > 40 ? el.textContent : el.innerText;
    const txt = (raw || '').trim().replace(/\s+/g, ' ');
    if (txt) d.text = txt.slice(0, 80);
  } catch (err) { /* ignore */ }
  if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') &&
      typeof el.value === 'string') {
    d.value_len = el.value.length;
    if (el.type === 'checkbox' || el.type === 'radio') d.checked = !!el.checked;
  }
  if (el.tagName === 'SELECT') {
    if (el.selectedIndex >= 0 && el.options[el.selectedIndex]) {
      d.selected = String(el.options[el.selectedIndex].text || '').slice(0, 80);
    }
    // A native <select> opens a browser-drawn popup that is NOT in CDP
    // screenshots — the option list is the only record of what was offered.
    d.options = Array.from(el.options).slice(0, 40).map(o => String(o.text || '').trim().slice(0, 60));
    if (el.multiple) d.multiple = true;
  }
  if (isSensitiveEl(el)) d.sensitive = true;
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

// --- Sensitive fields --------------------------------------------------------
// A field is sensitive when it is a password (or WAS one — "show password"
// flips type to text), asks for a credential/card/one-time code via
// autocomplete, is masked with CSS (-webkit-text-security, which some banks
// use on type=text to dodge password managers), or is LABELLED like one.
// Sensitive fields never report their value; what is typed into them is sent
// to the service worker only so it can scrub those strings out of network
// bodies (see redact.js) and their on-screen boxes are blanked in screenshots.
const NON_TEXT_TYPES = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'image',
  'file', 'hidden', 'range', 'color']);
const SENSITIVE_AUTOCOMPLETE = new Set(['current-password', 'new-password', 'one-time-code',
  'cc-number', 'cc-csc', 'cc-exp', 'cc-exp-month', 'cc-exp-year']);
const SENSITIVE_WORDS = new RegExp('\\b(' + [
  'pass(word|wd|code|phrase)?', 'pwd', 'pin', 'otp', 'one ?time', 'verification code',
  'security code', 'cvv2?', 'cvc2?', 'csc', 'cvn', 'ssn', 'social security', 'tax ?id',
  'card ?(number|no|num)', 'credit ?card', 'cc ?(num|number)', 'iban', 'routing',
  'account ?(number|no|num)', 'secret', 'api ?key', 'token', 'passport',
].join('|') + ')\\b', 'i');
const stickySensitive = new WeakSet();

function labelWords(s) {
  // userPassword / user_password / user-password  ->  "user Password"
  return String(s || '').replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_\-.:[\]()]+/g, ' ');
}

function isSensitiveEl(el) {
  if (!el || el.nodeType !== 1) return false;
  if (stickySensitive.has(el)) return true;
  let hit = false;
  try {
    const tag = el.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') return false;
    const ty = (el.type || '').toLowerCase();
    if (tag === 'INPUT' && NON_TEXT_TYPES.has(ty)) return false;
    if (ty === 'password') hit = true;
    if (!hit) {
      const ac = (el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
      hit = ac.some(t => SENSITIVE_AUTOCOMPLETE.has(t));
    }
    if (!hit) {
      const cs = getComputedStyle(el);
      const ts = cs.webkitTextSecurity || cs.getPropertyValue('-webkit-text-security');
      hit = !!ts && ts !== 'none';
    }
    if (!hit) {
      let label = [el.getAttribute('name'), el.id, el.getAttribute('aria-label'),
                   el.getAttribute('placeholder')].join(' ');
      if (el.labels && el.labels.length) label += ' ' + (el.labels[0].textContent || '').slice(0, 80);
      hit = SENSITIVE_WORDS.test(labelWords(label));
    }
  } catch (e) { /* ignore */ }
  if (hit) stickySensitive.add(el);
  return hit;
}

function isEditable(el) {
  try {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === 'TEXTAREA') return !el.readOnly && !el.disabled;
    if (el.tagName === 'INPUT') return !NON_TEXT_TYPES.has((el.type || '').toLowerCase()) && !el.readOnly && !el.disabled;
    return document.designMode === 'on';
  } catch (e) { return false; }
}

// Per-field ids, so the service worker keeps ONE latest value per field.
const fieldIds = new WeakMap();
let fieldSeq = 0;
function fieldKey(el) {
  let k = fieldIds.get(el);
  if (!k) { k = ++fieldSeq; fieldIds.set(el, k); }
  return k;
}

function reportSecret(el) {
  const v = typeof el.value === 'string' ? el.value : '';
  sendRuntime({ action: 'secret_value', key: fieldKey(el), value: v });
}

// Boxes of sensitive fields that currently show something, in THIS frame's
// viewport coordinates.
function localSensitiveRects() {
  const out = [];
  try {
    for (const el of deepQueryAll('input,textarea')) {
      if (!el.value || !isSensitiveEl(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue;
      out.push([r.x, r.y, r.width, r.height]);
    }
  } catch (e) { /* ignore */ }
  return out;
}

// --- Frame offsets -----------------------------------------------------------
// Coordinates in a subframe are relative to THAT frame, but the screenshot is
// of the whole tab. Each frame asks its parent where it sits; the parent finds
// the <iframe> whose contentWindow posted the request (comparing WindowProxies
// works across origins), adds its own offset, and answers. A frame whose
// chain does not answer (sandboxed parent without our script) gets null.
const pendingOffsets = new Map();
let offsetSeq = 0;

function getTopOffset() {
  if (IS_TOP) return Promise.resolve({ x: 0, y: 0 });
  return new Promise((resolve) => {
    const id = `sbo-${++offsetSeq}-${Math.random().toString(36).slice(2)}`;
    const timer = setTimeout(() => { pendingOffsets.delete(id); resolve(null); }, 300);
    pendingOffsets.set(id, (off) => { clearTimeout(timer); resolve(off || null); });
    try { window.parent.postMessage({ __sbOffsetReq: id }, '*'); }
    catch (e) { clearTimeout(timer); pendingOffsets.delete(id); resolve(null); }
  });
}

function findFrameElement(win) {
  for (const f of deepQueryAll('iframe,frame')) {
    try { if (f.contentWindow === win) return f; } catch (e) { /* ignore */ }
  }
  return null;
}

function broadcastRelayout() {
  try {
    for (let i = 0; i < window.frames.length; i++) {
      try { window.frames[i].postMessage({ __sbRelayout: 1 }, '*'); } catch (e) { /* ignore */ }
    }
  } catch (e) { /* ignore */ }
}

// Map a rect list from this frame's viewport into the top viewport.
function shiftRects(rects, off) {
  return rects.map(([x, y, w, h]) => [Math.round(x + off.x), Math.round(y + off.y), Math.round(w), Math.round(h)]);
}

// Each frame tells the service worker where ITS sensitive boxes are, so a
// screenshot triggered from ANY frame of the tab can blank all of them (a
// click on "Pay" in the page while the card iframe shows the number).
let lastSensitiveReport = '[]';
let sensitiveTimer = null;
function scheduleSensitiveRefresh() {
  clearTimeout(sensitiveTimer);
  sensitiveTimer = setTimeout(async () => {
    const local = localSensitiveRects();
    let rects = [], unlocated = false;
    if (local.length) {
      const off = await getTopOffset();
      if (off) rects = shiftRects(local, off); else unlocated = true;
    }
    const key = JSON.stringify([rects, unlocated]);
    if (key === lastSensitiveReport) return;
    lastSensitiveReport = key;
    sendRuntime({ action: 'sensitive_rects', rects, unlocated });
  }, 250);
}

// --- Event emission ----------------------------------------------------------
// EVERY interaction is recorded; only the SCREENSHOT is rate-limited. (The
// old throttle dropped the whole event: two clicks within 500 ms on different
// buttons, Ctrl+C then Ctrl+V, or five ArrowDowns all collapsed into one.)
const lastShots = {};
let lastInteractionAt = 0;

function viewportDetails() {
  const d = { dpr: window.devicePixelRatio || 1 };
  if (IS_TOP) {
    d.vw = window.innerWidth; d.vh = window.innerHeight;
    d.sx = Math.round(window.scrollX); d.sy = Math.round(window.scrollY);
  } else {
    d.in_frame = true;
  }
  return d;
}

function emit(eventType, state, details = {}, opts = {}) {
  const now = Date.now();
  const key = `${eventType}_${state}`;
  let shot = opts.shot !== false;
  if (state === 'periodic') {
    // Periodic frames exist only for the video: skip while the user is active
    // and at most one a minute.
    if (now - lastInteractionAt < 3000) return;
    if (now - (lastShots[key] || 0) < 60000) return;
    lastShots[key] = now;
  } else {
    lastInteractionAt = now;
    if (shot) {
      const cooldown = eventType === 'input' && state === 'action' ? 800 : 500;
      if (now - (lastShots[key] || 0) < cooldown) shot = false;
      else lastShots[key] = now;
    }
  }
  const out = Object.assign({}, details, viewportDetails(), { t: now / 1000.0 });
  const local = shot ? localSensitiveRects() : [];

  const send = (off) => {
    if (!IS_TOP) {
      if (off && typeof out.x === 'number' && typeof out.y === 'number') {
        // x/y are TOP-viewport coordinates (they pair with the screenshot);
        // the frame-local ones are kept alongside.
        out.frame_xy = [out.x, out.y];
        out.x = Math.round(out.x + off.x); out.y = Math.round(out.y + off.y);
      } else if (typeof out.x === 'number') {
        out.xy_frame_relative = true;
      }
      if (off) out.frame_offset = [Math.round(off.x), Math.round(off.y)];
    }
    const msg = { action: 'record_event', type: key, details: out, shot };
    if (local.length) {
      if (off) msg.sensitive_rects = shiftRects(local, off);
      else msg.sensitive_unlocated = true;
    }
    sendRuntime(msg);
  };
  if (IS_TOP) send({ x: 0, y: 0 });
  else getTopOffset().then(send);
}

function pointerDetails(e, tgt) {
  return { x: e.clientX, y: e.clientY, screen: [e.screenX, e.screenY], element: describeElement(tgt) };
}

// 1. The "Before" State
document.addEventListener('mousedown', (e) => {
  emit('click', 'pre_action', pointerDetails(e, realTarget(e)));
}, true);

// 2. The "Action" State & 3. The "After" State
document.addEventListener('click', (e) => {
  const details = pointerDetails(e, realTarget(e));
  emit('click', 'action', details);
  // Capture the result of the click (UI changes, modals, navigations)
  setTimeout(() => emit('click', 'post_action', Object.assign({}, details, { delay: 500 })), 500);
  setTimeout(() => emit('click', 'post_action', Object.assign({}, details, { delay: 1500 })), 1500);
}, true);

// The text the user typed into a field IS the task goal (e.g. a search query),
// which is exactly what the benchmark is allowed to surface — so we capture the
// field's VALUE, not just "something changed". Sensitive fields are excluded.
// Toxic session data like everything else; synth_state purges/synthesizes
// downstream.
function editableValue(el) {
  try {
    if (!el || !el.tagName || isSensitiveEl(el)) return undefined;
    if (typeof el.value === 'string') return el.value.slice(0, 300);
    if (el.isContentEditable) return (el.textContent || '').slice(0, 300);
  } catch (e) { /* ignore */ }
  return undefined;
}

function inputDetails(tgt) {
  const d = { tag: tgt && tgt.tagName, value: editableValue(tgt), element: describeElement(tgt) };
  if (isSensitiveEl(tgt)) d.sensitive = true;
  return d;
}

// Set only when an `input`/`beforeinput` event actually carried TEXT. Google Docs
// fires `input` on a hidden contenteditable it immediately clears (empty value),
// so keying off "an input event fired" would wrongly suppress the keystroke
// accumulator below — we key off "an input event captured non-empty text".
let lastUsefulInputAt = 0;
// Per FIELD: typing in A then moving to B within the debounce must still
// record A's final value (one shared timer used to lose it).
const inputTimers = new Map();
const lastInputEmit = new WeakMap();

function flushInput(tgt) {
  if (!inputTimers.has(tgt)) return;
  clearTimeout(inputTimers.get(tgt));
  inputTimers.delete(tgt);
  emit('input', 'post_action', inputDetails(tgt));
}

document.addEventListener('input', (e) => {
  // composedPath()[0] is the REAL element even inside shadow DOM.
  const tgt = realTarget(e);
  if (isSensitiveEl(tgt)) {
    reportSecret(tgt);
    scheduleSensitiveRefresh();
  }
  const details = inputDetails(tgt);
  if (details.value) lastUsefulInputAt = Date.now();
  // Continuous typing in ONE field coalesces (the post_action below carries
  // the final value); a different field is never suppressed.
  const now = Date.now();
  if (now - (lastInputEmit.get(tgt) || 0) >= 800) {
    lastInputEmit.set(tgt, now);
    emit('input', 'action', details);
  }
  // Capture the final state ~1.2s after the user stops typing, or as soon as
  // the field loses focus / commits.
  clearTimeout(inputTimers.get(tgt));
  inputTimers.set(tgt, setTimeout(() => flushInput(tgt), 1200));
}, true);

document.addEventListener('focusout', (e) => {
  const tgt = realTarget(e);
  if (inputTimers.has(tgt)) flushInput(tgt);
}, true);

// `beforeinput` carries the inserted text in `e.data` and fires for many rich
// editors the plain `input` event misses — capture it directly when present.
document.addEventListener('beforeinput', (e) => {
  if (e && typeof e.data === 'string' && e.data) lastUsefulInputAt = Date.now();
}, true);

// Canvas / hidden-contenteditable editors (Google Docs, Sheets, Slides) render
// text to a <canvas> and DON'T expose it via a normal input value — but keystrokes
// still reach a hidden editable element (we inject into its about:blank iframe via
// the manifest's match_about_blank). Accumulate printable keys typed into an
// editable target and flush the run on a pause, UNLESS a real input event
// already captured the text (normal fields) — avoiding double-logging.
let typedBuf = "", typedEl = null, typedTimer = null;
function flushTyped() {
  clearTimeout(typedTimer);
  const buf = typedBuf, el = typedEl;
  typedBuf = ""; typedEl = null;
  if (buf && Date.now() - lastUsefulInputAt > 1500) {
    emit('input', 'post_action',
      { tag: el && el.tagName, value: buf.slice(0, 300), element: describeElement(el), source: 'keys' });
  }
}

// Semantic navigation keys. Printable characters are only recorded as KEYS
// when they cannot be typing: with a modifier (Ctrl+C) or with no editable
// focus (Gmail's "c"/"j"/"k", YouTube's "k", "/" to search) — single-key
// shortcuts are actions an agent has to replay. Digits outside a field are
// left out: a custom PIN pad listening on the page would otherwise log a PIN.
const NAV_KEYS = new Set(['Enter', 'Escape', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft',
  'ArrowRight', 'Backspace', 'Delete', 'Home', 'End', 'PageUp', 'PageDown']);
const heldRepeats = new Map();

function keyCombo(e) {
  const parts = [];
  if (e.metaKey) parts.push('Meta');
  if (e.ctrlKey) parts.push('Control');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  parts.push(e.key === ' ' ? 'Space' : e.key);
  return parts.join('+');
}

document.addEventListener('keydown', (e) => {
  const tgt = realTarget(e);
  const key = typeof e.key === 'string' ? e.key : '';
  const printable = key.length === 1;
  const mod = e.metaKey || e.ctrlKey || e.altKey;
  const editable = isEditable(tgt);
  const sensitive = isSensitiveEl(tgt);
  const isShortcut = mod && printable;                                   // ⌘c, Ctrl+z, Alt+x
  const bareShortcut = !editable && !mod && printable && !/[0-9]/.test(key);  // j, k, /, ?, Space
  const fnKey = /^F([1-9]|1[0-2])$/.test(key);

  if (NAV_KEYS.has(key) || isShortcut || bareShortcut || fnKey) {
    if (e.repeat) {
      // Auto-repeat while a key is held: count, report once on keyup.
      heldRepeats.set(key, (heldRepeats.get(key) || 0) + 1);
    } else {
      const combo = keyCombo(e);
      const details = { key: key === ' ' ? 'Space' : key, combo, editable, element: describeElement(tgt) };
      emit('keydown', 'action', details);
      // Enter/Escape/shortcuts often trigger UI shifts/submits — capture the result.
      if (key === 'Enter' || key === 'Escape' || isShortcut || bareShortcut) {
        setTimeout(() => emit('keydown', 'post_action', Object.assign({}, details, { delay: 500 })), 500);
      }
    }
  }

  // Printable-key accumulator for canvas editors — see flushTyped above.
  if (editable && !mod && printable && !sensitive) {
    typedEl = tgt; typedBuf += key;
    clearTimeout(typedTimer); typedTimer = setTimeout(flushTyped, 1200);
  } else if (key === 'Backspace') {
    typedBuf = typedBuf.slice(0, -1);
  } else if (key === 'Enter' || key === 'Escape') {
    flushTyped();
  }
}, true);

document.addEventListener('keyup', (e) => {
  const key = typeof e.key === 'string' ? e.key : '';
  const n = heldRepeats.get(key);
  if (n) {
    heldRepeats.delete(key);
    emit('keydown', 'repeat_end', { key: key === ' ' ? 'Space' : key, repeats: n }, { shot: false });
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
    reader.onload = () => sendRuntime({ action: 'file_upload', file: Object.assign({}, meta, { dataUrl: reader.result }) });
    reader.onerror = () => sendRuntime({ action: 'file_upload', file: meta });
    reader.readAsDataURL(f);
  } else {
    sendRuntime({ action: 'file_upload', file: Object.assign({}, meta, { truncated: f.size > MAX_UPLOAD_BYTES }) });
  }
}

document.addEventListener('change', (e) => {
  const t = realTarget(e);
  // A committed change ends a typing run.
  if (inputTimers.has(t)) flushInput(t);
  if (t && isSensitiveEl(t)) { reportSecret(t); scheduleSensitiveRefresh(); }
  // (a) classic <input type=file>
  if (!t || t.tagName !== 'INPUT' || t.type !== 'file' || !t.files || !t.files.length) return;
  Array.prototype.forEach.call(t.files, (f) => captureUploadFile(f, describeElement(t), 'input'));
}, true);

// (b) drag-and-drop onto the page (Drive's "drag files here" upload). Capture
// phase so we read dataTransfer.files before the app consumes the drop.
document.addEventListener('drop', (e) => {
  try {
    const dt = e.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    Array.prototype.forEach.call(dt.files, (f) => captureUploadFile(f, describeElement(realTarget(e)), 'drop'));
  } catch (err) { /* ignore */ }
}, true);

// Window messages:
//  (c) File System Access API (showOpenFilePicker) — Drive's "+ New → File
//      upload". The MAIN-world hook (main_world.js) reads the bytes and posts
//      them here; we forward as a normal file_upload. showSaveFilePicker -> a
//      save target the user chose (download-like), forwarded for the host.
//  (d) frame-offset requests/answers and relayout notices (see getTopOffset).
window.addEventListener('message', async (e) => {
  const d = e.data;
  if (!d || typeof d !== 'object') return;
  if (e.source === window) {
    if (d.__siteBenchUpload && d.file) {
      sendRuntime({ action: 'file_upload', file: d.file });
    } else if (d.__siteBenchSave) {
      sendRuntime({ action: 'file_save_target', name: d.name, ts: d.ts });
    }
    return;
  }
  if (d.__sbOffsetReq && e.source) {
    let off = null;
    const frameEl = findFrameElement(e.source);
    if (frameEl) {
      const base = await getTopOffset();
      if (base) {
        const r = frameEl.getBoundingClientRect();
        off = { x: base.x + r.left + frameEl.clientLeft, y: base.y + r.top + frameEl.clientTop };
      }
    }
    try { e.source.postMessage({ __sbOffsetRes: d.__sbOffsetReq, off }, '*'); } catch (err) { /* ignore */ }
  } else if (d.__sbOffsetRes && e.source === window.parent) {
    const cb = pendingOffsets.get(d.__sbOffsetRes);
    if (cb) { pendingOffsets.delete(d.__sbOffsetRes); cb(d.off); }
  } else if (d.__sbRelayout && e.source === window.parent) {
    scheduleSensitiveRefresh();
    broadcastRelayout();
  }
}, false);

// Scrolling: trailing-edge per scroller (800 ms after it stops). Most SPAs
// (Gmail, Slack, Docs) scroll an inner element while window.scrollY stays 0,
// so the scrolled container and its offsets are recorded too.
const scrollTimers = new Map();
document.addEventListener('scroll', (e) => {
  const t = e.target;
  const isDoc = !t || t === document || t === document.documentElement
    || t === document.body || t === document.scrollingElement;
  const k = isDoc ? document : t;
  clearTimeout(scrollTimers.get(k));
  scrollTimers.set(k, setTimeout(() => {
    scrollTimers.delete(k);
    const d = { scrollY: Math.round(window.scrollY), scrollX: Math.round(window.scrollX) };
    if (!isDoc && t.nodeType === 1) {
      const r = t.getBoundingClientRect();
      d.container = {
        selector: shortSelector(t), tag: t.tagName.toLowerCase(),
        scrollTop: Math.round(t.scrollTop), scrollLeft: Math.round(t.scrollLeft),
        scrollHeight: t.scrollHeight, clientHeight: t.clientHeight,
        rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      };
    }
    emit('scroll', 'post_action', d);
    scheduleSensitiveRefresh();
    broadcastRelayout();
  }, 800));
}, true);

window.addEventListener('resize', () => { scheduleSensitiveRefresh(); broadcastRelayout(); });

// 4. The "Hover" State (Debounced Mouse Move).
// The mouse must stay still for `HOVER_IDLE_MS` before we count it as a
// hover. Higher = fewer hover snapshots from micro-jitter; too high and
// we miss genuine reading pauses.
let hoverTimeout;
const HOVER_IDLE_MS = 700;
document.addEventListener('mousemove', (e) => {
  clearTimeout(hoverTimeout);
  const tgt = realTarget(e);
  const x = e.clientX, y = e.clientY, sx = e.screenX, sy = e.screenY;
  hoverTimeout = setTimeout(() => {
    emit('hover', 'action', { x, y, screen: [sx, sy], element: describeElement(tgt) });
  }, HOVER_IDLE_MS);
}, true);

// Periodic generic frames for video consistency (1 per minute fallback)
if (IS_TOP) {
  setInterval(() => {
    emit('periodic', 'periodic');
  }, 60000);
}

// Sensitive fields filled without an input event (server-rendered values,
// some autofill paths) still need their boxes known.
window.addEventListener('load', () => setTimeout(scheduleSensitiveRefresh, 1500));

// --- Full rendered-DOM snapshot (top frame only) ---------------------------
// WHY: the CDP network layer CANNOT retrieve the body of a main-frame
// navigation — `Network.getResponseBody` returns nothing once the document is
// handed to the navigation/render pipeline — so every page the user navigates
// to reaches the recorder with an EMPTY html body (confirmed: 155/178 captured
// text/html responses were 0 bytes, all of them main-document navigations).
// For SPAs the raw body is an un-hydrated shell anyway. We therefore snapshot
// the RENDERED DOM of the top document. This is the reconstructor's text-level
// ground truth — real article prose for content sites, hydrated structure for
// SPAs — and it backfills the empty WARC bodies downstream (the warc-repair
// step keys these by URL).
//
// What a plain outerHTML misses, and this adds:
//  * shadow DOM — serialized as declarative <template shadowrootmode> (open
//    AND closed roots), so web-component sites keep their content;
//  * CSS-in-JS — styled-components / Emotion / MUI / JSS insert rules with
//    CSSOM insertRule, leaving their <style> tags EMPTY in the markup; the
//    live rules (and document.adoptedStyleSheets) are appended as
//    <style data-sitebench-cssom> so the snapshot renders styled;
//  * the doctype (without it a replay renders in quirks mode).
//
// PRIVACY: a DOM snapshot is session-toxic in EXACTLY the same way the
// screenshots and WARC already are — it lives only in the recording dir and is
// purged / synthesized by synth_state before anything is published. Live
// `.value`s are not serialized, BUT frameworks (React) mirror a controlled
// input's value into its `value` ATTRIBUTE — so the value attributes of
// sensitive fields are blanked before the snapshot leaves the page.
function escAttr(v) {
  return String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/ /g, '&nbsp;');
}

function sheetCss(sheet) {
  try {
    let s = '';
    for (const r of sheet.cssRules) s += r.cssText + '\n';
    return s;
  } catch (e) {
    return null;   // cross-origin sheet: its CSS is in the WARC
  }
}

function safeCss(css) {
  return css.replace(/<\/style/gi, '<\\/style');
}

function cssomStyles(roots) {
  let doc = '';
  for (const sheet of Array.from(document.styleSheets)) {
    const node = sheet.ownerNode;
    if (!node || node.tagName !== 'STYLE') continue;     // <link> sheets live in the WARC
    const live = sheetCss(sheet);
    if (!live) continue;
    const declared = (node.textContent || '').trim().length;
    // Rules inserted via CSSOM are invisible in the markup: an empty <style>
    // with live rules, or live rules far exceeding the declared text.
    if (declared === 0 || live.length > declared * 2 + 200) doc += live;
  }
  try {
    for (const sheet of (document.adoptedStyleSheets || [])) doc += sheetCss(sheet) || '';
  } catch (e) { /* ignore */ }
  // Constructable sheets adopted INSIDE shadow roots (Lit, most web-component
  // kits): kept for the reconstructor, keyed by the host element.
  let shadow = '';
  for (const r of roots) {
    try {
      const sheets = r.adoptedStyleSheets || [];
      if (!sheets.length) continue;
      const css = sheets.map(s => sheetCss(s) || '').join('');
      if (css) shadow += `<style data-sitebench-shadow-adopted data-host="${escAttr(shortSelector(r.host))}">${safeCss(css)}</style>`;
    } catch (e) { /* ignore */ }
  }
  return { doc, shadow };
}

function serializeDocument(rich = true) {
  const root = document.documentElement;
  const roots = collectShadowRoots(document, []);
  let html;
  if (rich && typeof root.getHTML === 'function') {
    const attrs = Array.from(root.attributes).map(a => ` ${a.name}="${escAttr(a.value)}"`).join('');
    html = `<html${attrs}>` + root.getHTML({ serializableShadowRoots: true, shadowRoots: roots }) + '</html>';
  } else {
    html = root.outerHTML || '';
  }
  if (document.doctype) html = `<!DOCTYPE ${document.doctype.name || 'html'}>` + html;

  const { doc, shadow } = rich ? cssomStyles(roots) : { doc: '', shadow: '' };
  const extra = (doc ? `<style data-sitebench-cssom>${safeCss(doc)}</style>` : '') + shadow;
  if (extra) {
    const at = html.indexOf('</head>');
    html = at !== -1 ? html.slice(0, at) + extra + html.slice(at) : html + extra;
  }

  // Blank the value attributes of sensitive fields (both spellings the
  // serializer may use for < and > inside attributes).
  for (const el of deepQueryAll('input[value],textarea', roots)) {
    if (!isSensitiveEl(el)) continue;
    const vals = [el.getAttribute('value'), el.tagName === 'TEXTAREA' ? el.textContent : null];
    for (const v of vals) {
      if (!v) continue;
      const a = escAttr(v);
      const b = a.replace(/</g, '&lt;').replace(/>/g, '&gt;');
      for (const s of new Set([`value="${a}"`, `value="${b}"`])) html = html.split(s).join('value=""');
    }
  }
  return { html, shadowRoots: roots.length, cssomBytes: doc.length };
}

function environment() {
  const env = {
    dpr: window.devicePixelRatio || 1,
    viewport: [window.innerWidth, window.innerHeight],
    screen: [screen.width, screen.height],
    lang: navigator.language, languages: (navigator.languages || []).slice(0, 5),
    ua: navigator.userAgent,
  };
  try { env.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { /* ignore */ }
  try {
    env.color_scheme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    env.reduced_motion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { /* ignore */ }
  try {
    // Web fonts that actually loaded (family|weight|style) — local system
    // fonts never appear in the WARC.
    const seen = new Set();
    document.fonts.forEach((f) => {
      if (f.status === 'loaded' && seen.size < 100) seen.add(`${f.family}|${f.weight}|${f.style}`);
    });
    env.fonts = Array.from(seen);
  } catch (e) { /* ignore */ }
  return env;
}

if (IS_TOP) {
  const MAX_DOM_BYTES = 5_000_000;   // skip pathological pages; 5 MB covers any article
  let lastDomLen = 0;
  let lastDomUrl = "";
  let domTimer;

  function captureDom(reason) {
    try {
      const root = document.documentElement;
      if (!root) return;
      let snap = serializeDocument(true);
      // Shadow content + CSSOM can push a big page over the cap: fall back
      // to the plain markup rather than losing the snapshot.
      if (snap.html.length > MAX_DOM_BYTES) snap = serializeDocument(false);
      const { html, shadowRoots, cssomBytes } = snap;
      if (!html || html.length > MAX_DOM_BYTES) return;
      const urlChanged = location.href !== lastDomUrl;
      // Resend on any navigation (new URL) or a material content change; skip
      // SPA micro-mutations that don't change the document size much.
      if (!urlChanged && reason === "mutation" && Math.abs(html.length - lastDomLen) < 512) return;
      lastDomLen = html.length;
      lastDomUrl = location.href;
      sendRuntime({
        action: "dom_snapshot",
        url: location.href,
        title: document.title || "",
        html: html,
        reason: reason,
        shadow_roots: shadowRoots,
        cssom_bytes: cssomBytes,
        env: environment(),
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
