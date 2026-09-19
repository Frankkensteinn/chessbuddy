"""Offscreen smoke test: builds the UI, renders a FEN to PNG, edits the board
programmatically, scrubs a full-game history, previews a captured board image
and runs a short Stockfish analysis. No window is shown.

Usage:  uv run python scripts/smoke_test.py [--png out.png]
"""
from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QByteArray, QSettings  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

import chess  # noqa: E402

from chessbuddy import analysis_panel as ap  # noqa: E402
from chessbuddy import duolingo_canvas as dcv  # noqa: E402
from chessbuddy import theme  # noqa: E402
from chessbuddy.analysis_panel import AnalysisPanel, san_pv  # noqa: E402
from chessbuddy.board_widget import BoardWidget  # noqa: E402
from chessbuddy.config import ASSETS_DIR, Source  # noqa: E402
from chessbuddy.duolingo_pipeline import (  # noqa: E402
    _SNAP_JS, BoardImage, DuolingoFetchError, _check_board_fen, _decode_png,
    _reconcile_canvas, derive_fen,
)
from chessbuddy.engine import pick_stockfish, StockfishClient  # noqa: E402
from chessbuddy.fen_pipeline import FenFetchError, fetch_live_fen  # noqa: E402
from chessbuddy.image_preview import ImagePreviewDialog  # noqa: E402
from chessbuddy.main_window import _FETCH_TIP, FetchWorker, MainWindow  # noqa: E402
from chessbuddy.webbridge import WebBridgeError  # noqa: E402

FEN = "8/p2Q3p/1p2p1pP/5p2/3P4/BP6/P1Pk2P1/1KR5 w - - 1 43"
# 1.e4 e5 2.Nf3 Nc6 3.Bb5 — the reference history used by the duolingo tests.
HISTORY = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"]
HISTORY_FEN = "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3"
# A legal 160-ply shuffle, to prove a whole game does not blow up the pill grid.
LONG_HISTORY = ["g1f3", "g8f6", "f3g1", "f6g8"] * 40


def png_bytes(pixmap) -> bytes:
    """PNG bytes for a QPixmap (what the board canvas hands back as a data URL)."""
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(qbuf, "PNG")
    qbuf.close()
    return bytes(buf)


# The live duolingo board canvas, reproduced for the tests: the frame sits
# 0.0948 of the canvas in from each edge and spans 0.8095 of it, the squares are
# inset a further 0.0066 of the frame, and the palette is the one measured off
# the real board on 2026-09-19 (empty #f2f4f5/#ffffff, dark pieces #3d3d3d,
# white pieces #dae4eb with a #9fafba outline).
_DUO_FRAME_X, _DUO_FRAME_W, _DUO_INSET = 0.0948, 0.8095, 0.0066


