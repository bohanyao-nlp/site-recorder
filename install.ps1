# SITE Recorder installer for Windows.
#
#   Double-click install.bat   (or: powershell -ExecutionPolicy Bypass -File install.ps1)
#
# Safe to re-run (also how you upgrade: unzip the new release, run it again).
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if it is missing
#   2. installs the recorder as a uv "tool" with its own private Python
#   3. registers the recorder with Chrome/Edge, adds Start-menu + desktop
#      shortcuts, then opens the app for the remaining setup
#
# (Keep this file ASCII-only: Windows PowerShell 5.1 reads scripts without a
# BOM in the ANSI code page.)

# 'Continue', not 'Stop': in Windows PowerShell 5.1 a native command that writes
# to stderr (or is blocked by Smart App Control) would otherwise abort the whole
# script. Every native call below is checked through $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
# uv prints UTF-8; without this, PowerShell 5.1 decodes its output (e.g. tool
# paths under C:\Users\Jose with an accent) in the OEM code page and garbles them.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$here = $PSScriptRoot

function Say($msg)  { Write-Host "[site-recorder] $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[site-recorder] $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path (Join-Path $here 'pyproject.toml'))) { Fail 'Run this from the unzipped site-recorder folder.' }

# Always use a uv-managed Python (it includes Tk for the app window). Set
# ONLY UV_PYTHON_PREFERENCE: uv refuses to run when it is combined with
# UV_MANAGED_PYTHON / UV_NO_MANAGED_PYTHON, which a participant may have set.
if ($env:UV_PYTHON_PREFERENCE -ne 'system') { $env:UV_PYTHON_PREFERENCE = 'only-managed' }
Remove-Item Env:UV_MANAGED_PYTHON, Env:UV_NO_MANAGED_PYTHON -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = '1'

# --- 1. uv ---------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    foreach ($d in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
        if (Test-Path "$d\uv.exe") { $env:Path = "$d;$env:Path" }
    }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'Installing uv...'
    try {
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    } catch { Fail "uv install failed: $_  (install it yourself: https://docs.astral.sh/uv/)" }
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv is not on PATH after installing it - open a new terminal and re-run.' }
Say ("Using " + (uv --version))
# Trust the OS certificate store (university networks often inspect TLS).
# Newer uv calls this UV_SYSTEM_CERTS (UV_NATIVE_TLS is deprecated there).
if ((uv --help 2>$null | Out-String) -match '--system-certs') {
    $env:UV_SYSTEM_CERTS = '1'; Remove-Item Env:UV_NATIVE_TLS -ErrorAction SilentlyContinue
} else {
    $env:UV_NATIVE_TLS = '1'
}

$toolRoot = Join-Path (uv tool dir).Trim() 'site-recorder'
$toolPy  = Join-Path $toolRoot 'Scripts\python.exe'
$toolPyw = Join-Path $toolRoot 'Scripts\pythonw.exe'

# --- upgrading? never replace the program under a running app/recording ------
$appDir = Join-Path $env:LOCALAPPDATA 'SiteRecorder'
if ($env:SITE_RECORDER_HOME) { $appDir = Join-Path $env:SITE_RECORDER_HOME 'app' }
if (Test-Path (Join-Path $appDir 'active_session.json')) {
    Fail 'A recording is in progress - stop it in SITE Recorder first, then run the installer again.'
}
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($toolRoot, [StringComparison]::OrdinalIgnoreCase) })
if ($running.Count -gt 0) {
    Fail 'SITE Recorder is still running - close its window (and the recording browser), then run the installer again.'
}

# --- 2. the recorder ------------------------------------------------------------
# A release zip carries a prebuilt wheel (no build step on the participant's
# machine); a source checkout is built with pinned build tools.
$wheel = Get-ChildItem -Path (Join-Path $here 'wheel') -Filter '*.whl' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wheel) { $source = $wheel.FullName } else { $source = $here }
$constraints = Join-Path $here 'constraints.txt'

# Windows Smart App Control (on by default on new Windows 11 installs) only lets
# executables run that Microsoft has seen before. A brand-new Python build from
# uv can be blocked ("Querying Python ... exit code 0xc0e90002"), so try several
# builds and keep the first one whose console AND windowed interpreters run
# (with Tk, the compiled dependencies and the recorder itself).
# On Windows-on-ARM, use x64 builds (they run emulated; every dependency ships
# x64 wheels, not all have ARM64 ones).
$arm = ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64')
$versions = @('3.12.14', '3.13', '3.11')
$installed = $false
foreach ($v in $versions) {
    $req = $v
    if ($arm) { $req = "cpython-$v-windows-x86_64-none" }
    Say "Installing SITE Recorder with Python $v (the first run downloads ~20 MB)..."
    uv tool install --reinstall --python $req --constraints $constraints --build-constraints $constraints "$source"
    if ($LASTEXITCODE -ne 0) { Say "  Python $v did not work here - trying another build."; continue }
    $probeOk = $false
    try {
        & $toolPy -c "import tkinter, psutil, PIL.Image, warcio, site_recorder.cli" 2>&1 | Out-Null
        $probeOk = ($LASTEXITCODE -eq 0)
        if ($probeOk) {
            # The app runs through pythonw.exe - a different binary that Smart
            # App Control judges separately.
            # (One pre-quoted argument string: PowerShell 5.1 joins an array
            # unquoted, which would run 'pythonw -c import tkinter'.)
            $p = Start-Process -FilePath $toolPyw -ArgumentList '-c "import tkinter"' -Wait -PassThru -WindowStyle Hidden
            $probeOk = ($p.ExitCode -eq 0)
        }
    } catch { $probeOk = $false }
    if ($probeOk) { $installed = $true; break }
    Say "  Python $v installed but cannot run here (blocked?) - trying another build."
}
if (-not $installed) {
    $which = 'the 64-bit (x64) installer'
    Fail ("Could not get a working Python. If Windows Smart App Control is on, install Python 3.12 " +
          "from https://www.python.org/downloads/windows/ ($which, signed by the PSF), then run: " +
          "`$env:UV_PYTHON_PREFERENCE='system'; .\install.ps1")
}

$bin = (uv tool dir --bin).Trim()
uv tool update-shell 2>$null | Out-Null

# --- 3. browser registration + shortcuts ----------------------------------------
# Run the tool's own interpreter, never uv's generated site-recorder*.exe
# launchers: Smart App Control blocks those unsigned one-off binaries.
& $toolPy -m site_recorder.cli post-install --bin-dir "$bin"
if ($LASTEXITCODE -ne 0) { Fail 'post-install step failed (see above).' }

Say ''
Say 'Installed. Next: finish setup in the app (browser, extension).'
Say "Start it any time from the Start menu or desktop: 'SITE Recorder'."
if (-not $env:SITE_RECORDER_NO_LAUNCH) {
    Say 'Opening SITE Recorder...'
    # From the home folder and without the installer's settings, exactly as
    # the shortcut starts it (the unzipped folder must stay deletable).
    foreach ($n in 'PYTHONUTF8', 'UV_PYTHON_PREFERENCE', 'UV_SYSTEM_CERTS', 'UV_NATIVE_TLS') {
        Remove-Item "Env:$n" -ErrorAction SilentlyContinue
    }
    Start-Process -FilePath $toolPyw -ArgumentList '-m', 'site_recorder.gui' -WorkingDirectory $env:USERPROFILE
}
