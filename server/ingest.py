#!/usr/bin/env python3
"""Ingest participant recordings (.zip files from SITE Recorder) on the server.

Run from the site-bench repo (it uses the repo's environment for the video,
captions and title):

    uv run python ../site-recorder/server/ingest.py PATH [PATH ...] [--sessions-root sessions]

PATH may be a .zip or a folder of .zip files. For each zip:

  1. verify every file against the checksums in export_manifest.json
  2. extract (zip-slip safe) into <sessions-root>/.incoming/
  3. normalize: forward-slash relative paths in the download/local-file
     manifests, drop recorder plumbing, make sure metadata.json has a start_url
  4. move into <sessions-root>/<YYYYMMDD_HHMMSS>_<participant> — refusing to
     overwrite; the participant suffix keeps sessions of different
     participants that started in the same second apart (the folder name keys
     the batch workspace, resume and library provenance downstream)
  5. post-process exactly like `site-bench record` does at the end of a
     session: browser_recording.mp4, captions (Claude Code / Codex login),
     summary.json — skippable with --skip-video / --skip-label

Already-ingested zips are skipped (matched by the manifest's participant +
session id), so re-running over a folder of zips is safe.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORDER_SRC = HERE.parent / "src" / "site_recorder"

# Recorder plumbing with no reader on the server (and absolute local paths).
DROP_FILES = {"control.json", "status.json", "stop.request", ".host_alive", ".host.lock",
              ".host_done", "nm_host.bat", "nm_host.sh", "com.sitebench.collector.json",
              ".export_info.json", "finalize_status.json", ".warc_committed",
              ".browser_windows_closed"}


def _load_export_module():
    """src/site_recorder/export.py (this repo) is stdlib-only; load it by path so
    the server does not need the recorder package installed."""
    # export.py does `from . import __version__`; give it a tiny parent package.
    import types
    pkg = types.ModuleType("_site_recorder_pkg")
    pkg.__path__ = [str(RECORDER_SRC)]
    pkg.__version__ = "server"
    sys.modules["_site_recorder_pkg"] = pkg
    spec = importlib.util.spec_from_file_location("_site_recorder_pkg.export", RECORDER_SRC / "export.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_site_recorder_pkg.export"] = mod
    spec.loader.exec_module(mod)
    return mod


EXPORT = _load_export_module()
SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def log(msg: str) -> None:
    print(f"[ingest] {msg}", flush=True)


def _ledger_path(root: Path) -> Path:
    return root / ".incoming" / "ingested.jsonl"


def _already_ingested(root: Path) -> dict:
    out = {}
    p = _ledger_path(root)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                out[(d["participant_id"], d["session_id"])] = d
            except (ValueError, KeyError):
                pass
    return out


def _record_ingest(root: Path, entry: dict) -> None:
    p = _ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"unsafe path in zip: {info.filename}")
    zf.extractall(dest)


def _posixify_jsonl(path: Path, keys=("saved_path",)) -> None:
    if not path.exists():
        return
    lines = []
    changed = False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        for k in keys:
            if isinstance(d.get(k), str) and "\\" in d[k]:
                d[k] = d[k].replace("\\", "/")
                changed = True
        lines.append(json.dumps(d))
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _first_http_url(session: Path) -> str:
    tl = session / "tab_timeline.jsonl"
    if tl.exists():
        for line in tl.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                u = json.loads(line).get("url") or ""
            except ValueError:
                continue
            if u.startswith("http"):
                return u
    return "https://google.com"


def normalize(session: Path) -> None:
    for name in DROP_FILES:
        try:
            (session / name).unlink()
        except FileNotFoundError:
            pass
    _posixify_jsonl(session / "downloads" / "downloads.jsonl")
    _posixify_jsonl(session / "local_files.jsonl")
    meta_p = session / "metadata.json"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not meta.get("start_url"):
        meta["start_url"] = _first_http_url(session)
    meta_p.write_text(json.dumps(meta), encoding="utf-8")


def check_server_env(video: bool, label: bool, agent: str | None) -> str:
    """Fail fast (before touching any zip) when the repo modules the
    post-processing needs are missing — otherwise every session would be
    ingested silently without video/captions."""
    missing = []
    if video:
        try:
            import site_bench.postprocess  # noqa: F401
        except Exception as e:  # noqa: BLE001
            missing.append(f"site_bench.postprocess ({e})")
    if label:
        for mod in ("site_bench.labeler", "site_bench.agents.llm", "site_bench.task.summarize"):
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001
                missing.append(f"{mod} ({e})")
        if not missing:
            from site_bench.agents import llm
            if not llm.is_available(agent):
                missing.append(f"the {agent or llm.default_agent()} SDK/login for captions")
    if missing:
        return ("cannot post-process here — missing: " + "; ".join(missing)
                + ". Run from the site-bench repo (uv run …), or pass --skip-video / --skip-label.")
    return ""


def postprocess(session: Path, *, video: bool, label: bool, model: str | None, agent: str | None) -> list[str]:
    """Video + captions + title, as `site-bench record` does after a session."""
    problems = []
    if video:
        try:
            from site_bench.postprocess import create_browser_video
            if not create_browser_video(Path(session)):
                problems.append("video: no frames")
        except Exception as e:  # noqa: BLE001 - report and continue
            problems.append(f"video failed: {e}")
    if label:
        try:
            from site_bench.agents import llm
            if not llm.is_available(agent):
                problems.append("captions skipped: no Claude Code / Codex SDK importable here")
                return problems
            from site_bench import labeler
            frames = labeler.frames(session)
            chunk = int(os.environ.get("SITE_BENCH_LABEL_CHUNK", labeler.DEFAULT_CHUNK))
            # skip_already_labeled: a resumed ingest keeps the chunks it already has
            labeler.run_labeler(session, chunk_frames=chunk, agent=agent, model=model,
                                skip_already_labeled=True)
            expected = math.ceil(len(frames) / chunk) if frames else 0
            got = len(list((session / "captions").glob("*_captions.jsonl")))
            if got < expected:
                # one retry of only the missing chunks
                labeler.run_labeler(session, chunk_frames=chunk, agent=agent, model=model,
                                    skip_already_labeled=True)
                got = len(list((session / "captions").glob("*_captions.jsonl")))
            if got < expected:
                problems.append(f"captions incomplete: {got}/{expected} chunks")
            caps = session / "captions.jsonl"
            if caps.exists() and caps.stat().st_size > 0:
                from site_bench.task.summarize import run_summarize
                run_summarize(session, model=model, overwrite=True)
            else:
                problems.append("summary skipped: no captions")
        except Exception as e:  # noqa: BLE001
            problems.append(f"labeling failed: {e}")
    return problems


def ingest_zip(zip_path: Path, root: Path, args, seen: dict) -> str:
    ok, msg, manifest = EXPORT.verify_zip(zip_path)
    if not ok:
        log(f"REJECTED {zip_path.name}: {msg}")
        return "rejected"
    pid = SAFE.sub("-", manifest.get("participant_id") or "anon").strip("-") or "anon"
    sid = manifest["session_id"]
    if (pid, sid) in seen and not args.force:
        log(f"skip {zip_path.name}: already ingested as {seen[(pid, sid)].get('folder')}")
        return "skipped"
    final = root / f"{sid}_{pid}"
    if final.exists() and (final / ".ingest_pending").exists() and not args.force:
        # A previous run was interrupted during post-processing: resume it.
        log(f"resuming post-processing of {final.name}")
        return _postprocess_and_record(final, zip_path, pid, sid, root, args, seen, msg="resumed")
    if final.exists():
        if not args.force:
            log(f"skip {zip_path.name}: {final} already exists (use --force to replace)")
            return "skipped"
        shutil.rmtree(final)

    incoming = root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ingest-", dir=incoming))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract(zf, tmp)
        src = tmp / sid
        if not (src / "archive" / "session.warc.gz").exists():
            log(f"WARNING {zip_path.name}: no archive/session.warc.gz — the pipeline will not treat "
                "this as a session (the recording was probably empty)")
        normalize(src)
        (src / ".ingest_pending").write_text(str(time.time()))
        os.replace(src, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"ingested {zip_path.name} -> {final} ({msg})")
    return _postprocess_and_record(final, zip_path, pid, sid, root, args, seen, msg=msg)


def _postprocess_and_record(final: Path, zip_path: Path, pid: str, sid: str, root: Path,
                            args, seen: dict, msg: str) -> str:
    problems = postprocess(final, video=not args.skip_video, label=not args.skip_label,
                           model=args.model, agent=args.agent)
    try:
        (final / ".ingest_pending").unlink()
    except FileNotFoundError:
        pass
    for p in problems:
        log(f"  ! {p}")
    entry = {"participant_id": pid, "session_id": sid, "folder": final.name,
             "zip": str(zip_path), "ingested_at": time.time(), "problems": problems}
    _record_ingest(root, entry)
    seen[(pid, sid)] = entry
    return "ok" if not problems else "ok-with-problems"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", type=Path, help=".zip files or folders containing them")
    ap.add_argument("--sessions-root", type=Path, default=Path("sessions"))
    ap.add_argument("--skip-video", action="store_true")
    ap.add_argument("--skip-label", action="store_true", help="no captions / summary")
    ap.add_argument("--agent", default=None, help="captioning agent (claude | codex)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--force", action="store_true", help="re-ingest and replace existing folders")
    args = ap.parse_args(argv)

    os.environ.setdefault("PYTHONUTF8", "1")
    if args.agent:
        # run_summarize and other default-agent callers read this.
        os.environ["SITE_BENCH_LLM_AGENT"] = args.agent
    problem = check_server_env(not args.skip_video, not args.skip_label, args.agent)
    if problem:
        log(problem)
        return 3
    root = args.sessions_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    zips: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            zips += sorted(p.glob("*.zip"))
        elif p.suffix.lower() == ".zip" and p.exists():
            zips.append(p)
        else:
            log(f"not a .zip or folder: {p}")
    if not zips:
        log("nothing to ingest")
        return 1
    seen = _already_ingested(root)
    results = {}
    for z in zips:
        try:
            r = ingest_zip(z, root, args, seen)
        except Exception as e:  # noqa: BLE001 - one bad zip must not stop the batch
            log(f"FAILED {z.name}: {e}")
            r = "failed"
        results[r] = results.get(r, 0) + 1
    log("done: " + ", ".join(f"{k}={v}" for k, v in sorted(results.items())))
    return 0 if not (results.get("rejected") or results.get("failed")) else 2


if __name__ == "__main__":
    sys.exit(main())