def duolingo_canvas(board: chess.Board, size: int = 1118) -> bytes:
    """A PNG shaped like duolingo's canvas, showing ``board`` white-side-down."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))               # transparent outside the board
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    frame_x = round(size * _DUO_FRAME_X)
    frame_w = round(size * _DUO_FRAME_W)
    inset = round(frame_w * _DUO_INSET)
    cell = (frame_w - 2 * inset) / 8
    x0 = frame_x + inset
    painter.fillRect(frame_x, frame_x, frame_w, frame_w, QColor("#f2f4f5"))
    for row in range(8):
        for col in range(8):
            if (row + col) % 2 == 0:
                painter.fillRect(round(x0 + col * cell), round(x0 + row * cell),
                                 round(cell) + 1, round(cell) + 1, QColor("#ffffff"))
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue
        col = chess.square_file(square)
        row = 7 - chess.square_rank(square)      # a8 is the top-left square
        cx, cy = x0 + (col + 0.5) * cell, x0 + (row + 0.5) * cell
        radius = cell * 0.28
        white = piece.color == chess.WHITE
        fill, edge = (("#dae4eb", "#9fafba") if white else ("#3d3d3d", "#3d3d3d"))
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(edge), max(2.0, cell * 0.06)))
        painter.drawEllipse(int(cx - radius), int(cy - radius),
                            int(2 * radius), int(2 * radius))
    painter.end()
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(qbuf, "PNG")
    qbuf.close()
    return bytes(buf)


def rotated(grid: dict) -> dict:
    """The same board as the other side sees it (180°)."""
    out = {}
    for square in chess.SQUARES:
        other = chess.square(7 - chess.square_file(square),
                             7 - chess.square_rank(square))
        out[chess.square_name(square)] = grid[chess.square_name(other)]
    return out


def _replay(moves) -> chess.Board:
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


def main() -> int:
    app = QApplication(sys.argv)
    # Keep this run's QSettings out of the real app's domain.
    app.setApplicationName("ChessBuddy-smoke")
    app.setOrganizationName("chessbuddy-smoke")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.APP_QSS)

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
    board_png = png_bytes(board.grab())

    # 1b. duolingo pipeline, offline parts
    # Both pipelines share one error base so FetchWorker can catch either.
    assert issubclass(DuolingoFetchError, WebBridgeError)
    assert issubclass(FenFetchError, WebBridgeError)
    assert derive_fen([]) == chess.STARTING_FEN, derive_fen([])
    assert derive_fen(HISTORY) == HISTORY_FEN, derive_fen(HISTORY)
    for bad, why in ((["e2e4", "e2e4"], "illegal"), (["e2e4", 7], "malformed")):
        try:
            derive_fen(bad)                     # type: ignore[arg-type]
            raise AssertionError(f"{why} history must not be replayed silently")
        except DuolingoFetchError as exc:
            assert why in str(exc), exc
    assert b"\x89PNG" in board_png and _decode_png(
        "data:image/png;base64," + base64.b64encode(board_png).decode()
    ) == board_png
    for junk in ("", "data:image/jpeg;base64,AAAA", None):
        try:
            _decode_png(junk)
            raise AssertionError(f"_decode_png accepted {junk!r}")
        except DuolingoFetchError:
            pass
    # The fragile DOM anchors the pipeline depends on must still be present.
    # The container is matched by prefix so it covers both match kinds:
    # "challenge challenge-chessMatch" (lesson) and "...-chessPvpMatch" (PvP).
    for anchor in ("__reactFiber$", "challenge challenge-chess",
                   "challengeState", "moveHistory", "toDataURL",
                   "boardFen", "isViewingHistory",
                   "inBoard", "aboveBoard", "belowBoard"):
        assert anchor in _SNAP_JS, anchor
    # PvP publishes its own boardFen + UCI history; a disagreement between the
    # two readings must fail loudly instead of picking a winner. Counters are
    # not compared, so a different half-move/full-move number is fine.
    _check_board_fen(HISTORY_FEN, HISTORY_FEN)
    _check_board_fen(HISTORY_FEN.split(" - ")[0] + " - 9 40", HISTORY_FEN)
    _check_board_fen("not-a-fen", HISTORY_FEN)          # unparsable: ignored
    # The en-passant field is written two ways in practice: Duolingo's own
    # boardFen keeps the square after any double pawn push (the letter of the
    # FEN spec) while a python-chess replay only keeps it while a capture is
    # actually available. 1.f4 — no black pawn on e4/g4 to answer it — is the
    # shape of the case that failed live, so it must not read as a
    # disagreement; the second pair is that failure verbatim.
    assert derive_fen(["f2f4"]) == (
        "rnbqkbnr/pppppppp/8/8/5P2/8/PPPPP1PP/RNBQKBNR b KQkq - 0 1"
    ), derive_fen(["f2f4"])
    _check_board_fen("rnbqkbnr/pppppppp/8/8/5P2/8/PPPPP1PP/RNBQKBNR b KQkq f3 0 1",
                     derive_fen(["f2f4"]))
    _check_board_fen("r4r2/ppp1Npkp/4b1p1/2qpp3/5P2/1B6/PPP5/R1K2Q2 b - f3",
                     "r4r2/ppp1Npkp/4b1p1/2qpp3/5P2/1B6/PPP5/R1K2Q2 b - - 0 1")
    for bad in ("8/8/8/8/8/8/8/8 b - - 0 1",
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"):
        try:
            _check_board_fen(bad, HISTORY_FEN)
            raise AssertionError(f"a disagreed boardFen must not pass: {bad}")
        except DuolingoFetchError as exc:
            assert "disagree" in str(exc), exc
    # A single ep square is the record of the last move, so it is only forgiven
    # while no capture hangs off it: a page FEN whose ep square implies a
    # different last move is still a disagreement.
    try:
        _check_board_fen(
            "rnbqkbnr/1pp1pppp/p7/3pP3/8/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 3",
            derive_fen(["e2e4", "a7a6", "e4e5", "d7d5"]))
        raise AssertionError("a page ep square the history contradicts must fail")
    except DuolingoFetchError as exc:
        assert "disagree" in str(exc), exc
    # 1c. reading the board canvas, and the ply it recovers
    # A lesson match commits the user's own move to its history at once but the
    # opponent's scripted reply only when the *next* user move is submitted, so
    # the canvas can hold one ply the history has not caught up with (verified
    # live: history 5 plies ending on the user's move while the board showed the
    # reply, which made a history-only fetch a move behind and named the wrong
    # side to move). Nothing else in the page carries that ply, so it is read
    # back off the canvas — see duolingo_canvas's module docstring.
    base = chess.Board()
    for uci in HISTORY:
        base.push_uci(uci)
    reply = "a7a6"                                    # the uncommitted answer
    after = base.copy()
    after.push_uci(reply)
    for size in (1118, 480):                          # board + avatar canvas
        assert dcv.read_grid(duolingo_canvas(base, size)) == dcv._expected(base), size
    assert dcv.reconcile(HISTORY, dcv.read_grid(duolingo_canvas(base))).state \
        == dcv.STATE_IN_SYNC
    ahead = dcv.read_grid(duolingo_canvas(after))
    verdict = dcv.reconcile(HISTORY, ahead)
    assert (verdict.state, verdict.uci) == (dcv.STATE_RECOVERED, reply), verdict
    # The canvas is drawn from the user's own side, so the same board read 180°
    # round must reconcile just as well (a PvP match where the user plays black).
    assert dcv.reconcile(HISTORY, rotated(ahead)).state == dcv.STATE_RECOVERED
    # A canvas showing only the position before the history's last ply is the
    # paint lagging, not the history: nothing gets recovered there.
    behind = dcv.read_grid(duolingo_canvas(_replay(HISTORY[:-1])))
    assert dcv.reconcile(HISTORY, behind).state == dcv.STATE_CANVAS_BEHIND
    assert dcv.read_grid(duolingo_canvas(base)) != dcv.read_grid(duolingo_canvas(after))
    # Anything that is not an exact fit is reported, never guessed at.
    for junk in (b"", b"not a png"):
        assert dcv.read_grid(junk) is None, junk
    assert dcv.reconcile(HISTORY, None).state == dcv.STATE_UNREADABLE
    # ... including an image that is not duolingo's canvas at all (the app's own
    # board render): it must never yield a recovery.
    assert dcv.reconcile(HISTORY, dcv.read_grid(board_png)).state not in (
        dcv.STATE_RECOVERED, dcv.STATE_IN_SYNC)
    marred = dict(ahead)
    empty = next(sq for sq, val in marred.items() if not val)
    marred[empty] = "w"
    assert dcv.reconcile(HISTORY, marred).state == dcv.STATE_UNRECONCILED
    assert dcv.reconcile(["e2e4", "e2e4"], ahead).state == dcv.STATE_UNRECONCILED

    # ... and the pipeline appends it, but only where no FEN of its own exists
    snapshot = {"png": duolingo_canvas(after), "moves": list(HISTORY),
                "ply": len(HISTORY), "fen": derive_fen(HISTORY),
                "board_fen": "", "recovered": "", "canvas_state": ""}
    assert _reconcile_canvas(snapshot).state == dcv.STATE_RECOVERED
    assert snapshot["recovered"] == reply
    assert snapshot["moves"] == HISTORY + [reply]
    assert snapshot["ply"] == len(HISTORY) + 1
    assert snapshot["fen"] == derive_fen(HISTORY + [reply])
    pvp = dict(snapshot, moves=list(HISTORY), ply=len(HISTORY),
               fen=derive_fen(HISTORY), recovered="",
               board_fen=derive_fen(HISTORY))
    assert _reconcile_canvas(pvp).state == dcv.STATE_UNRECONCILED
    assert pvp["moves"] == HISTORY and not pvp["recovered"], pvp
    print(f"canvas reconciliation OK: recovered {reply} off the board")

    image = BoardImage(png=board_png, width=480, height=480, moves=tuple(HISTORY),
                       status="started")
    assert image.ply == len(HISTORY)
    assert "live position · ply 5" in image.caption(), image.caption()
    recovered_image = BoardImage(png=board_png, width=480, height=480,
                                 moves=tuple(HISTORY + [reply]), recovered=reply)
    assert recovered_image.ply == 6
    assert f"reply {reply} read off the board" in recovered_image.caption()
    assert not image.is_stale(60) and image.is_stale(0.0)
    stale = BoardImage(png=board_png, width=8, height=8,
                       captured_at=datetime.now() - timedelta(seconds=120))
    assert stale.is_stale() and stale.ply == 0
    print(f"duolingo pipeline OK: {image.caption()}")

    # 2. main window constructs fine
    win = MainWindow()
    win._board.set_fen(FEN)
    win.show()
    print("MainWindow OK; board fen:", win._board.fen()[:40])

    # 2b. source picker: duolingo is selectable, persisted, and dispatched
    labels = [win._source_box.itemText(i) for i in range(win._source_box.count())]
    assert labels == [Source.CHESSCOM.value, Source.DUOLINGO.value], labels
    win._source_box.setCurrentIndex(1)
    assert win._source is Source.DUOLINGO
    assert win._fetch_btn.toolTip() == _FETCH_TIP[Source.DUOLINGO]
    assert Source.from_value(QSettings().value("source")) is Source.DUOLINGO
    assert win._source_box.currentIndex() == 1  # survives a fresh window
    assert MainWindow()._source is Source.DUOLINGO
    win._restore_source(Source.CHESSCOM)
    # chess.com mode: duolingo-only controls are struck through, not merely grey
    assert not win._preview_btn.isEnabled() and not win._history_btn.isEnabled()
    assert win._preview_btn.font().strikeOut() and win._history_btn.font().strikeOut()
    assert "duolingo-only" in win._preview_btn.toolTip()
    win._restore_source(Source.DUOLINGO)
    # duolingo mode without a snapshot: greyed out, but NOT struck through
    assert not win._preview_btn.isEnabled() and not win._preview_btn.font().strikeOut()
    assert "Fetch from duolingo" in win._preview_btn.toolTip()
    win._restore_source(Source.CHESSCOM)

    # 2c. FetchWorker dispatches per source (live result optional — the daemon
    #     may be down or the tab may not exist; either way it must not crash).
    worker = FetchWorker(Source.DUOLINGO)
    seen: list[str] = []
    worker.success.connect(lambda data: seen.append("ok"))
    worker.failed.connect(lambda msg: seen.append(f"error: {msg}"))
    worker.finished.connect(lambda: seen.append("done"))
    worker.run()
    assert len(seen) == 2 and seen[-1] == "done", seen
    if seen[0] == "ok":
        print("duolingo fetch OK (live tab found)")
    else:
        assert "duolingo.com" in seen[0] or "WebBridge" in seen[0], seen[0]
        print("duolingo fetch unavailable (expected without a tab):", seen[0][:110])

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

    # 5d. full-game history scrubbing (the duolingo path): plies are counted
    #     from the start position, but leaving playback returns to the live one
    board.set_fen(FEN)
    assert panel.enter_full_game(HISTORY) is True
    assert panel._base_fen == chess.STARTING_FEN, panel._base_fen
    assert panel._restore_fen == FEN, panel._restore_fen
    assert panel._play_kind == "History"
    assert panel._ply == len(HISTORY) - 1        # lands on the live position
    assert board.fen() == HISTORY_FEN, board.fen()
    assert not board.interactive, "history browsing must be read-only"
    assert len(panel._pills) == len(HISTORY)
    panel._show_ply(1)                           # scrub back to ply 2
    assert board.fen() == derive_fen(HISTORY[:2]), board.fen()
    assert panel._ply_label.text() == f"2/{len(HISTORY)}"
    assert panel._pills_scroll.height() <= ap._PILLS_MAX_H
    panel._exit_playback()
    assert board.fen() == FEN, "leaving history must restore the live position"
    assert board.interactive and panel._base_fen is None and panel._restore_fen is None
    # an unplayable or empty history is refused rather than half-applied
    assert panel.enter_full_game([]) is False
    assert panel.enter_full_game(["e2e4", "e2e4"]) is False
    assert board.fen() == FEN and panel._play_moves == []
    # a whole game (160 plies / 80 rows) is capped by the scroller, not the panel
    assert panel.enter_full_game(LONG_HISTORY) is True
    assert panel._pills_scroll.height() == ap._PILLS_MAX_H, panel._pills_scroll.height()
    assert panel._ply == len(LONG_HISTORY) - 1
    panel._exit_playback()
    print(f"history scrub OK: 80 rows capped at {ap._PILLS_MAX_H}px")

    # 5e. board-image preview dialog + the window's snapshot bookkeeping
    img = BoardImage(png=board_png, width=480, height=480, moves=tuple(HISTORY),
                     status="started")
    dlg = ImagePreviewDialog(img)
    dlg.show()
    app.processEvents()
    texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    assert any("live position · ply 5" in t for t in texts), texts
    assert any("history steps have no image" in t for t in texts), texts
    assert any("started" in t for t in texts), texts
    dlg.close()
    broken = ImagePreviewDialog(BoardImage(png=b"not-a-png", width=0, height=0))
    broken.show()
    app.processEvents()
    assert any("could not be decoded" in lbl.text()
               for lbl in broken.findChildren(QLabel))
    broken.close()
    print("image preview dialog OK")

    # 5f. main-window snapshot state: source-aware gating, TTL backstop, and
    #     unconditional replacement by a newer fetch
    win._source_box.setCurrentIndex(0)           # chess.com
    win._set_board_image(img)
    assert not win._preview_btn.isEnabled() and win._preview_btn.font().strikeOut()
    assert not win._history_btn.isEnabled() and win._history_btn.font().strikeOut()
    win._source_box.setCurrentIndex(1)           # duolingo: the snapshot was kept
    assert win._preview_btn.isEnabled() and not win._preview_btn.font().strikeOut()
    assert win._history_btn.isEnabled()
    win._on_history_clicked()
    assert win._panel._base_fen == chess.STARTING_FEN
    assert win._panel._restore_fen == FEN, win._panel._restore_fen
    assert win._fen_edit.text() == HISTORY_FEN, win._fen_edit.text()
    win._panel._exit_playback()
    assert win._board.fen() == FEN and win._fen_edit.text() == FEN
    win._set_board_image(stale)                  # 120s old -> TTL backstop fires
    win._image_timer.stop()
    win._on_image_ttl()
    assert win._board_image is None and not win._preview_btn.isEnabled()
    assert not win._preview_btn.font().strikeOut(), "right mode, just no snapshot"
    win._set_board_image(img)
    win._set_board_image(BoardImage(png=board_png, width=480, height=480))
    assert win._board_image.ply == 0 and not win._history_btn.isEnabled()
    assert win._preview_btn.isEnabled(), "the image is still viewable at ply 0"
    win.close()
    print("snapshot bookkeeping OK")

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
