"""Export: session folder -> <session>.zip with a checksummed manifest."""
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from site_recorder import export, sessions


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


def test_export(tmp_path):
    sd = _fake_session(tmp_path / "sessions")
    out = export.export_session(sd, tmp_path / "exports")
    assert out.name == "20260102_101010.zip"
    zf = zipfile.ZipFile(out)
    names = zf.namelist()
    manifest = json.loads(zf.read("20260102_101010/export_manifest.json"))
    assert manifest["format"] == export.EXPORT_FORMAT and manifest["session_id"] == "20260102_101010"
    for e in manifest["files"]:                        # every file listed with its checksum
        assert hashlib.sha256(zf.read("20260102_101010/" + e["path"])).hexdigest() == e["sha256"]
    assert {"20260102_101010/" + e["path"] for e in manifest["files"]} == \
        set(names) - {"20260102_101010/export_manifest.json"}
    assert "20260102_101010/control.json" not in names
    assert "20260102_101010/export_manifest.json" in names
    assert export.export_info(sd)["zip"] == str(out)
    assert not list((tmp_path / "exports").glob("*.partial"))
    s = sessions.summarize(sd)
    assert s.exported_zip and s.duration_s == pytest.approx(60.5) and s.screenshots == 2
