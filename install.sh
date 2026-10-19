#!/bin/sh
# SITE Recorder installer for macOS and Linux.
#
#   sh install.sh
#
# Safe to re-run (also how you upgrade: unzip the new release, run it again).
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if it is missing
#   2. installs the recorder as a uv "tool" with its own private Python 3.13
#      (nothing touches your system Python)
#   3. registers the recorder with Chrome/Edge and adds a "SITE Recorder"
#      app shortcut, then opens the app for the remaining setup
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
say()  { printf '\033[1;36m[site-recorder]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[site-recorder]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$HERE/pyproject.toml" ] || fail "Run this script from the unzipped site-recorder folder."

# Always use a uv-managed Python (it includes Tk for the app window). Set
# ONLY UV_PYTHON_PREFERENCE: uv refuses to run when it is combined with
# UV_MANAGED_PYTHON / UV_NO_MANAGED_PYTHON, which a participant may have set.
[ "${UV_PYTHON_PREFERENCE:-}" = "system" ] || export UV_PYTHON_PREFERENCE=only-managed
unset UV_MANAGED_PYTHON UV_NO_MANAGED_PYTHON 2>/dev/null || true
export PYTHONUTF8=1

# --- 1. uv ------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$d/uv" ]; then PATH="$d:$PATH"; export PATH; fi
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv…"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    fail "Need curl or wget to install uv (or install uv yourself: https://docs.astral.sh/uv/)."
  fi
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi
command -v uv >/dev/null 2>&1 || fail "uv is not on PATH after installing it — open a new terminal and re-run."
say "Using $(uv --version)"
# Trust the OS certificate store (university networks often inspect TLS).
# Newer uv calls this UV_SYSTEM_CERTS (UV_NATIVE_TLS is deprecated there).
if uv --help 2>/dev/null | grep -q -- "--system-certs"; then
  export UV_SYSTEM_CERTS=1; unset UV_NATIVE_TLS 2>/dev/null || true
else
  export UV_NATIVE_TLS=1
fi

BIN="$(uv tool dir --bin)"
TOOL_ROOT="$(uv tool dir)/site-recorder"
TOOL_PY="$TOOL_ROOT/bin/python"

# --- upgrading? never replace the program under a running app/recording ----
if [ -n "${SITE_RECORDER_HOME:-}" ]; then
  APP_DIR="$SITE_RECORDER_HOME/app"
elif [ "$(uname -s)" = "Darwin" ]; then
  APP_DIR="$HOME/Library/Application Support/SiteRecorder"
else
  APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/site-recorder"
fi
[ -f "$APP_DIR/active_session.json" ] && \
  fail "A recording is in progress — stop it in SITE Recorder first, then run the installer again."
if command -v pgrep >/dev/null 2>&1 && pgrep -f "site_recorder\.gui|site-recorder-gui" >/dev/null 2>&1; then
  fail "SITE Recorder is still running — quit it, then run the installer again."
fi

# --- 2. the recorder --------------------------------------------------------
# A release zip carries a prebuilt wheel (no build step on the participant's
# machine); a source checkout is built with pinned build tools.
SOURCE="$HERE"
for w in "$HERE"/wheel/*.whl; do
  if [ -f "$w" ]; then SOURCE="$w"; fi
done

# Keep the first Python build that installs AND runs (interpreter + Tk + the
# compiled dependencies + the recorder). 3.13 first: on macOS/Linux the
# managed Python ships Tk 9, which needs >= 3.13 for safe threading — tried
# twice so a network blip does not push us to 3.12.
ok=""
for v in 3.13 3.13 3.12; do
  say "Installing SITE Recorder with Python $v (the first run downloads ~30 MB)…"
  if uv tool install --reinstall --python "$v" --constraints "$HERE/constraints.txt" \
       --build-constraints "$HERE/constraints.txt" "$SOURCE"; then
    if "$TOOL_PY" -c "import psutil, PIL.Image, warcio, site_recorder.cli" >/dev/null 2>&1; then
      ok=1; break
    fi
  fi
  say "  Python $v did not work here — trying again / another build."
done
[ -n "$ok" ] || fail "uv tool install failed (see above)."
if [ "$v" = "3.12" ]; then
  say "Note: using Python 3.12 (3.13 was not available). The app works; if its window"
  say "      misbehaves, use the command line: site-recorder start / stop / export"
fi
uv tool update-shell >/dev/null 2>&1 || true

# Tk check: the app window needs it (the uv-managed Python ships it).
if ! "$TOOL_PY" -c "import tkinter" >/dev/null 2>&1; then
  say "Note: this Python has no Tk — the app window won't open, but the"
  say "      command line works:  site-recorder start / stop / export"
fi

# --- 3. browser registration + shortcuts -------------------------------------
"$TOOL_PY" -m site_recorder.cli post-install || fail "post-install step failed (see above)."

say ""
say "Installed. Next: finish setup in the app (browser, extension)."
case "$(uname -s)" in
  Darwin) say "The app is in ~/Applications/SITE Recorder.app (also: run 'site-recorder')." ;;
  *)      say "Find 'SITE Recorder' in your applications menu (also: run 'site-recorder')." ;;
esac

if [ "${SITE_RECORDER_NO_LAUNCH:-}" = "" ]; then
  if [ "$(uname -s)" = "Darwin" ] || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    say "Opening SITE Recorder…"
    # From the home folder and without the installer's settings, exactly as
    # the shortcut starts it.
    (cd "$HOME" && env -u PYTHONUTF8 -u UV_PYTHON_PREFERENCE -u UV_SYSTEM_CERTS -u UV_NATIVE_TLS \
       nohup "$BIN/site-recorder-gui" >/dev/null 2>&1 &)
  else
    say "No display detected — run 'site-recorder setup' for a terminal-only setup."
  fi
fi
