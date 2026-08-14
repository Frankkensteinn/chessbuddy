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


def _engine_files(d: Path) -> list[Path]:
    """Regular files under ``d`` whose name suggests a Stockfish binary.

    Matches by name (not extension) so it works on Windows (*.exe),
    macOS and Linux (extensionless executables).
    """
    return [p for p in sorted(d.iterdir()) if p.is_file() and "stockfish" in p.name.lower()]


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
            if p.is_file() and "stockfish" in p.name.lower():
                add(p)

    return candidates


def asset_path(piece_color: str, piece_name: str) -> Path:
    """asset_path("white", "king") -> <repo>/assets/white/king.svg"""
    return ASSETS_DIR / piece_color / f"{piece_name}.svg"
