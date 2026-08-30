"""Path / environment configuration for ChessBuddy.

Everything is derived from this file's location so the app works
regardless of the current working directory. The repository root is
the project root:

    <repo>/assets/white|black/*.svg   <- piece SVGs
    <repo>/Stockfish/                 <- Stockfish binaries (installed by scripts/install_stockfish.py)
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]             # <repo> (this project)
ASSETS_DIR = REPO_ROOT / "assets"
STOCKFISH_DIR = REPO_ROOT / "Stockfish"

WEBBRIDGE_URL = "http://127.0.0.1:10086/command"
WEBBRIDGE_SESSION = "chess-read"

PIECE_NAMES = {1: "pawn", 2: "knight", 3: "bishop", 4: "rook", 5: "queen", 6: "king"}


# The official release tarball ships sources and wiki docs named
# `stockfish*`/`Stockfish*`, so a name match alone picks up .md files.
_JUNK_SUFFIXES = {
    ".7z", ".bz2", ".c", ".cc", ".cff", ".cpp", ".gz", ".h", ".hpp",
    ".json", ".md", ".pdf", ".py", ".rst", ".sh", ".tar", ".txt", ".xz",
    ".yaml", ".yml", ".zip",
}


def _is_engine(path: Path) -> bool:
    """True if ``path`` looks like an executable engine rather than a doc."""
    if not path.is_file():
        return False
    low = path.name.lower()
    if low.endswith(".exe"):
        return True
    if Path(low).suffix in _JUNK_SUFFIXES:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return bool(st.st_mode & 0o111) or st.st_size > 1_000_000


def _engine_files(d: Path) -> list[Path]:
    """Plausible Stockfish binaries directly under ``d``, biggest first.

    Matches by name (not extension) so it works on Windows (*.exe),
    macOS and Linux (extensionless executables).
    """
    return sorted(
        (p for p in d.iterdir() if "stockfish" in p.name.lower() and _is_engine(p)),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )


def stockfish_candidates() -> list[Path]:
    """Ordered list of plausible Stockfish binaries, best first.

    Search order:
      1. $STOCKFISH_BIN (explicit override)
      2. Stockfish/prebuilt/*/stockfish*  (official release, fastest first)
      3. any stockfish* file found anywhere under Stockfish/
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        p = p.resolve()
        if p not in seen and p.is_file():
            seen.add(p)
            candidates.append(p)

    env = os.environ.get("STOCKFISH_BIN")
    if env:
        add(Path(env))

    prebuilt = STOCKFISH_DIR / "prebuilt"
    if prebuilt.is_dir():
        for d in sorted(prebuilt.iterdir()):
            if d.is_dir():
                for bin_path in _engine_files(d):
                    add(bin_path)

    if STOCKFISH_DIR.is_dir():
        for p in sorted(STOCKFISH_DIR.rglob("*")):
            if "stockfish" in p.name.lower() and _is_engine(p):
                add(p)

    return candidates


def asset_path(piece_color: str, piece_name: str) -> Path:
    """asset_path("white", "king") -> <repo>/assets/white/king.svg"""
    return ASSETS_DIR / piece_color / f"{piece_name}.svg"
