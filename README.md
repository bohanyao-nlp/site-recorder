# SITE Recorder

Records your browsing in a **separate Chrome or Edge window** for the SITE-Bench study, and packs each
recording into a `.zip` file you send to the study team.

Your everyday browser is never recorded. Recording happens only in the dedicated browser window that
SITE Recorder opens, and only between **Start** and **Stop**.

---

## 1. Install (about 2 minutes)

You need Google Chrome or Microsoft Edge, and an internet connection for the first install.

Get the files in one of two ways:
- unzip the `site-recorder-<version>.zip` the study team sent you;
- or clone this repository: `git clone https://github.com/bohanyao-nlp/site-recorder.git` (private;
  ask the study team for access).

Then, in that folder:

| OS | How |
|---|---|
| **Windows** | Double-click **`install.bat`**. |
| **macOS** | Open Terminal in the folder and run `sh install.sh`. |
| **Linux** | Run `sh install.sh` (Chrome/Edge from the official `.deb`/`.rpm`, not snap/flatpak). |

The installer:
- sets up [uv](https://docs.astral.sh/uv/) and a private Python (it doesn't touch your system Python);
- registers the recorder with your browser;
- adds a **SITE Recorder** shortcut (Start menu and Desktop on Windows, `~/Applications` on macOS, the
  applications menu on Linux);
- then opens the app.

To upgrade, close SITE Recorder (and stop any recording), unzip the new version and run the installer
again. Your recordings are kept.

## 2. One-time setup (in the app, *Setup* tab)

1. **Participant ID**: enter the ID the study team gave you.
2. **Browser**: Chrome or Edge.
3. **Install the SITE Collector extension**: `https://chromewebstore.google.com/detail/site-collector/gbhaoocbgjdighlaaflhgfbmaahgmjaa`

   *If the Web Store is unavailable:* click *Use the bundled copy*. The app turns on Developer mode for
   you and opens the extensions page:
   - click **Load unpacked**;
   - paste the folder path (it's already on your clipboard).

   Keep Developer mode on. In Edge, close its "Turn off developer mode?" notice with the **X**.
4. *(Optional)* Click *Open recording browser (not recorded)* and sign in to the websites your tasks need.
   The recording profile starts empty and remembers these logins.

## 3. Record

1. Click **● Start recording**. The recording browser opens.
2. Do your tasks in **that** window.
3. Click **❚❚ Pause** any time you want privacy. You can also pause from the extension icon in the browser
   toolbar. Nothing is captured while paused.
4. Click **■ Stop**, or close the recording browser window. The session is saved.

**What is recorded:** screenshots of the recording window, clicks, typed text (not passwords),
scrolling, the pages you visit and their network traffic, and files you upload or download in that window.

## 4. Send your recordings

Click **Export new sessions**. A `.zip` per session is written to `~/SiteRecorder/exports`. Send these
files to the study team the way they asked (for example a shared drive upload).

Each zip carries checksums, so the team can detect a damaged transfer. You can review or **delete**
sessions in the app before exporting.

---

## Command line

Everything in the app also works from a terminal:

```
site-recorder setup | start | stop | pause | resume | status | list | export | delete ID | doctor
```

`site-recorder start` records until you close the recording browser or press Ctrl+C.

## Troubleshooting

- Run **Setup → Run diagnostics** (or `site-recorder doctor`) and send the output to the study team.
- **"The extension has not connected"**: check that SITE Collector shows as installed and enabled in
  Setup. If Edge asks to "turn off extensions in developer mode", choose to keep them.
- **"Waiting for the extension…" right after Start**: it can take a few seconds the first time.
- Logs are in the app folder (shown at the top of the diagnostics output) under `logs/`. Each session
  also has `recorder_trace.log` and `extension_host.log` in its own folder.
- **Windows Smart App Control**: the installer avoids the blocked launcher programs automatically. Start
  the app from the **SITE Recorder** shortcut.

## Uninstall

```
site-recorder uninstall           # removes browser registration + shortcuts
uv tool uninstall site-recorder   # removes the program
```

Your recordings in `~/SiteRecorder` are kept. Add `--purge` to the first command to also delete the
recording browser profile.

---

## For the study team

- **Make the participant download:** run `python build_release.py`. This writes
  `dist/site-recorder-<version>.zip` (installers, a prebuilt wheel and this README); send participants
  that file. It also writes `dist/site-collector-<version>-webstore.zip`, for publishing the extension
  to the Chrome Web Store.
- **Collect recordings on the server:** see `server/README.md`. It verifies each zip, puts the sessions
  into site-bench's `sessions/` folder, and builds the video, captions and title.
- **Design, verified platform facts, known limitations and tests:** see `DEVELOPER_NOTES.md`. Run the
  unit tests with `uv run --with pytest pytest tests`; the manual real-browser tests are in
  `tests/manual/`.
