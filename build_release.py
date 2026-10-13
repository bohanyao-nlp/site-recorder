#!/usr/bin/env python3
"""Build the participant download: dist/site-recorder-<version>.zip

    python build_release.py

The zip holds exactly what a participant needs to install: the installers,
the package source (installed by `uv tool install`), the pinned constraints
and the participant README. No tests, no server code.
Shell scripts keep their executable bit; .bat/.ps1 keep CRLF line endings.
"""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INCLUDE_FILES = ["install.sh", "install.ps1", "install.bat", "pyproject.toml",
                 "constraints.txt", "README.md"]
INCLUDE_DIRS = ["src"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (HERE / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0"


def check_consistency() -> None:
    """Versions that must agree before shipping."""
    import json
    v = version()
    init = (HERE / "src" / "site_recorder" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{v}"' not in init:
        sys.exit(f"__init__.__version__ != pyproject version {v}")
    man = json.loads((HERE / "src" / "site_recorder" / "extension" / "manifest.json").read_text(encoding="utf-8"))
    for ref in [*man.get("icons", {}).values(), *man["action"]["default_icon"].values(),
                man["action"]["default_popup"], man["background"]["service_worker"],
                *[j for cs in man["content_scripts"] for j in cs["js"]]]:
        if not (HERE / "src" / "site_recorder" / "extension" / ref).is_file():
            sys.exit(f"extension manifest references missing file {ref}")


def build_store_zip(dist: Path) -> Path:
    """The extension as a Chrome Web Store upload: same files, manifest WITHOUT
    the "key" (the store rejects packages that carry one; it assigns the same
    id gbhaoocbgjdighlaaflhgfbmaahgmjaa to the existing listing)."""
    import json
    src = HERE / "src" / "site_recorder" / "extension"
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    man.pop("key", None)
    out = dist / f"site-collector-{man['version']}-webstore.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(man, indent=2))
        for f in sorted(src.iterdir()):
            if f.is_file() and f.name != "manifest.json":
                zf.write(f, f.name)
    print(f"built {out} (upload at https://chrome.google.com/webstore/devconsole; "
          f"bump the version for every upload)")
    return out


def build_wheel(dest: Path) -> Path | None:
    """A pure-Python wheel, so participants install without any build step."""
    uv = shutil.which("uv")
    if not uv:
        print("(uv not found — the release will install from source)")
        return None
    r = subprocess.run([uv, "build", "--wheel", "--out-dir", str(dest), str(HERE),
                        "--build-constraints", str(HERE / "constraints.txt")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        sys.exit("wheel build failed")
    wheels = sorted(dest.glob("site_recorder-*.whl"))
    return wheels[-1] if wheels else None


def main() -> int:
    check_consistency()
    v = version()
    dist = HERE / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"site-recorder-{v}.zip"
    root = f"site-recorder-{v}"
    wheel_dir = Path(tempfile.mkdtemp(prefix="sr-wheel-"))
    wheel = build_wheel(wheel_dir)
    files: list[Path] = [HERE / f for f in INCLUDE_FILES]
    for d in INCLUDE_DIRS:
        files += [p for p in sorted((HERE / d).rglob("*")) if p.is_file()
                  and not (SKIP_PARTS & set(p.parts)) and p.suffix not in SKIP_SUFFIXES
                  and not p.name.endswith(".egg-info") and ".egg-info" not in str(p)]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        if wheel:
            zi = zipfile.ZipInfo(f"{root}/wheel/{wheel.name}", date_time=(2026, 1, 1, 0, 0, 0))
            zi.create_system = 3
            zi.external_attr = (stat.S_IFREG | 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, wheel.read_bytes())
        for f in files:
            rel = f.relative_to(HERE).as_posix()
            data = f.read_bytes()
            if f.suffix in (".bat", ".ps1"):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            elif f.suffix in (".sh", ".py", ".toml", ".txt", ".md", ".js", ".json", ".html"):
                data = data.replace(b"\r\n", b"\n")
            zi = zipfile.ZipInfo(f"{root}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
            # create_system = Unix, or unzip tools ignore the mode bits and
            # install.sh loses its executable bit.
            zi.create_system = 3
            mode = 0o755 if f.suffix == ".sh" else 0o644
            zi.external_attr = (stat.S_IFREG | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
    shutil.rmtree(wheel_dir, ignore_errors=True)
    shutil.rmtree(HERE / "build", ignore_errors=True)        # setuptools' wheel-build leftovers
    print(f"built {out} ({out.stat().st_size // 1024} KB, {len(files)} files"
          + (f" + wheel {wheel.name})" if wheel else ")"))
    build_store_zip(dist)
    return 0


if __name__ == "__main__":
    sys.exit(main())
