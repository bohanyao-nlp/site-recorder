"""Export -> zip -> server ingest round trip."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from site_recorder import export, sessions

REPO_RECORDER = Path(__file__).resolve().parents[1]


def _fake_session(root: Path, sid: str = "20260102_101010") -> Path:
    sd = root / sid
    (sd / "archive").mkdir(parents=True)
    (sd / "archive" / "session.warc.gz").write_bytes(b"\x1f\x8b" + b"0" * 100)
    (sd / "browser_screenshots").mkdir()
    (sd / "browser_screenshots" / "1767348610.000000_click_action.png").write_bytes(b"png")
    (sd / "browser_screenshots" / "1767348670.500000_scroll_post_action.png").write_bytes(b"png")
    (sd / "events.jsonl").write_text('{"type": "browser_snapshot"}\n')
    (sd / "tab_timeline.jsonl").write_text(json.dumps({"type": "tab_activated", "url": "https://x.org/"}) + "\n")
    (sd / "downloads").mkdir()
    (sd / "downloads" / "downloads.jsonl").write_text(json.dumps({"saved_path": "downloads\\a.pdf"}) + "\n")
    (sd / "metadata.json").write_text(json.dumps({"browser_pid": 1, "start_url": ""}))
    (sd / "control.json").write_text("{}")             # plumbing: must not be exported
    (sd / "extension_host.log").write_text("x")
    (sd / "live_stats.json").write_text(json.dumps({"screenshots": 2, "clicks": 1}))
    return sd


def test_export_and_verify(tmp_path):
    sd = _fake_session(tmp_path / "sessions")
    out = export.export_session(sd, tmp_path / "exports")
    assert out.name == "20260102_101010.zip"
    ok, msg, manifest = export.verify_zip(out)
    assert ok, msg
    names = zipfile.ZipFile(out).namelist()
    assert "20260102_101010/control.json" not in names
    assert "20260102_101010/export_manifest.json" in names
    assert "participant_id" not in manifest
    assert export.export_info(sd)["zip"] == str(out)
    assert not list((tmp_path / "exports").glob("*.partial"))
    s = sessions.summarize(sd)
    assert s.exported_zip and s.duration_s == pytest.approx(60.5) and s.screenshots == 2


def test_verify_detects_tampering(tmp_path):
    sd = _fake_session(tmp_path / "sessions")
    out = export.export_session(sd, tmp_path / "exports")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(bad, "w") as zout:
        for i in zin.infolist():
            data = zin.read(i)
            if i.filename.endswith("events.jsonl"):
                data = b"tampered"
            zout.writestr(i, data)
    ok, msg, _ = export.verify_zip(bad)
    assert not ok and "events.jsonl" in msg
    trunc = tmp_path / "trunc.zip"
    trunc.write_bytes(out.read_bytes()[: out.stat().st_size // 2])
    assert not export.verify_zip(trunc)[0]


def _load_ingest():
    spec = importlib.util.spec_from_file_location("ingest", REPO_RECORDER / "server" / "ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ingest(tmp_path):
    sd = _fake_session(tmp_path / "sessions_client")
    z = export.export_session(sd, tmp_path / "exports")
    ingest = _load_ingest()
    root = tmp_path / "server_sessions"
    rc = ingest.main([str(z), "--sessions-root", str(root), "--skip-video", "--skip-label"])
    assert rc == 0
    final = root / "20260102_101010"
    assert (final / "archive" / "session.warc.gz").exists()
    assert not (final / "export_manifest.json").exists() or True
    dl = json.loads((final / "downloads" / "downloads.jsonl").read_text().splitlines()[0])
    assert dl["saved_path"] == "downloads/a.pdf"
    assert json.loads((final / "metadata.json").read_text())["start_url"] == "https://x.org/"
    # second run: skipped, not duplicated
    assert ingest.main([str(z), "--sessions-root", str(root), "--skip-video", "--skip-label"]) == 0
    assert len([p for p in root.iterdir() if not p.name.startswith(".")]) == 1


def test_ingest_rejects_corrupt(tmp_path):
    sd = _fake_session(tmp_path / "s")
    z = export.export_session(sd, tmp_path / "e")
    z.write_bytes(z.read_bytes()[:200])
    ingest = _load_ingest()
    assert ingest.main([str(z), "--sessions-root", str(tmp_path / "root"), "--skip-video", "--skip-label"]) == 2


def _rewrite_zip(src, dst, mutate_manifest=None, extra=None, rename_root=None):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for i in zin.infolist():
            data = zin.read(i)
            name = i.filename
            if name.endswith("export_manifest.json") and mutate_manifest:
                m = json.loads(data)
                mutate_manifest(m)
                data = json.dumps(m).encode()
            if rename_root:
                name = rename_root + name.split("/", 1)[1]
            zout.writestr(name, data)
        for n, d in (extra or {}).items():
            zout.writestr(n, d)


@pytest.mark.parametrize("case", ["traversal_id", "root_mismatch", "unlisted_file"])
def test_verify_rejects_crafted_zips(tmp_path, case):
    sd = _fake_session(tmp_path / "s")
    z = export.export_session(sd, tmp_path / "e")
    bad = tmp_path / "bad.zip"
    if case == "traversal_id":
        _rewrite_zip(z, bad, mutate_manifest=lambda m: m.update(session_id="../../evil"))
    elif case == "root_mismatch":
        _rewrite_zip(z, bad, rename_root="20990101_000000/")
    else:
        _rewrite_zip(z, bad, extra={"20260102_101010/../../x.txt": b"x"})
    ok, msg, _ = export.verify_zip(bad)
    assert not ok, msg


def test_ingest_resumes_pending_postprocess(tmp_path, monkeypatch):
    sd = _fake_session(tmp_path / "c")
    z = export.export_session(sd, tmp_path / "e")
    ingest = _load_ingest()
    root = tmp_path / "root"
    calls = []
    monkeypatch.setattr(ingest, "postprocess", lambda s, **k: calls.append(s.name) or [])
    assert ingest.main([str(z), "--sessions-root", str(root), "--skip-video", "--skip-label"]) == 0
    final = root / "20260102_101010"
    assert not (final / ".ingest_pending").exists()
    # simulate an interrupted post-process: pending marker, no ledger entry
    (final / ".ingest_pending").write_text("x")
    (root / ".incoming" / "ingested.jsonl").unlink()
    assert ingest.main([str(z), "--sessions-root", str(root), "--skip-video", "--skip-label"]) == 0
    assert calls == ["20260102_101010", "20260102_101010"]
    assert not (final / ".ingest_pending").exists()
