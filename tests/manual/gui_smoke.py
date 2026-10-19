"""Drive the real Tk app: setup tab refresh, Start, wait, Stop, Export. Fails loudly."""
import sys
import tkinter as tk
from tkinter import messagebox

import testsite
from site_recorder import config, gui, recorder, sessions

SRV = testsite.start(8766)
cfg = config.load()
cfg.browser, cfg.start_url = sys.argv[1], "http://127.0.0.1:8766/a"
config.save(cfg)

# Dialogs would block an unattended run: record them instead.
shown = []
for name in ("showinfo", "showwarning", "showerror"):
    setattr(messagebox, name, lambda title, msg, _n=name, **k: shown.append((_n, msg)) or "ok")
messagebox.askyesno = lambda *a, **k: shown.append(("askyesno", a[1] if len(a) > 1 else "")) or False

root = tk.Tk()
app = gui.App(root)
log = []
errors = []


def step(fn, delay):
    root.after(int(delay * 1000), lambda: _safe(fn))


def _safe(fn):
    try:
        fn()
    except Exception:
        import traceback
        errors.append(traceback.format_exc())


def check_setup():
    log.append(("setup status", app.ext_status.cget("text")))


def start():
    app.nb.select(app.tab_rec)
    app.on_start()
    log.append(("after start click", app.status_lbl.cget("text")))


def mid():
    log.append(("recording", app.status_lbl.cget("text"), app.detail_lbl.cget("text")))
    app.on_pause()


def resume():
    log.append(("paused?", app.status_lbl.cget("text")))
    app.on_pause()


def stop():
    log.append(("before stop", app.status_lbl.cget("text")))
    app.on_stop()


def export():
    log.append(("after stop", app.status_lbl.cget("text"), [s[0] for s in shown]))
    app.on_export_new()


def finish():
    log.append(("sessions", [(s.id, s.status, s.exported_zip is not None) for s in sessions.list_sessions(cfg.sessions_path)][:3]))
    log.append(("dialogs", shown))
    root.destroy()


app.nb.select(app.tab_setup)
step(check_setup, 4)
step(start, 5)
step(mid, 30)
step(resume, 34)
step(stop, 40)
step(export, 70)
step(finish, 85)
root.mainloop()
for l in log:
    print(l)
print("ERRORS:", errors or "none")
print("active:", recorder.active_session())
