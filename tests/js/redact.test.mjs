// Unit tests for the service worker's value-match redaction.
// Run: node --test tests/js
import { test } from "node:test";
import assert from "node:assert/strict";
import { SecretStore, redactSecrets, scrubLayoutSnapshot, REDACTED } from "../../src/site_recorder/extension/redact.js";

test("secret is scrubbed in raw, URL-, form- and JSON-encoded spellings", () => {
  const s = ["p@ss w\"rd"];
  assert.equal(redactSecrets("pw=p@ss w\"rd", s), `pw=${REDACTED}`);
  assert.equal(redactSecrets("pw=p%40ss%20w%22rd", s), `pw=${REDACTED}`);
  assert.equal(redactSecrets("pw=p%40ss+w%22rd", s), `pw=${REDACTED}`);
  assert.equal(redactSecrets('{"pw":"p@ss w\\"rd"}', s), `{"pw":"${REDACTED}"}`);
});

test("short values are ignored, unrelated text is untouched", () => {
  const store = new SecretStore();
  store.set("1:0:1", "abc");
  assert.deepEqual(store.values(), []);
  assert.equal(redactSecrets("hello world", ["hunter22"]), "hello world");
});

test("one entry per field: typed prefixes do not accumulate", () => {
  const store = new SecretStore();
  for (const v of ["hunt", "hunte", "hunter", "hunter2"]) store.set("1:0:7", v);
  assert.deepEqual(store.values(), ["hunter2"]);
  // clearing the field forgets it
  store.set("1:0:7", "");
  assert.deepEqual(store.values(), []);
});

test("longest secret wins when one contains another", () => {
  const store = new SecretStore();
  store.set("a", "1234");
  store.set("b", "123456789");
  assert.equal(redactSecrets("card=123456789", store.values()), `card=${REDACTED}`);
});

test("entries expire", () => {
  let now = 0;
  const store = new SecretStore(() => now);
  store.set("a", "hunter22");
  now = 3 * 60 * 60 * 1000;
  assert.deepEqual(store.values(), []);
});

test("layout snapshot: live values dropped, password value attribute blanked, strings scrubbed", () => {
  const snap = {
    strings: ["INPUT", "type", "password", "value", "hunter22", "DIV", "title", "hi hunter22", "text"],
    documents: [{
      nodes: {
        nodeName: [0, 5, 0],
        attributes: [[1, 2, 3, 4], [6, 7], [1, 8, 3, 4]],
        inputValue: { index: [0], value: [4] },
        textValue: { index: [], value: [] },
      },
    }],
  };
  scrubLayoutSnapshot(snap, ["hunter22"]);
  const nodes = snap.documents[0].nodes;
  assert.equal(nodes.inputValue, undefined);
  assert.equal(nodes.textValue, undefined);
  assert.equal(snap.strings[nodes.attributes[0][3]], "");          // password value attr blanked
  assert.equal(snap.strings[7], `hi ${REDACTED}`);                 // secret scrubbed everywhere
  // the text input's value attribute pointed at the same (now scrubbed) string
  assert.equal(snap.strings[nodes.attributes[2][3]], REDACTED);
});
