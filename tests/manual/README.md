# Manual real-browser tests

These scripts drive a real Chrome or Edge on a development machine. pytest does not collect them,
because their names don't start with `test_`. Run them from this folder, with the recorder installed in
a venv (`uv venv && uv pip install -e ..[dev]`). Set `SITE_RECORDER_HOME` to a scratch folder so nothing
touches a real installation:

```powershell
$env:SITE_RECORDER_HOME = "$PWD\_home"; $env:PYTHONUTF8 = "1"
python persistent_install.py edge          # one-time "Load unpacked" into the test profile (Windows: UI Automation)
python e2e.py edge 65 --persistent         # record the local test site, then pause/resume, stop, export, verify
python gui_smoke.py edge                   # drive the Tk app through setup check, start, pause, stop, export
```

- **`e2e.py`** without `--persistent` loads the extension temporarily over a DevTools pipe instead.
  That mode is quicker, but it has an extra DevTools client attached and sets `navigator.webdriver`, so
  production behaviour is only proven with `--persistent`.
- **`testsite.py`** serves a page that navigates within its own tab, then fetches JSON, posts data and
  loads CSS and an image. The e2e test checks that this traffic lands in the WARC with tab ids.
- **`persistent_install.py`** works like a participant using the bundled copy: it turns Developer mode on,
  then runs `developerPrivate.loadUnpacked()` and fills the folder picker with `uia.ps1` (Windows only).
