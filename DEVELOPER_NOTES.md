# SITE Recorder: developer notes

This is the standalone recorder package for participants. It is self-contained and does not import
`site_bench`. Participant docs are in `README.md`; server ingestion is in `server/README.md`.

## Releasing

```bash
python build_release.py
```

This writes two files:

- `dist/site-recorder-<version>.zip`: what participants download. It contains the installers,
  the package source, `constraints.txt` and `README.md`.
- `dist/site-collector-<extension version>-webstore.zip`: the extension for a Chrome Web Store
  upload, with the manifest `"key"` stripped (the store rejects keyed packages).

Bump the versions in `pyproject.toml` and `src/site_recorder/__init__.py` together; `build_release.py`
checks that they match. Bump `extension/manifest.json` → `"version"` whenever the extension changes.

## Architecture

```
GUI (Tk) / CLI ──► recorder.py ── active_session.json ──┐
     │                 │  control.json / stop.request   │
     │                 ▼                                ▼
     │      dedicated Chrome/Edge  ◄── extension ──► host.py (native messaging)
     │      (--user-data-dir)          (MV3)            writes the session folder
     └──► export.py ── <participant>_<session>.zip ──► server/ingest.py ──► sessions/<id>_<participant>
```

- **The recording browser** is a dedicated persistent profile per browser, `<app_dir>/profiles/<chrome|edge>`.
  - It is a separate browser instance, so the participant's own browser is never touched or quit.
  - Launch flags: `--silent-debugger-extension-api` (no debugger infobar) and `--disable-sync`. The second
    one matters because Edge implicitly signs a new profile into the Windows account and would otherwise
    sync real data in and out of it.
- **Native-messaging registration**:
  - macOS/Linux: the manifest goes into `<profile>/NativeMessagingHosts/`. Chromium derives this directory
    from `--user-data-dir`, so the everyday browser never sees it.
  - Windows: an HKCU registry key (global) points to a `.bat` wrapper in `<app_dir>/host/`.
  - Registration is permanent. The host decides per connection whether to serve it: it requires an
    `active_session.json` naming a live recorder process (pid + create time), and a connecting browser
    whose `--user-data-dir` is the recording profile.
- **Lifecycle** (`recorder.py`): start → marker state `recording`; stop → marker state `stopping`, then
  `stop.request`.
  - The host relays a `stop` message. The extension stops capturing, detaches its debuggers and answers
    `stopped`. Because the port is FIFO, everything queued before that answer is written first.
  - The host then writes `.host_done` and exits. The recorder closes the browser gracefully
    (WM_CLOSE / SIGTERM) and finalizes (`session_info.json`).
  - Start, stop, recover and adopt are serialized across threads and processes by
    `<app_dir>/lifecycle.lock`.
  - `stop_active(expected_session=…)` never stops a session other than the one its caller means.
  - `recover()` stops sessions whose recorder died.
  - `adopt_if_orphaned()` lets a restarted app take over a live recording.
  - Small JSON state files are written atomically by `fsutil` (temp file + replace), which retries
    Windows sharing violations.
- **Host** (`host.py`): the same files and formats as `src/site_bench/record/extension_host.py`, minus live
  captioning. Additions:
  - a per-session OS lock, so two hosts never append to one WARC;
  - counters and file numbering continue across service-worker reconnects;
  - stdout is reserved for the protocol;
  - the host exits by itself if an extension ignores `stop`.
- **Extension** v1.8.0 = the Web Store 1.7.1 code (attach mode) plus:
  - from `main`: the HTML-body retry and early/pagehide DOM snapshots;
  - fixes:
    - the pause state survives same-session reconnects (it used to un-pause silently);
    - a dismissed debugger banner stays dismissed;
    - response bodies over 32 MiB are dropped instead of killing the port;
    - the debugger attaches to tabs opened with a URL (`pendingUrl`);
    - a liveness guard for re-injection.
  - The manifest carries the store listing's public key, so the unpacked id equals the store id
    (`gbhaoocbgjdighlaaflhgfbmaahgmjaa`).

## Verified facts (Chrome 155 / Edge 155, Windows 11)

