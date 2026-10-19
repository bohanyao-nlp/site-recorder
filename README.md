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
- download this repo as a zip (**Code → Download ZIP**) and unzip it, or
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

To update to a new version, close SITE Recorder (stop any recording first), get the new version the
same way and run the installer again. Your existing recordings won't be affected.

## 2. One-time setup (in the app's *Setup* tab)

1. **Browser**: pick Chrome or Edge.
2. [**Install the SITE Collector extension**](https://chromewebstore.google.com/detail/site-collector/gbhaoocbgjdighlaaflhgfbmaahgmjaa).

   *If the Web Store link doesn't work:* click *Use the bundled copy*. The app turns on Developer mode
   and opens the extensions page for you. Then:
   - click **Load unpacked**;
   - paste the folder path (it's already copied to your clipboard).

   Leave Developer mode on. If Edge shows a "Turn off developer mode?" notice, close it with the **X**.
3. *(Optional)* Click *Open recording browser (not recorded)* and log in to any sites you plan to use.
   The recording browser starts out with no logins, and it keeps the ones you add here.

## 3. Record

1. Click **● Start recording**. The recording browser opens.
2. Browse in **that** window the way you normally would.
3. Click **❚❚ Pause** whenever you'd rather not be recorded. You can also pause from the extension icon
   in the browser toolbar. Nothing is recorded while you're paused.
4. When you're finished, click **■ Stop** or close the recording browser window. The session saves
   automatically.

**What gets recorded:** screenshots of the recording window, clicks, typed text, scrolling, the pages
you visit and their network traffic, and any files you upload or download in that window.

**What doesn't:** anything you type into password, card-number or one-time-code fields. Those fields are
blanked out in screenshots, and what you typed is removed from the network traffic. Login cookies and
tokens are replaced by meaningless placeholders. Password-manager websites are never recorded. To skip
other sites, add them to `"skip_domains"` in the app folder's `config.json` (for example
`"mybank.com, mail.example.org"`).

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

**How it works.** The app (`gui.py`, or the CLI in `cli.py`) starts Chrome/Edge with its own profile in
the app folder, so the participant's everyday browser is never touched. That profile is launched with
`--disable-sync`, because Edge would otherwise sign it into the Windows account. The SITE Collector
extension captures screenshots, events, DOM snapshots and network traffic, and sends them over native
messaging to `host.py`, which writes the session folder (WARC, screenshots, `events.jsonl` and so on).
The host only accepts a browser running the recording profile while a session is active.
`export.py` zips a session with a SHA-256 checksum for every file.

**Collecting recordings.** Ingesting happens in the site-bench repo. It verifies each zip, copies the
session into `sessions/` and generates the video, captions and title:
`PYTHONUTF8=1 uv run python -m site_bench.record.ingest <zips or folder> --sessions-root sessions`.

**Versions.** Bump `pyproject.toml` and `src/site_recorder/__init__.py` together. Bump
`src/site_recorder/extension/manifest.json` whenever the extension changes. For a Chrome Web Store
upload, zip the `extension/` folder with the manifest's `"key"` removed, because the store rejects keyed
packages. The key keeps the unpacked copy's ID equal to the store ID. The listing is owned by valteu's
account, and the store currently has 1.7.1.

**Tests.** `uv run --with pytest pytest tests`.

**Known limitations.**
- macOS and Linux were reviewed but never run on real hardware.
- When a tab opens straight to a URL, its first page can load before the debugger attaches. Its network
  records are then missing, but the DOM snapshot still captures the page.
- With the bundled (unpacked) extension, Edge shows a "Turn off developer mode?" notice at each start.
- Smart App Control can block brand-new Python builds, so `install.ps1` tries 3.12.14, then 3.13, then
  3.11.
