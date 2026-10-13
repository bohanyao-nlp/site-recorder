// Popup controller for SITE Collector — pause / resume recording.

const statusEl = document.getElementById("status");
const statusText = document.getElementById("status-text");
const toggleBtn = document.getElementById("toggle");
const hintEl = document.getElementById("hint");

function render(state) {
  const recording = !!(state && state.isRecording);
  const paused = !!(state && state.isPaused);

  statusEl.classList.remove("off", "recording", "paused");

  if (!recording) {
    statusEl.classList.add("off");
    statusText.textContent = "Not recording";
    toggleBtn.textContent = "No active session";
    toggleBtn.disabled = true;
    toggleBtn.classList.remove("resume");
    hintEl.textContent = "Start a SITE-Bench recording to control capture here.";
    return;
  }

  toggleBtn.disabled = false;
  if (paused) {
    statusEl.classList.add("paused");
    statusText.textContent = "Paused";
    toggleBtn.textContent = "▶  Resume recording";
    toggleBtn.classList.add("resume");
    hintEl.textContent = "Capture is stopped. Nothing is being recorded right now.";
  } else {
    statusEl.classList.add("recording");
    statusText.textContent = "Recording";
    toggleBtn.textContent = "❚❚  Pause recording";
    toggleBtn.classList.remove("resume");
    hintEl.textContent = "Pausing stops screenshots, network capture, and input events until you resume.";
  }
}

let lastErr = null;

function ask(action, extra) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(Object.assign({ action }, extra || {}), (resp) => {
      lastErr = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
      resolve(resp || null);
    });
  });
}

// Diagnostic line (screenshot-able) — shows the raw status + any messaging
// error so we can tell whether the popup reached the service worker.
function setDbg(state) {
  const dbg = document.getElementById("dbg");
  if (!dbg) return;
  const s = state || {};
  dbg.textContent =
    "rec=" + (s.isRecording === undefined ? "?" : s.isRecording) +
    " paused=" + (s.isPaused === undefined ? "?" : s.isPaused) +
    " build=" + (s.build || "STALE/none") +
    (lastErr ? "  ERR: " + lastErr : "");
}

toggleBtn.addEventListener("click", async () => {
  toggleBtn.disabled = true;
  const state = await ask("toggle_pause");
  render(state || {});
  setDbg(state);
});

(async () => {
  const state = await ask("get_status");
  render(state || {});
  setDbg(state);
})();
