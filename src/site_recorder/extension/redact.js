// Value-match redaction for the service worker.
//
// The content script reports what the participant types into SENSITIVE
// fields (passwords, card numbers, one-time codes …) as `secret_value`
// messages. Those values live ONLY in this module's memory — they are never
// sent to the native host — and every outgoing string that could echo them
// (request bodies, request URLs, sent WebSocket frames, layout-snapshot
// strings) is scrubbed here first, so a typed secret never reaches disk.
//
// Pure module (no chrome.* calls) so it can be unit-tested under node.

export const REDACTED = "[REDACTED]";

// Shorter values would match unrelated text everywhere (a 3-digit "123"
// inside every JSON number); the host's key-name redaction still covers
// short secrets submitted under a sensitive field name.
const MIN_SECRET_LEN = 4;
const MAX_SECRETS = 64;
const SECRET_TTL_MS = 2 * 60 * 60 * 1000;

export class SecretStore {
  constructor(now = () => Date.now()) {
    this._now = now;
    this._byKey = new Map();   // fieldKey -> { value, at }
  }

  // One entry per FIELD (keyed by tab/frame/field), replaced on every input
  // event, so the prefixes typed on the way ("h", "hu", "hun"…) never pile up.
  set(key, value) {
    if (typeof value !== "string" || value.length < MIN_SECRET_LEN) {
      this._byKey.delete(key);
      return;
    }
    this._byKey.delete(key);
    this._byKey.set(key, { value, at: this._now() });
    while (this._byKey.size > MAX_SECRETS) {
      this._byKey.delete(this._byKey.keys().next().value);
    }
  }

  values() {
    const cutoff = this._now() - SECRET_TTL_MS;
    const out = new Set();
    for (const [k, v] of this._byKey) {
      if (v.at < cutoff) this._byKey.delete(k);
      else out.add(v.value);
    }
    // Longest first: a secret that contains another must be replaced whole.
    return Array.from(out).sort((a, b) => b.length - a.length);
  }

  get size() { return this._byKey.size; }
}

function formEncode(v) {
  return encodeURIComponent(v).replace(/%20/g, "+");
}

// Every spelling a value takes on the way into a request: raw, URL-encoded,
// form-encoded (spaces as +), and JSON-escaped (quotes / backslashes).
export function secretVariants(v) {
  const out = new Set([v]);
  try { out.add(encodeURIComponent(v)); } catch (e) { /* lone surrogate */ }
  try { out.add(formEncode(v)); } catch (e) { /* lone surrogate */ }
  try { out.add(JSON.stringify(v).slice(1, -1)); } catch (e) { /* ignore */ }
  return Array.from(out).filter((s) => s && s.length >= MIN_SECRET_LEN);
}

// Replace every occurrence of every secret (in any spelling) with REDACTED.
// Returns the input unchanged (same string) when nothing matched.
export function redactSecrets(text, secrets) {
  if (typeof text !== "string" || !text || !secrets || !secrets.length) return text;
  let out = text;
  for (const s of secrets) {
    for (const variant of secretVariants(s)) {
      if (out.indexOf(variant) !== -1) out = out.split(variant).join(REDACTED);
    }
  }
  return out;
}

// --- DOMSnapshot.captureSnapshot scrubbing ---------------------------------
// The layout snapshot carries every input's live value (`inputValue`,
// `textValue`) and every attribute — including the `value` attribute that
// React mirrors from controlled inputs. Typed text is captured elsewhere (for
// non-sensitive fields), so the live values are dropped wholesale; value
// attributes of password-type / sensitive-autocomplete inputs are blanked;
// and every remaining string is value-matched against the typed secrets.
const SENSITIVE_AUTOCOMPLETE = new Set([
  "current-password", "new-password", "one-time-code",
  "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
]);

export function scrubLayoutSnapshot(snap, secrets) {
  if (!snap || !Array.isArray(snap.strings) || !Array.isArray(snap.documents)) return snap;
  const strings = snap.strings;
  let emptyIdx = strings.indexOf("");
  if (emptyIdx === -1) { emptyIdx = strings.length; strings.push(""); }
  const str = (i) => (typeof i === "number" && i >= 0 ? strings[i] : undefined);
  for (const doc of snap.documents) {
    const nodes = doc && doc.nodes;
    if (!nodes) continue;
    delete nodes.inputValue;
    delete nodes.textValue;
    const names = nodes.nodeName || [];
    const attrs = nodes.attributes || [];
    for (let n = 0; n < attrs.length; n++) {
      if (String(str(names[n]) || "").toUpperCase() !== "INPUT") continue;
      const a = attrs[n];
      if (!Array.isArray(a)) continue;
      let sensitive = false;
      for (let i = 0; i + 1 < a.length; i += 2) {
        const name = String(str(a[i]) || "").toLowerCase();
        const val = String(str(a[i + 1]) || "").toLowerCase();
        if (name === "type" && val === "password") sensitive = true;
        if (name === "autocomplete" && val.split(/\s+/).some((t) => SENSITIVE_AUTOCOMPLETE.has(t))) sensitive = true;
      }
      if (!sensitive) continue;
      for (let i = 0; i + 1 < a.length; i += 2) {
        if (String(str(a[i]) || "").toLowerCase() === "value") a[i + 1] = emptyIdx;
      }
    }
  }
  if (secrets && secrets.length) {
    for (let i = 0; i < strings.length; i++) {
      if (typeof strings[i] === "string" && strings[i]) strings[i] = redactSecrets(strings[i], secrets);
    }
  }
  return snap;
}
