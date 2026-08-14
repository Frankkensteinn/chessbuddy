"""Offscreen smoke test: builds the UI, renders a FEN to PNG, edits the board
programmatically, and runs a short Stockfish analysis. No window is shown.

Usage:  uv run python scripts/smoke_test.py [--png out.png]
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import chess  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from chessbuddy.analysis_panel import AnalysisPanel, san_pv  # noqa: E402
from chessbuddy.board_widget import BoardWidget  # noqa: E402
from chessbuddy.config import ASSETS_DIR  # noqa: E402
from chessbuddy.engine import pick_stockfish, StockfishClient  # noqa: E402
from chessbuddy.fen_pipeline import FenFetchError, fetch_live_fen  # noqa: E402
from chessbuddy.main_window import MainWindow  # noqa: E402

FEN = "8/p2Q3p/1p2p1pP/5p2/3P4/BP6/P1Pk2P1/1KR5 w - - 1 43"


def main() -> int:
    app = QApplication(sys.argv)

    # 1. board rendering + editing
    board = BoardWidget(ASSETS_DIR)
    board.resize(480, 480)
    board.set_fen(FEN)
    assert board.board.piece_at(chess.E4) is None
    board.board.set_piece_at(chess.E4, chess.Piece(chess.QUEEN, chess.WHITE))
    board.boardEdited.emit(board.fen())
    out_png = sys.argv[sys.argv.index("--png") + 1] if "--png" in sys.argv else None
    if out_png:
        board.grab().save(out_png)
        print(f"board png -> {out_png}")

    # 2. main window constructs fine
    win = MainWindow()
    win._board.set_fen(FEN)
    win.show()
    print("MainWindow OK; board fen:", win._board.fen()[:40])

    # 3. SAN conversion
    pv = ["d7e6", "f5f4", "a3b4"]
    san = san_pv(FEN, pv)
    print("san_pv:", san)
    assert san[0] == "Qxe6", san

    # 4. engine analysis (short)
    path, name = pick_stockfish()
    client = StockfishClient(path)
    client.handshake()
    result = client.analyze(FEN, movetime_ms=1500, multipv=2)
    print(f"engine {name}: bestmove={result['bestmove']} lines={len(result['lines'])}")
    client.quit()

    # 5. analysis panel (offscreen, short) + board flip + propose mode
    panel = AnalysisPanel(board)
    panel.analyze(FEN)
    if panel._thread:
        panel._thread.wait(15000)
    app.processEvents()
    print("panel lines:", panel._list.count(), "| status:", panel._status_label.text())
    print("board overlays:", [(f, t, n) for f, t, n in board._move_overlays])
    assert len(board._move_overlays) == panel._list.count(), (
        board._move_overlays, panel._list.count()
    )
    panel.shutdown()

    # 5b. board flip round-trip
    board.toggle_flip()
    assert board.flipped
    board.toggle_flip()
    assert not board.flipped

    # 5c. propose mode: a legal move proposal is signalled, board unchanged
    proposals = []
    board.moveProposed.connect(proposals.append)
    board.set_propose_mode(True)
    fen_before = board.fen()
    mv = next(iter(board.board.legal_moves))   # guaranteed legal on current board
    board._try_proposal(mv.from_square, mv.to_square)
    board.set_propose_mode(False)
    assert len(proposals) == 1 and proposals[0] == mv, (proposals, mv)
    assert board.fen() == fen_before, "propose mode must not edit the board"
    print("propose mode OK:", board.board.san(proposals[0]))

    # 6. webbridge (may fail gracefully — that is OK)
    try:
        data = fetch_live_fen()
        print("webbridge OK:", {k: data.get(k) for k in ("fen", "san", "isAtEnd", "playingAs")})
    except FenFetchError as exc:
        print("webbridge unavailable (expected if no tab/daemon):", str(exc)[:120])

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
