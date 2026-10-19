# SITE Recorder

This is the tool Sherry and I are using to collect browsing data for SITE-Bench. It records what you do
in a **separate Chrome or Edge window** and saves each recording as a `.zip` file that you can send
to me when you're ready.

Your normal browser is never recorded. The recorder only captures the separate browser window it opens
for you, and only between **Start** and **Stop**.

---

## 1. Install (about 2 minutes)

You'll need Google Chrome or Microsoft Edge, and an internet connection for the first install.

Get the files in one of two ways:
- unzip the `site-recorder-<version>.zip` I sent you, or
- clone this repo: `git clone https://github.com/bohanyao-nlp/site-recorder.git`

Then, in that folder:

| OS | How |
|---|---|
| **Windows** | Double-click **`install.bat`**. |
| **macOS** | Open Terminal in the folder and run `sh install.sh`. |
| **Linux** | Run `sh install.sh`. Chrome/Edge needs to be installed from the official `.deb`/`.rpm`, not snap or flatpak. |

The installer:
- sets up [uv](https://docs.astral.sh/uv/) with its own copy of Python, so your system Python isn't
  touched;
- connects the recorder to your browser;
- adds a **SITE Recorder** shortcut (Start menu and Desktop on Windows, `~/Applications` on macOS, the
  applications menu on Linux);
- opens the app when it's done.

To update to a new version, close SITE Recorder (stop any recording first), unzip the new version and
run the installer again. Your existing recordings won't be affected.

## 2. One-time setup (in the app's *Setup* tab)

1. **Participant ID**: enter the ID I gave you.
2. **Browser**: pick Chrome or Edge.
3. [**Install the SITE Collector extension**](https://chromewebstore.google.com/detail/site-collector/gbhaoocbgjdighlaaflhgfbmaahgmjaa).

   *If the Web Store link doesn't work:* click *Use the bundled copy*. The app turns on Developer mode
   and opens the extensions page for you. Then:
   - click **Load unpacked**;
   - paste the folder path (it's already copied to your clipboard).

   Leave Developer mode on. If Edge shows a "Turn off developer mode?" notice, close it with the **X**.
4. *(Optional)* Click *Open recording browser (not recorded)* and log in to any sites you plan to use.
   The recording browser starts out with no logins, and it keeps the ones you add here.

## 3. Record

1. Click **● Start recording**. The recording browser opens.
2. Browse in **that** window the way you normally would.
3. Click **❚❚ Pause** whenever you'd rather not be recorded. You can also pause from the extension icon
   in the browser toolbar. Nothing is recorded while you're paused.
4. When you're finished, click **■ Stop** or close the recording browser window. The session saves
   automatically.

**What gets recorded:** screenshots of the recording window, clicks, typed text (but not passwords),
scrolling, the pages you visit and their network traffic, and any files you upload or download in that
window.

## 4. Send me your recordings

Before you export, you can look through your sessions in the app and **delete** any you'd rather not
share.

Then click **Export new sessions**. This saves a `.zip` for each session to `~/SiteRecorder/exports`.
Upload those to the shared Torch folder (or send them however we agreed) and let me know when they're
there.

Each zip includes checksums, so I can tell if a file got damaged on the way.

---

## Command line

If you prefer the terminal, everything in the app works there too:

```
site-recorder setup | start | stop | pause | resume | status | list | export | delete ID | doctor
```

`site-recorder start` keeps recording until you close the recording browser or press Ctrl+C.

## Troubleshooting

If something isn't working, run **Setup → Run diagnostics** (or `site-recorder doctor`) and send me the
output. Some common problems:

- **"The extension has not connected"**: in Setup, make sure SITE Collector shows as installed and
  enabled. If Edge asks whether to "turn off extensions in developer mode", keep them on.
- **"Waiting for the extension…" right after Start**: this can take a few seconds the first time.
- **Windows Smart App Control**: the installer already works around the launchers it blocks. Just open
  the app from the **SITE Recorder** shortcut.
- **Logs**: the diagnostics output shows the app folder, and its logs are in `logs/` there. Each session
  folder also has its own `recorder_trace.log` and `extension_host.log`.

## Uninstall

```
site-recorder uninstall           # removes the browser connection and shortcuts
uv tool uninstall site-recorder   # removes the program itself
```

This keeps your recordings in `~/SiteRecorder`. To also delete the recording browser's profile, add
`--purge` to the first command.

---

## Maintainer notes (Sherry and me)

- **Building the download for participants:** run `python build_release.py`. This creates
  `dist/site-recorder-<version>.zip` (installers, a prebuilt wheel and this README), which is the file
  participants get. It also creates `dist/site-collector-<version>-webstore.zip` for publishing the
  extension to the Chrome Web Store.
- **Collecting recordings on the server:** see `server/README.md`. It checks each zip, copies the sessions
  into site-bench's `sessions/` folder, and generates the video, captions and title.
- **Design, verified platform behavior, known limitations and tests:** see `DEVELOPER_NOTES.md`. Run the
  unit tests with `uv run --with pytest pytest tests`. The manual tests that use a real browser are in
  `tests/manual/`.
