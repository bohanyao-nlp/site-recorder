"""The SITE Recorder window (Tk — ships with the uv-managed Python).

One window, two tabs:

* Record   — big Start / Pause / Stop, live counters, and the list of
             recorded sessions with Export / Delete / Open folder.
* Setup    — browser choice, extension installation
             (guided), diagnostics.

All slow work (starting the browser and waiting for the extension, stopping
and flushing, zipping) runs on worker threads; the Tk thread only polls a
result queue. The window also watches the recording browser: closing it ends
the session, exactly like pressing Stop.
"""
from __future__ import annotations

import queue
import shutil
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import __version__, browsers, config, export, paths, recorder, sessions, setup

POLL_MS = 700


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = config.load()
        self.q: queue.Queue = queue.Queue()
        # Two kinds of background work: LIFECYCLE (start/stop/recover — the
        # recording controls wait for it) and JOBS (export, diagnostics,
        # setup helpers — Pause/Stop keep working while they run).
        self.busy = False
        self.busy_label = ""
        self.job = False
        self.job_label = ""
        self._closing = False
        self._last_disk_check = 0.0
        self.browser_gone_since: float | None = None
        self._last_list_refresh = 0.0
        self._last_setup_refresh = 0.0

        root.title("SITE Recorder")
        root.minsize(760, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if sys.platform == "darwin":
            # ⌘Q / Dock › Quit / logout arrive as a quit Apple event, which
            # Tk turns into an immediate exit unless this command exists —
            # it must ask about a running recording like closing the window.
            root.createcommand("::tk::mac::Quit", self.on_close)
        root.report_callback_exception = self._report_callback_exception
        self._style()

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_rec = ttk.Frame(self.nb, padding=12)
        self.tab_setup = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.tab_rec, text="  Record  ")
        self.nb.add(self.tab_setup, text="  Setup  ")
        self._build_record_tab()
        self._build_setup_tab()

        footer = ttk.Frame(root, padding=(12, 0, 12, 8))
        footer.pack(fill="x")
        self.footer_lbl = ttk.Label(footer, text=f"site-recorder {__version__}", style="Dim.TLabel")
        self.footer_lbl.pack(side="left")

        # A crash of a previous app window must not end a running recording:
        # adopt it; a session whose browser is gone is finalized instead
        # (in the background — finalizing can take a while).
        def _startup():
            recorder.adopt_if_orphaned()
            return recorder.recover(self.cfg)
        self.run_bg("Checking the previous recording…", _startup)

        if not self.cfg.is_configured or recorder.Recorder(self.cfg).preflight():
            self.nb.select(self.tab_setup)
        self.refresh_sessions()
        self.refresh_setup()
        self.root.after(POLL_MS, self.poll)

    # ------------------------------------------------------------------ style
    def _style(self) -> None:
        s = ttk.Style(self.root)
        try:
            if "clam" in s.theme_names() and self.root.tk.call("tk", "windowingsystem") == "x11":
                s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Big.TButton", font=("TkDefaultFont", 12, "bold"), padding=(18, 10))
        s.configure("Status.TLabel", font=("TkDefaultFont", 15, "bold"))
        s.configure("Dim.TLabel", foreground="#6b7280")
        s.configure("Ok.TLabel", foreground="#15803d")
        s.configure("Bad.TLabel", foreground="#b91c1c")
        s.configure("H.TLabel", font=("TkDefaultFont", 11, "bold"))

    # ------------------------------------------------------------- record tab
    def _build_record_tab(self) -> None:
        t = self.tab_rec
        top = ttk.Frame(t)
        top.pack(fill="x")
        self.status_lbl = ttk.Label(top, text="Not recording", style="Status.TLabel")
        self.status_lbl.pack(side="left")
        self.who_lbl = ttk.Label(top, text="", style="Dim.TLabel")
        self.who_lbl.pack(side="right")

        self.detail_lbl = ttk.Label(t, text="", style="Dim.TLabel")
        self.detail_lbl.pack(fill="x", pady=(2, 10))

        btns = ttk.Frame(t)
        btns.pack(fill="x", pady=(0, 12))
        self.btn_start = ttk.Button(btns, text="●  Start recording", style="Big.TButton", command=self.on_start)
        self.btn_pause = ttk.Button(btns, text="❚❚  Pause", style="Big.TButton", command=self.on_pause)
        self.btn_stop = ttk.Button(btns, text="■  Stop", style="Big.TButton", command=self.on_stop)
        self.btn_start.pack(side="left")
        self.btn_pause.pack(side="left", padx=8)
        self.btn_stop.pack(side="left")

        hint = ("While recording, use the separate browser window that opens (your normal browser is "
                "not recorded). Close that window" + (" (or quit it with ⌘Q)" if sys.platform == "darwin" else "")
                + " or press Stop to finish. Pause any time for privacy.")
        ttk.Label(t, text=hint, wraplength=700, style="Dim.TLabel").pack(fill="x", pady=(0, 10))

        ttk.Label(t, text="Recorded sessions", style="H.TLabel").pack(anchor="w")
        cols = ("started", "length", "shots", "size", "status", "exported")
        frame = ttk.Frame(t)
        frame.pack(fill="both", expand=True, pady=(4, 6))
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=9, selectmode="extended")
        for c, w, txt in (("started", 150, "Started"), ("length", 80, "Length"), ("shots", 90, "Screenshots"),
                          ("size", 90, "Size"), ("status", 100, "Status"), ("exported", 90, "Exported")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w" if c in ("started", "status") else "center")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        row = ttk.Frame(t)
        row.pack(fill="x")
        ttk.Button(row, text="Export new sessions", command=self.on_export_new).pack(side="left")
        ttk.Button(row, text="Export selected", command=self.on_export_selected).pack(side="left", padx=6)
        ttk.Button(row, text="Open exports folder", command=lambda: setup.open_path(paths.exports_dir())).pack(side="left")
        ttk.Button(row, text="Delete selected", command=self.on_delete).pack(side="right")
        ttk.Button(row, text="Open sessions folder",
                   command=lambda: setup.open_path(self.cfg.sessions_path)).pack(side="right", padx=6)

    # -------------------------------------------------------------- setup tab
    def _build_setup_tab(self) -> None:
        t = self.tab_setup
        ttk.Label(t, text="1. Browser to record in", style="H.TLabel").grid(row=2, column=0, sticky="w")
        self.browser_var = tk.StringVar(value=self.cfg.browser or (browsers.installed_browsers() or ["chrome"])[0])
        bf = ttk.Frame(t)
        bf.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))
        for b in config.BROWSERS:
            rb = ttk.Radiobutton(bf, text=browsers.DISPLAY_NAME[b], value=b, variable=self.browser_var,
                                 command=self.on_save_settings)
            if not browsers.find_executable(b):
                rb.configure(state="disabled", text=browsers.DISPLAY_NAME[b] + " (not installed)")
            rb.pack(side="left", padx=(0, 16))
        ttk.Label(t, text="A separate recording profile of this browser is used — your everyday browser, "
                          "its tabs and accounts are never recorded.",
                  wraplength=680, style="Dim.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Button(t, text="Save", command=self.on_save_settings).grid(row=5, column=0, sticky="w", pady=(0, 12))

        ttk.Label(t, text="2. Install the SITE Collector extension (once)", style="H.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w")
        self.ext_status = ttk.Label(t, text="", style="Dim.TLabel")
        self.ext_status.grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self.ext_steps = ttk.Label(t, text="", wraplength=680, justify="left")
        self.ext_steps.grid(row=8, column=0, columnspan=2, sticky="w")
        eb = ttk.Frame(t)
        eb.grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 12))
        ttk.Button(eb, text="Install from Chrome Web Store",
                   command=self.on_install_store).pack(side="left")
        ttk.Button(eb, text="Use the bundled copy", command=self.on_install_bundled).pack(side="left", padx=6)
        ttk.Button(eb, text="Copy folder path", command=self.on_copy_ext_path).pack(side="left")
        ttk.Button(eb, text="Check again", command=self.refresh_setup).pack(side="left", padx=6)

        ttk.Label(t, text="3. Sign in to the websites you will use (optional)", style="H.TLabel").grid(
            row=10, column=0, columnspan=2, sticky="w")
        ttk.Label(t, text="The recording browser starts with no accounts. Open it (not recorded) and sign "
                          "in once to the sites your tasks need; it remembers them.",
                  wraplength=680, style="Dim.TLabel").grid(row=11, column=0, columnspan=2, sticky="w")
        ttk.Button(t, text="Open recording browser (not recorded)", command=self.on_open_browser).grid(
            row=12, column=0, sticky="w", pady=(4, 12))

        ttk.Separator(t).grid(row=13, column=0, columnspan=2, sticky="ew", pady=6)
        df = ttk.Frame(t)
        df.grid(row=14, column=0, columnspan=2, sticky="w")
        ttk.Button(df, text="Run diagnostics", command=self.on_doctor).pack(side="left")
        ttk.Label(df, text=f"  Recordings are saved in {self.cfg.sessions_path}", style="Dim.TLabel").pack(side="left")
        t.columnconfigure(1, weight=1)

    # ---------------------------------------------------------------- helpers
    def run_bg(self, label: str, fn, done=None, kind: str = "lifecycle") -> None:
        if (self.busy if kind == "lifecycle" else self.job):
            return
        if kind == "lifecycle":
            self.busy, self.busy_label = True, label
        else:
            self.job, self.job_label = True, label

        def worker():
            try:
                res = fn()
                self.q.put(("ok", kind, done, res))
            except Exception as e:  # surfaced in a dialog
                self.q.put(("err", kind, None, e))
        threading.Thread(target=worker, daemon=True).start()
        self.update_buttons()

    def _report_callback_exception(self, exc, val, tb) -> None:
        setup.log_exception(val)
        try:
            messagebox.showerror("SITE Recorder", f"Unexpected error: {val}\n\nDetails are in the log.",
                                 parent=self.root)
        except tk.TclError:
            pass

    def poll(self) -> None:
        # Whatever happens, the next poll is scheduled: a dead poll loop would
        # freeze the status and the browser-closed auto-stop.
        try:
            while True:
                try:
                    status, kind, done, res = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "lifecycle":
                    self.busy, self.busy_label = False, ""
                else:
                    self.job, self.job_label = False, ""
                try:
                    if status == "err":
                        msg = str(res) if isinstance(res, recorder.RecorderError) else \
                            f"{type(res).__name__}: {res}\n\nDetails were written to the log."
                        if not isinstance(res, recorder.RecorderError):
                            setup.log_exception(res)
                        messagebox.showerror("SITE Recorder", msg, parent=self.root)
                    elif done:
                        done(res)
                    self.refresh_sessions()
                    self.refresh_setup()
                except Exception as e:
                    setup.log_exception(e)
            self.tick()
        except Exception as e:
            setup.log_exception(e)
        finally:
            if self._closing and not self.busy:
                self.root.destroy()
                return
            self.root.after(POLL_MS, self.poll)

    def tick(self) -> None:
        # The Setup tab notices an extension install by itself (the browser
        # writes its preferences a few seconds after "Add extension").
        if self.nb.select() == str(self.tab_setup) and time.time() - self._last_setup_refresh > 3:
            self._last_setup_refresh = time.time()
            self.refresh_setup()
        m = recorder.active_session()
        self.who_lbl.configure(text=f"Browser: {browsers.DISPLAY_NAME.get(self.cfg.browser, '—')}")
        self.footer_lbl.configure(text=f"site-recorder {__version__}"
                                  + (f"   ·   {self.job_label}" if self.job else ""))
        if self.busy:
            self.status_lbl.configure(text=self.busy_label, foreground="")
        elif m:
            sd = Path(m["session_dir"])
            el = int(time.time() - float(m.get("started_at") or time.time()))
            paused = recorder.Recorder.is_paused()
            self.status_lbl.configure(
                text=("❚❚ Paused" if paused else "● Recording") + f"   {el // 3600}:{el // 60 % 60:02d}:{el % 60:02d}",
                foreground="#b45309" if paused else "#b91c1c")
            stats = recorder._read_json(sd / "live_stats.json") or {}
            conn = recorder.host_connected(sd)
            hb = recorder.host_heartbeat_age(sd)
            if not conn:
                cstate = "waiting for the extension to connect…"
            elif hb is not None and hb > 40:
                cstate = f"⚠ no heartbeat from the extension for {hb:.0f}s"
            else:
                cstate = "extension connected ✓"
            if not recorder.owned_by_me(m):
                cstate += " · controlled from a terminal"
            if time.time() - self._last_disk_check > 30:
                self._last_disk_check = time.time()
                try:
                    free = shutil.disk_usage(sd).free
                    self._low_disk = free < 500 * 2**20
                except OSError:
                    self._low_disk = False
            if getattr(self, "_low_disk", False):
                cstate += " · ⚠ DISK ALMOST FULL — stop soon"
            self.detail_lbl.configure(text=f"Session {sd.name} · {cstate} · "
                                           f"{stats.get('screenshots', 0)} screenshots, {stats.get('clicks', 0)} clicks, "
                                           f"{stats.get('inputs', 0)} inputs, {stats.get('scrolls', 0)} scrolls")
            # Closing the recording browser ends the session (only for the
            # session THIS window runs; a terminal recording handles its own).
            if m.get("state") == "recording" and recorder.owned_by_me(m) and recorder.browser_closed(m):
                self.browser_gone_since = self.browser_gone_since or time.time()
                if time.time() - self.browser_gone_since > 3:
                    self.browser_gone_since = None
                    sid = m.get("session_id")
                    self.run_bg("Saving…", lambda: recorder.stop_active(
                        close_browser=True, reason="recording browser closed", cfg=self.cfg,
                        expected_session=sid), done=self._after_stop)
            else:
                self.browser_gone_since = None
            if time.time() - self._last_list_refresh > 15:
                self.refresh_sessions()
        else:
            self.status_lbl.configure(text="Not recording", foreground="")
            self.detail_lbl.configure(text="")
        self.update_buttons()

    def update_buttons(self) -> None:
        m = recorder.active_session()
        ready = self.cfg.is_configured
        st = lambda b, on: b.configure(state="normal" if on else "disabled")
        st(self.btn_start, ready and not m and not self.busy and not self._closing)
        st(self.btn_stop, bool(m) and not self.busy)
        st(self.btn_pause, bool(m) and not self.busy and m.get("state") == "recording")
        self.btn_pause.configure(text="▶  Resume" if (m and recorder.Recorder.is_paused()) else "❚❚  Pause")

    def refresh_sessions(self) -> None:
        self._last_list_refresh = time.time()
        m = recorder.active_session()
        rows = sessions.list_sessions(self.cfg.sessions_path, m.get("session_id") if m else None)
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for s in rows:
            self.tree.insert("", "end", iid=s.id, values=(
                s.started_label, s.duration_label, s.screenshots, sessions.human_size(s.size_bytes),
                s.status, "yes" if s.exported_zip else "–"))
        for i in sel:
            if self.tree.exists(i):
                self.tree.selection_add(i)

    def refresh_setup(self) -> None:
        b = self.cfg.browser or self.browser_var.get()
        if b not in browsers.DISPLAY_NAME:
            return
        stt = browsers.extension_state(b)
        if stt["installed"] and stt["enabled"]:
            src = "Web Store" if stt.get("source") == "store" else "bundled copy"
            self.ext_status.configure(text=f"✓ Installed in the {browsers.DISPLAY_NAME[b]} recording profile"
                                           f" ({src}" + (f", version {stt['version']})" if stt["version"] else ")"),
                                      style="Ok.TLabel")
        elif stt.get("problem") == "developer_mode_off":
            self.ext_status.configure(text="✗ Disabled because Developer mode was turned off — click "
                                           "'Use the bundled copy' to switch it back on", style="Bad.TLabel")
        elif stt["installed"]:
            self.ext_status.configure(text="✗ Installed but DISABLED — enable it on the extensions page",
                                      style="Bad.TLabel")
        else:
            self.ext_status.configure(text="✗ Not installed yet", style="Bad.TLabel")
        self.ext_steps.configure(text=setup.extension_instructions(b))

    # ---------------------------------------------------------------- actions
    def on_save_settings(self) -> None:
        self.cfg.browser = self.browser_var.get()
        config.save(self.cfg)
        try:
            setup.prepare(self.cfg.browser)
        except Exception as e:
            setup.log_exception(e)
            messagebox.showerror("SITE Recorder", f"Setup failed: {e}", parent=self.root)
        self.refresh_setup()
        self.update_buttons()

    def on_install_store(self) -> None:
        self.on_save_settings()
        if not self.cfg.browser:
            return
        self.run_bg("Opening the Web Store…", lambda: setup.open_webstore_install(self.cfg.browser), kind="job")

    def on_install_bundled(self) -> None:
        self.on_save_settings()
        if not self.cfg.browser:
            return
        if browsers.is_running(self.cfg.browser) and not messagebox.askyesno(
                "SITE Recorder", "The recording browser is open and must restart for this step. "
                                 "Close it now?", parent=self.root):
            return
        ext = str(recorder.ensure_extension_copy())
        self.root.clipboard_clear()
        self.root.clipboard_append(ext)

        def done(msg):
            messagebox.showinfo("SITE Recorder", f"{msg}\n\n" + setup.load_unpacked_howto(),
                                parent=self.root)
        self.run_bg("Preparing the extensions page…",
                    lambda: setup.open_bundled_install(self.cfg.browser), done=done, kind="job")

    def on_copy_ext_path(self) -> None:
        p = str(recorder.ensure_extension_copy())
        self.root.clipboard_clear()
        self.root.clipboard_append(p)
        messagebox.showinfo("SITE Recorder", f"Copied:\n{p}", parent=self.root)

    def on_open_browser(self) -> None:
        self.on_save_settings()
        if recorder.active_session():
            messagebox.showinfo("SITE Recorder", "A recording is running — use its browser window.", parent=self.root)
            return
        self.run_bg("Opening browser…", lambda: setup.open_browser_unrecorded(self.cfg.browser), kind="job")

    def on_doctor(self) -> None:
        def done(text):
            win = tk.Toplevel(self.root)
            win.title("Diagnostics")
            txt = tk.Text(win, width=100, height=30, wrap="word")
            txt.insert("1.0", text)
            txt.configure(state="disabled")
            txt.pack(fill="both", expand=True)
        self.run_bg("Running diagnostics…", setup.doctor_report, done=done, kind="job")

    def on_start(self) -> None:
        problems = recorder.Recorder(self.cfg).preflight()
        if problems:
            messagebox.showwarning("SITE Recorder", "\n".join(problems), parent=self.root)
            self.nb.select(self.tab_setup)
            return

        def done(sd):
            if not recorder.host_connected(sd):
                messagebox.showwarning(
                    "SITE Recorder",
                    "The browser opened but the extension has not connected yet.\n\n"
                    "Recording continues and may connect shortly. If the status still says "
                    "'waiting', press Stop and run Setup → Run diagnostics.", parent=self.root)
        self.run_bg("Starting…", lambda: recorder.Recorder(self.cfg).start(), done=done)

    def on_pause(self) -> None:
        try:
            recorder.Recorder(self.cfg).set_paused(not recorder.Recorder.is_paused())
        except recorder.RecorderError as e:
            messagebox.showerror("SITE Recorder", str(e), parent=self.root)

    def on_stop(self) -> None:
        m = recorder.active_session()
        if not m:
            return
        sid = m.get("session_id")
        self.run_bg("Stopping and saving…", lambda: recorder.stop_active(cfg=self.cfg, expected_session=sid),
                    done=self._after_stop)

    def _after_stop(self, sd) -> None:
        if not sd:
            return
        s = sessions.summarize(Path(sd))
        if s.status == "empty":
            info = recorder._read_json(Path(sd) / "session_info.json") or {}
            messagebox.showwarning("SITE Recorder", f"Session {s.id} is empty.\n\n{info.get('note', '')}",
                                   parent=self.root)
        else:
            messagebox.showinfo("SITE Recorder",
                                f"Saved session {s.id}\n{s.duration_label} · {s.screenshots} screenshots · "
                                f"{sessions.human_size(s.size_bytes)}\n\n"
                                "Use 'Export new sessions' to create the .zip to send.", parent=self.root)

    def _export(self, ids: list[str]) -> None:
        m = recorder.active_session()
        ids = [i for i in ids if not (m and m.get("session_id") == i)]
        if not ids:
            messagebox.showinfo("SITE Recorder", "Nothing to export.", parent=self.root)
            return

        def work():
            outs = []
            for i in ids:
                self.job_label = f"Exporting {i}…"
                outs.append(export.export_session(self.cfg.sessions_path / i, paths.exports_dir()))
            return outs

        def done(outs):
            if messagebox.askyesno("SITE Recorder",
                                   f"Exported {len(outs)} session(s) to\n{paths.exports_dir()}\n\n"
                                   "Send the .zip file(s) there to the study team.\n\nOpen the folder now?",
                                   parent=self.root):
                setup.open_path(paths.exports_dir())
        self.run_bg("Exporting…", work, done=done, kind="job")

    def on_export_new(self) -> None:
        rows = sessions.list_sessions(self.cfg.sessions_path)
        self._export([s.id for s in rows if not s.exported_zip and s.status != "recording"])

    def on_export_selected(self) -> None:
        self._export(list(self.tree.selection()))

    def on_delete(self) -> None:
        ids = list(self.tree.selection())
        m = recorder.active_session()
        ids = [i for i in ids if not (m and m.get("session_id") == i)]
        if not ids:
            return
        if self.job and self.job_label.startswith("Export"):
            messagebox.showinfo("SITE Recorder", "Wait until the export has finished.", parent=self.root)
            return
        if not messagebox.askyesno("SITE Recorder", f"Permanently delete {len(ids)} session(s)?\n"
                                   "Exported .zip files are kept.", parent=self.root):
            return
        import shutil
        for i in ids:
            if sessions.SESSION_ID_RE.match(i):
                shutil.rmtree(self.cfg.sessions_path / i, ignore_errors=True)
        self.refresh_sessions()

    def on_close(self) -> None:
        if self._closing:
            return
        if self.busy:
            messagebox.showinfo("SITE Recorder", f"Please wait — {self.busy_label}", parent=self.root)
            return
        m = recorder.active_session()
        if m and recorder.owned_by_me(m):
            if not messagebox.askyesno("SITE Recorder",
                                       "A recording is in progress. Stop it and quit?", parent=self.root):
                return
            self._closing = True
            sid = m.get("session_id")
            self.run_bg("Stopping and saving before quitting…",
                        lambda: recorder.stop_active(cfg=self.cfg, expected_session=sid))
            return                      # poll() closes the window once saved
        if self.job and not messagebox.askyesno(
                "SITE Recorder", f"Still working ({self.job_label}). Quit anyway?", parent=self.root):
            return
        self.root.destroy()


def main() -> int:
    paths.ensure_dirs()
    lock = setup.single_instance_lock("gui")
    if lock is None:
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo("SITE Recorder", "SITE Recorder is already open.")
            r.destroy()
        except tk.TclError:
            print("SITE Recorder is already open.")
        return 0
    root = tk.Tk()
    try:
        icon = Path(__file__).parent / "extension" / "icon_128.png"
        if icon.exists():
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except tk.TclError:
        pass
    try:
        App(root)
        root.mainloop()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
