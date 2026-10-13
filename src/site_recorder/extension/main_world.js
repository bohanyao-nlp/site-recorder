// MAIN-world hook — runs in the PAGE's JS context (not the isolated content
// script), so it can patch `window.showOpenFilePicker` / `showSaveFilePicker`.
// Google Drive (and other modern apps) pick files via the File System Access
// API, NOT a `<input type=file>` — the isolated content script can't see those
// calls. We wrap them, read the chosen file's bytes here, and hand the result
// to the isolated world via `window.postMessage`, which content.js forwards to
// the background as a normal `file_upload`.
//
// PRIVACY: an uploaded file IS user content; it lives only in the toxic session
// dir (like the WARC / screenshots) and is purged or synthesized by synth_state
// before anything ships.
(function () {
  if (window.__siteBenchMainHooked) return;
  window.__siteBenchMainHooked = true;

  var MAX = 8000000; // 8 MB cap — larger files captured as metadata only

  function emitUpload(detail) {
    try { window.postMessage({ __siteBenchUpload: true, file: detail }, '*'); } catch (e) { /* ignore */ }
  }

  function captureFile(f, source) {
    var meta = { name: f.name, size: f.size, mime: f.type, source: source, ts: Date.now() / 1000.0 };
    if (f.size > 0 && f.size <= MAX) {
      var r = new FileReader();
      r.onload = function () { emitUpload(Object.assign({}, meta, { dataUrl: r.result })); };
      r.onerror = function () { emitUpload(meta); };
      r.readAsDataURL(f);
    } else {
      emitUpload(Object.assign({}, meta, { truncated: f.size > MAX }));
    }
  }

  async function captureHandles(handles) {
    if (!handles) return;
    var list = Array.isArray(handles) ? handles : [handles];
    for (var i = 0; i < list.length; i++) {
      try {
        var h = list[i];
        if (h && typeof h.getFile === 'function') {
          var f = await h.getFile();
          captureFile(f, 'showOpenFilePicker');
        }
      } catch (e) { /* ignore */ }
    }
  }

  var _open = window.showOpenFilePicker;
  if (typeof _open === 'function') {
    window.showOpenFilePicker = function () {
      var args = arguments;
      return _open.apply(this, args).then(function (handles) {
        try { captureHandles(handles); } catch (e) { /* ignore */ }
        return handles;
      });
    };
  }

  // showSaveFilePicker = the user chose a SAVE target (a download-like action).
  // We can't read bytes (the app writes them after), but the chosen filename is
  // a useful download signal.
  var _save = window.showSaveFilePicker;
  if (typeof _save === 'function') {
    window.showSaveFilePicker = function () {
      var args = arguments;
      return _save.apply(this, args).then(function (handle) {
        try {
          window.postMessage({
            __siteBenchSave: true,
            name: handle && handle.name, ts: Date.now() / 1000.0,
          }, '*');
        } catch (e) { /* ignore */ }
        return handle;
      });
    };
  }
})();