| Fact | Consequence |
|---|---|
| Branded Chrome ignores `--load-extension`. | Not used. |
| A DevTools `Extensions.loadUnpacked` install is temporary (purged at the next normal start). | Used only for tests (`extinstall.launch_with_temporary_extension`). Recording launches never carry debugging flags; they would expose `navigator.webdriver`. |
| "Load unpacked" persists, but only while Developer mode is on. Developer mode lives in protected Secure Preferences and can't be pre-seeded. | `extinstall.enable_developer_mode` flips it once through the extensions page's API over a one-time DevTools pipe launch. |
| Web Store installs persist with no Developer mode. | This is the primary install path in Setup. Edge 155 installs from the Chrome Web Store directly ("Get extension"). |
| Windows Smart App Control blocks uv's per-install launcher `.exe`s. | Shortcuts use the tool's `pythonw.exe -m site_recorder.gui`. `post-install` writes `site-recorder.cmd` / `site-recorder-gui.cmd` and removes the `.exe` launchers if they're blocked. |
| `.bat` files are decoded with the console code page. | Wrappers spell paths through `%LOCALAPPDATA%`, `%APPDATA%` and so on, so they stay ASCII. |
| uv refuses `UV_MANAGED_PYTHON` together with `UV_PYTHON_PREFERENCE`. | The installers set only the latter. |
| An installed unpacked extension keeps running its old service worker after its files change, even after a version change and a browser restart. | `recorder.reload_unpacked_if_changed` runs `developerPrivate.reload` once, through a one-time DevTools launch, whenever the bundled code changed (the copied manifest's 4th version component is a content stamp). This happens automatically at the next Start after an upgrade. |
| Smart App Control also judges each Python *build*. A brand-new uv-managed CPython build (3.13.15 here) is blocked with `0xc0e90002`; 3.12.14 runs. | `install.ps1` tries 3.12.14, then 3.13, then 3.11. It keeps the first build whose `python.exe` and `pythonw.exe` both run (with Tk, the compiled dependencies and the recorder), and refuses to upgrade while the app or a recording is running. |

## Testing done

- `pytest tests` (44 tests):
  - the host protocol end to end as a subprocess: gating, the full session file set, WARC readability,
    the screenshot/event timestamp join, reconnects, a stop with an uncooperative extension, a garbage
    length prefix;
  - export/verify, including tampered and truncated zips;
  - server ingest;
  - state parsing, finalize rules, recover/adopt;
  - batch files with non-ASCII paths.
- Real-browser e2e on this machine in Edge and Chrome, in production launch mode with a persistent
  install, using `tests/manual/e2e.py` and `testsite.py`:
  - the extension connects in about 1.5 s;
  - in-tab navigation, XHR/fetch, POST and subresources are captured with tab ids;
  - nothing is captured while paused;
  - stop flushes, finalize runs, and the export verifies.
- A GUI smoke test drives the real Tk window through Start, Pause, Resume, Stop and Export
  (`gui_smoke.py`).
- The real installer was run from the release zip in a normal user terminal on Windows:
  - fresh install;
  - upgrade over an existing install, with an automatic extension reload and a post-upgrade recording
    after which `doctor` reported ALL GOOD;
  - full uninstall (`site-recorder uninstall --purge` plus `uv tool uninstall`), which left nothing behind.
- In that real environment, the participant's own everyday Edge (with the store extension) tried to
  connect during the recording and was refused by the host's browser-profile check, as designed.
- An adversarial review covered four dimensions (macOS/Linux, Windows plus installers, lifecycle
  robustness, data parity plus extension), each verified by a second agent. All 38 confirmed findings
  are fixed; 4 were refuted.
- Manual real-browser tests are in `tests/manual/` (see its README).

macOS and Linux code paths were reviewed but **not executed** (no machine available). A first macOS
participant run is the main remaining risk. Run `site-recorder doctor` there first.

## Known limitations (same as the in-repo recorder)

- **First page of a new tab.** When a tab is opened with a URL (the start page, a link opened in a new
  tab), its first document can finish loading before the debugger attaches. That page's network records
  are then missing from the WARC; its rendered HTML is still captured as a DOM snapshot. Navigations
  inside an attached tab are captured fully. A fix would be browser-level auto-attach
  (`Target.setAutoAttach` with `waitForDebuggerOnStart`), a larger change.
- **macOS window close.** Closing the last window doesn't quit the browser on macOS. The bundled
  extension (1.8.0) reports "all windows closed", which ends the session. With the Web Store 1.7.1
  build, the session ends with Stop or ⌘Q instead.
- **Windows registry is shared.** If a participant also has the store SITE Collector in their everyday
  browser, that browser starts the host every 30 s; the host refuses after its profile check. This is
  harmless, apart from a line in `logs/host_refusals.log`.
- **Edge developer-mode notice.** With the bundled (unpacked) extension, Edge shows a "Turn off developer
  mode?" notice at each start. The Web Store install avoids it.
- **Store version.** The Web Store currently has 1.7.1. Publishing `site-collector-1.8.0-webstore.zip` from
  the listing owner's account (valteu) brings store installs up to date; they auto-update.

## Tabled improvements (discussed, not implemented)

- Capture additions: request bodies (CDP `postData`), redirect responses, sent WebSocket frames and their
  URLs, drag/selection/clipboard events, and better password-field detection.
- The `content.js` issues from the review:
  - rapid events of the same kind are dropped, not just their screenshots;
  - clicks inside shadow DOM use `e.target` instead of `composedPath()[0]`;
  - coordinates inside iframes are relative to the frame, not the page.
