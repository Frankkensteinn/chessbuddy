"""Download the official Stockfish 18 engine binary into Stockfish/prebuilt/.

Only the engine itself is kept: the release archive (~110 MB of tarball
containing src/ and wiki/) is downloaded to a temp dir and discarded, so
prebuilt/<variant>/ ends up with exactly one file.

Auto-detects the OS and CPU and pulls the matching build from the official
sf_18 GitHub release:

    Windows x86-64   stockfish-windows-x86-64[-avx2].zip
    macOS x86-64     stockfish-macos-x86-64[-avx2].tar
    macOS arm64      stockfish-macos-m1-apple-silicon.tar
    Linux x86-64     stockfish-ubuntu-x86-64[-avx2].tar

Usage (from the project dir):
    uv run python scripts/install_stockfish.py          # preferred + fallback build
    uv run python scripts/install_stockfish.py --check  # handshake test only

Stdlib-only so it also runs without the venv:
    python scripts/install_stockfish.py
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREBUILT = REPO_ROOT / "Stockfish" / "prebuilt"

TAG = "sf_18"

_OS = sys.platform
_MACHINE = platform.machine().lower()

# The official .tar bundles the whole repo (src/, wiki/, *.md) next to the
# engine, so "name starts with stockfish" is not enough to find the binary.
_JUNK_SUFFIXES = {
    ".7z", ".bz2", ".c", ".cc", ".cff", ".cpp", ".gz", ".h", ".hpp",
    ".json", ".md", ".pdf", ".py", ".rst", ".sh", ".tar", ".txt", ".xz",
    ".yaml", ".yml", ".zip",
}


def _looks_like_engine(name: str, *, executable: bool, size: int) -> bool:
    low = name.lower()
    if low.endswith(".exe"):
        return True
    if Path(low).suffix in _JUNK_SUFFIXES:
        return False
    return executable or size > 1_000_000


def _is_x86() -> bool:
    return _MACHINE in ("x86_64", "amd64")


def _is_arm() -> bool:
    return _MACHINE in ("arm64", "aarch64")


def targets() -> dict[str, str]:
    """variant-name -> release asset filename for the current platform."""
    if _OS == "win32":
        return {
            "avx2": "stockfish-windows-x86-64-avx2.zip",
            "baseline": "stockfish-windows-x86-64.zip",
        }
    if _OS == "darwin":
        if _is_arm():
            return {"apple-silicon": "stockfish-macos-m1-apple-silicon.tar"}
        return {
            "avx2": "stockfish-macos-x86-64-avx2.tar",
            "baseline": "stockfish-macos-x86-64.tar",
        }
    if _OS.startswith("linux") and _is_x86():
        return {
            "avx2": "stockfish-ubuntu-x86-64-avx2.tar",
            "baseline": "stockfish-ubuntu-x86-64.tar",
        }
    raise SystemExit(
        f"Unsupported platform: {_OS} / {_MACHINE}. "
        "Install Stockfish manually and point STOCKFISH_BIN at it."
    )


def _fetch(url: str, dest: Path) -> None:
    print(f"[get ] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)


def _extract_binary(archive: Path, outdir: Path) -> Path | None:
    """Pull the engine binary out of a zip (Windows) or tar (macOS/Linux)."""
    outdir.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            infos = [
                i for i in z.infolist()
                if not i.is_dir() and _looks_like_engine(
                    Path(i.filename).name, executable=False, size=i.file_size
                )
            ]
        if not infos:
            return None
        with zipfile.ZipFile(archive) as z:
            for info in infos:
                target = outdir / Path(info.filename).name
                with z.open(info) as src, open(target, "wb") as out:
                    out.write(src.read())
                print(f"[exe ] {target}")
        return outdir / Path(infos[0].filename).name
    if archive.suffix == ".tar":
        with tarfile.open(archive) as t:
            members = [
                m for m in t.getmembers()
                if m.isfile() and _looks_like_engine(
                    Path(m.name).name, executable=bool(m.mode & 0o111), size=m.size
                )
            ]
        if not members:
            return None
        # Biggest file with the exec bit set wins (the engine is ~100 MB).
        member = max(members, key=lambda m: (bool(m.mode & 0o111), m.size))
        with tarfile.open(archive) as t:
            target = outdir / Path(member.name).name
            src = t.extractfile(member)
            if src is None:
                return None
            with open(target, "wb") as out:
                out.write(src.read())
            target.chmod(0o755)
            print(f"[exe ] {target}")
        return target
    return None


def _fs_is_engine(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return _looks_like_engine(
        path.name, executable=bool(st.st_mode & 0o111), size=st.st_size
    )


def download(variant: str, fname: str) -> Path | None:
    """Ensure the engine binary for ``variant`` is present under prebuilt/."""
    outdir = PREBUILT / variant
    existing = [p for p in sorted(outdir.glob("*")) if p.is_file() and _fs_is_engine(p)] \
        if outdir.is_dir() else []
    if existing:
        print(f"[skip] {variant}: {existing[0].name} already present")
        return existing[0]
    # Download to a temp dir and throw the archive away afterwards: only the
    # engine itself ever lands in prebuilt/ (the tarball is ~110 MB of waste).
    url = f"https://github.com/official-stockfish/Stockfish/releases/download/{TAG}/{fname}"
    with tempfile.TemporaryDirectory(prefix="sf-dl-") as tmp:
        archive = Path(tmp) / fname
        _fetch(url, archive)
        return _extract_binary(archive, outdir)


def handshake(path: Path) -> str | None:
    """Return engine name if the binary answers `uci`, else None."""
    try:
        proc = subprocess.Popen(
            [str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        name = None
        proc.stdin.write("uci\n")
        proc.stdin.flush()
        deadline = time.time() + 8
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("id name"):
                name = line[7:].strip()
            if line.strip() == "uciok":
                break
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.wait(timeout=3)
        return name
    except Exception:
        return None


def main() -> int:
    PREBUILT.mkdir(parents=True, exist_ok=True)
    if "--check" in sys.argv:
        bins = sorted(p for p in PREBUILT.glob("*/*") if p.is_file() and _fs_is_engine(p))
        ok = False
        for bin_path in bins:
            name = handshake(bin_path)
            print(f"[check] {bin_path.relative_to(REPO_ROOT)} -> {name or 'NO UCI HANDSHAKE'}")
            ok = ok or name is not None
        return 0 if ok else 1

    for variant, fname in targets().items():
        download(variant, fname)
    print("done. run with --check to verify the handshake.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
