# ChessBuddy

A small PyQt6 desktop app that reads the live FEN of the chess.com game open in your browser, renders it on an editable board, and analyzes the position with a local Stockfish engine.

## Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) — `uv sync` installs PyQt6 and python-chess into `.venv`.
- **Stockfish 18** — no manual install needed: `scripts/install_stockfish.py` auto-detects your OS/CPU and downloads the official prebuilt binaries (Windows, macOS, Linux) into `Stockfish/prebuilt/`. (Cloning the Stockfish repo is **not** required.)
- **WebBridge daemon** *(optional — only for live FEN)* — a local WebBridge running at `http://127.0.0.1:10086` to read the position from your open chess.com tab. Without it, paste a FEN or edit the board by hand.

## Quickstart

```bash
uv sync                                        # install dependencies
uv run python scripts/install_stockfish.py     # download Stockfish binaries
uv run python scripts/install_stockfish.py --check   # verify engine handshake
uv run chessbuddy                              # launch the app
```

- `STOCKFISH_BIN=/path/to/stockfish.exe` overrides engine discovery if the binary lives elsewhere.
- `uv run python scripts/smoke_test.py` runs an offscreen self-test.

---

Board piece SVGs: [cburnett chess set](https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces) (CC BY-SA 3.0, via Wikimedia Commons).
