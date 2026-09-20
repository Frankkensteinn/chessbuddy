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

from PyQt6.QtCore import QBuffer, QByteArray, QPointF, QSettings  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

import chess  # noqa: E402

from chessbuddy import analysis_panel as ap  # noqa: E402
from chessbuddy import duolingo_canvas as dcv  # noqa: E402
from chessbuddy import graph_model as gm  # noqa: E402
from chessbuddy import graph_view as Gv  # noqa: E402
from chessbuddy import theme  # noqa: E402
from chessbuddy.analysis_panel import AnalysisPanel, san_pv  # noqa: E402
from chessbuddy.board_widget import BoardMode, BoardWidget  # noqa: E402
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


# ------------------------------------------------------------- what-if tree
def _tree_checks() -> None:
    """The what-if tree's rules, with no canvas involved."""
    # identity: the same position reached two ways is two nodes, so
    # "how did I get here" stays unique — which is the only reason Δ means
    # anything; the engine cache is keyed by FEN instead, so the transposition
    # still reuses a search.
    tree = gm.Tree(chess.STARTING_FEN)
    node = tree.root
    for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
        node = tree.add_child(node, uci, source="user")
    assert chess.Board(node.fen).board_fen() \
        == chess.Board(tree.root.fen).board_fen(), node.fen
    assert node.fen != tree.root.fen, "the counters really did move on"
    assert gm.cache_key(node.fen) == gm.cache_key(tree.root.fen), "same position"
    assert gm.cache_key(node.fen) != node.fen, "the move counters must not key it"
    assert node.id != tree.root.id and len(tree.nodes) == 5
    assert node.id == "root/g1f3/g8f6/f3g1/f6g8", node.id
    # the cache is keyed by *position*, so the four-move shuffle hits it
    assert tree.eval_cache == {}
    tree.eval_cache[gm.cache_key(node.fen)] = {"lines": {1: {"score_cp": 7}}, "t": 0.0}
    assert tree.eval_cache[gm.cache_key(tree.root.fen)]["lines"][1]["score_cp"] == 7
    # an existing child comes back, it is never duplicated
    assert tree.add_child(tree.root, "g1f3") is tree.nodes["root/g1f3"]

    # Δ: the loss is measured from the side that had to choose, so the
    # same White-relative numbers have to flip for a Black move.
    parent = gm.Node(id="p", parent=None, move=None,
                     fen="4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    child = gm.Node(id="c", parent=parent, move=chess.Move.from_uci("e1e2"),
                    fen="4k3/8/8/8/8/8/4K3/8 b - - 1 1")
    parent.eval_cp, child.eval_cp = 30, -10          # White +0.30 → -0.10
    assert gm.delta_cp(parent, child) == 40, gm.delta_cp(parent, child)
    parent.fen = child.fen                           # now Black had to choose
    parent.eval_cp, child.eval_cp = -30, 40          # Black +0.30 → White +0.40
    assert gm.delta_cp(parent, child) == 70, gm.delta_cp(parent, child)
    # a move the engine rates *better* than the parent's own best line comes
    # out negative (search noise) rather than clamped to no loss at all
    child.eval_cp = -40
    assert gm.delta_cp(parent, child) == -10, gm.delta_cp(parent, child)
    child.eval_cp = None
    assert gm.delta_cp(parent, child) is None, "an unsearched child has no Δ"
    parent.eval_cp = None
    assert gm.delta_cp(parent, child) is None, "an unsearched parent has none either"

    # lanes: the first child keeps the parent's row, a later sibling is
    # inserted below the previous sibling's whole subtree, and — the property
    # the viewport anchor depends on — an insertion never moves an ancestor.
    tree = gm.Tree(chess.STARTING_FEN)
    main = tree.grow_line(tree.root, ["e2e4", "e7e5", "g1f3", "b8c6"])
    tree.relayout()
    assert [n.lane for n in main] == [0, 0, 0, 0], [n.lane for n in main]
    assert [n.ply for n in main] == [1, 2, 3, 4]
    side = tree.add_child(tree.root, "d2d4")         # a second root child
    tree.relayout()
    assert side.lane == 1, side.lane
    assert [n.lane for n in main] == [0, 0, 0, 0], "a main line must not drift"
    deep = tree.add_child(main[-1], "d2d4")          # deeper, on the main line
    tree.relayout()
    assert deep.lane == 0 and deep.ply == 5, "a first child keeps its parent's row"
    before = (tree.root.lane, [n.lane for n in main])
    # deep's *next* sibling goes below deep's whole subtree, and the shift
    # pushes the unrelated lane below it further down
    extra = tree.add_child(deep, "e5d4")
    tree.relayout()
    assert extra.lane == 0 and extra.ply == 6
    fork = tree.add_child(deep, "f8b4")
    tree.relayout()
    assert fork.lane == 1, fork.lane
    assert side.lane == 2, "the lane under the insertion point shifts down"
    assert (tree.root.lane, [n.lane for n in main]) == before, \
        "an insertion may not move anything above it"
    assert tree.lanes() == 3 and tree.plies() == 6
    # the ply stays a plain distance from the root whatever the lanes do
    assert fork.ply == 6 and all(n.ply == len(tree.path_to(n)) - 1
                                 for n in tree.nodes.values())

    # a terminal position is drawn but never searched
    mate = gm.Tree("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    assert mate.root.searchable
    mated = mate.add_child(mate.root, "a1a8")
    assert chess.Board(mated.fen).is_checkmate(), mated.san
    assert not mated.searchable, "a mate is drawn, but there is nothing to search"
    mate.relayout()
    assert mated.ply == 1 and mated.lane == 0
    # pruning: killing a node takes its whole branch and nothing else,
    # and a *candidate* lane that dies by hand puts its parent back to
    # expandable — otherwise the ＋ that laid it out could never come back
    pruned = gm.Tree(chess.STARTING_FEN)
    lane = pruned.grow_line(pruned.root, ["e2e4", "e7e5", "g1f3"])
    pruned.root.candidates = {1: {"pv": ["e2e4"]}}
    pruned.root.spawned.append(lane[0].id)
    assert not gm.can_expand(pruned.root) and gm.can_collapse(pruned.root)
    gone = pruned.remove_subtree(lane[1])
    assert [n.id for n in gone] == [lane[1].id, lane[2].id], [n.id for n in gone]
    assert set(pruned.nodes) == {pruned.root.id, lane[0].id}
    assert lane[0].children == [] and pruned.root.children == [lane[0]]
    assert pruned.remove_subtree(pruned.root) == [], "the anchor is not removable"
    # collapse folds exactly the candidate lanes back and the node survives,
    # expandable again — and the engine cache is none of its business, since
    # it is keyed by position and a folded lane costs nothing to grow back
    assert pruned.collapse(pruned.root) == [lane[0]]
    assert set(pruned.nodes) == {pruned.root.id} and not pruned.root.spawned
    assert gm.can_expand(pruned.root), "a collapsed node must be expandable again"
    pruned.relayout()
    print(f"what-if model OK: {len(tree.nodes)} nodes, {tree.lanes()} lanes, "
          f"transposition ids differ, Δ flips with the mover")


def _whatif_checks(app) -> None:
    """The whole view: entry, search, expand, branch, leave, and re-entry."""
    win = MainWindow()
    win.resize(1300, 900)
    win._board.set_fen(FEN)
    win.show()
    # the button is gated on a *finished* analysis, not on a started one
    assert not win._whatif_btn.isVisible(), "What-if before an analysis"
    win._panel.analyze(FEN)
    assert not win._whatif_btn.isVisible(), "What-if while the analysis runs"
    if win._engine.thread:
        win._engine.thread.wait(20000)
    app.processEvents()
    assert win._whatif_btn.isVisible(), "What-if after the analysis"

    win._enter_whatif()
    app.processEvents()
    wi = win._whatif
    tree = wi._tree
    canvas = wi._canvas
    assert win._stack.currentIndex() == 1 and wi.is_active()
    # three engine lines became three lanes for free; nothing was searched
    assert tree.lanes() == 3, tree.lanes()
    assert len(tree.nodes) > 3 * 4, len(tree.nodes)
    assert tree.root.lit and not tree.root.children[0].lit, "only the root is lit"
    assert win._engine.busy is False, "entering must not start a search"
    # the opening picture is at a zoom whose nodes still carry their text
    assert Gv.LOD_TEXT <= canvas.zoom_factor() <= Gv.ZOOM_MAX, canvas.zoom_factor()
    assert canvas.item_for(tree.root) is not None
    assert len(canvas._items) == len(tree.nodes)
    assert canvas.width() > 300 and canvas.height() > 300

    # picking a node searches it once (MultiPV) and lights it up
    target = tree.root.children[1].children[0]
    lane_before, nodes_before = target.lane, len(tree.nodes)
    canvas.select_node(target)
    if win._engine.thread:
        win._engine.thread.wait(20000)
    app.processEvents()
    assert target.lit, "a clicked node must light up"
    assert target.depth and target.eval_cp is not None
    assert sorted(target.candidates) == [1, 2, 3], sorted(target.candidates)
    assert target.delta_cp == gm.delta_cp(target.parent, target)
    assert wi._move_label.text() == gm.move_text(target)
    assert wi._depth_label.text() == f"d{target.depth}"
    assert "Δ" in wi._delta_label.text(), wi._delta_label.text()
    # the dock board shows the node, with its candidates as the usual arrows
    assert wi._board.fen() == target.fen
    assert len(wi._board._move_overlays) == len(target.candidates)
    assert target.lane == lane_before, "selecting must not re-layout"

    # expand is free: it re-lays the candidates that same search returned
    assert gm.can_expand(target) and wi._expand_btn.isEnabled()
    assert wi._expand_btn.text().startswith("＋")
    kids_before = len(target.children)
    children_before_expand = list(target.children)
    wi.expand_cursor()
    app.processEvents()
    assert len(tree.nodes) > nodes_before, "expand added nothing"
    assert win._engine.busy is False, "expanding must not search"
    # the node already had a PV continuation, so only the *other* candidates
    # arrive as new children — and each one heads its own chain
    appended = target.children[kids_before:]
    assert appended, "no candidate lanes were laid out"
    assert appended[0].note.startswith("engine candidate #"), appended[0].note
    # only the *heads* of the new lanes are remembered as spawned: the rest of
    # a chain is a variation continuation, and collapse must not fold that —
    # nor the lane the node was already continuing (candidate #1 usually *is*
    # that move, and ``add_child`` hands the same node back), nor a branch the
    # user grew by hand
    assert target.spawned == [c.id for c in appended], target.spawned
    assert all(c.children for c in appended), "a lane head must head a chain"
    assert len(tree.nodes) - nodes_before > len(appended), "the chains came along"
    # the button is the toggle's other half: once the candidates are out there
    # is nothing left to expand, and the chip says so instead of greying out
    assert not gm.can_expand(target) and gm.can_collapse(target)
    assert wi._expand_btn.isEnabled() and wi._expand_btn.text().startswith("−")
    assert canvas.item_for(target).marker_action() == "collapse"

    # ... and the same key folds them back in: the node stays, the lanes go,
    # and the node ends up expandable again rather than stuck
    lanes = [tree.nodes[cid] for cid in target.spawned]
    expanded_nodes = len(tree.nodes)
    nodes_after_expand = list(tree.nodes)
    wi.expand_cursor()
    app.processEvents()
    assert len(tree.nodes) < expanded_nodes, "collapse removed nothing"
    assert target.id in tree.nodes and canvas.item_for(target) is not None
    assert target.children == children_before_expand, "collapse took too much"
    assert not (set(tree.nodes) - set(nodes_after_expand)), "collapse made a node"
    assert not target.spawned and gm.can_expand(target) and not gm.can_collapse(target)
    assert canvas.item_for(target).marker_action() == "expand"
    assert not wi._expand_btn.text().startswith("−"), wi._expand_btn.text()
    assert win._engine.busy is False, "folding back must not search either"
    # re-expanding is a redraw again — same ids in the same order, same count,
    # still no search, and no lane left dangling in the canvas
    wi.expand_cursor()
    app.processEvents()
    assert len(tree.nodes) == expanded_nodes, len(tree.nodes)
    assert [c.id for c in target.children] \
        == [c.id for c in children_before_expand] + [c.id for c in appended]
    assert all(c.id in tree.nodes for c in lanes), "a folded lane stayed dangling"
    assert set(canvas._items) == set(tree.nodes)
    wi.expand_cursor()                       # leave it folded for the next block
    app.processEvents()

    # killing a branch takes the node, its move and everything under it, and
    # nothing above it — and a cursor parked inside the doomed subtree has to
    # come out of it rather than point at a node that no longer exists.
    # The victim is the last lane the root owns: a whole engine line, which is
    # what a user actually wants gone when a candidate turns out to be junk.
    victim = tree.root.children[-1]
    assert victim.parent is tree.root and victim.children
    parent = victim.parent
    # what the right-click actually offers, and to whom: the ＋/−
    # entries mirror the same toggle the chip does, and only the root has no
    # delete — the tree is anchored to it
    menu, verbs = canvas.item_for(victim).node_menu()
    state = {verb: action.isEnabled() for action, verb in verbs.items()}
    assert set(verbs.values()) == {"expand", "collapse", "kill"}
    assert state == {"expand": gm.can_expand(victim),
                     "collapse": gm.can_collapse(victim), "kill": True}, state
    menu.deleteLater()
    root_menu, root_verbs = canvas.item_for(tree.root).node_menu()
    assert {v: a.isEnabled() for a, v in root_verbs.items()}["kill"] is False
    root_menu.deleteLater()
    doomed = {n.id for n in tree.subtree(victim)}
    deepest = [n for n in tree.subtree(victim) if not n.children][-1]
    total = len(tree.nodes)
    # a delete with something behind it asks first, and a "no" changes nothing
    asked: list[tuple[str, str]] = []
    wi._confirm = lambda title, text: (asked.append((title, text)), False)[1]
    wi.kill_branch(victim)
    app.processEvents()
    assert asked and "Delete" in asked[0][0], asked
    assert "cancelled" in wi._status.text(), wi._status.text()
    assert len(tree.nodes) == total and parent.children, "a refused delete moved it"
    # a leaf has nothing behind it to lose, so it goes without a dialog
    leaf = next(n for n in tree.nodes.values()
                if not n.children and n.parent and n.id not in doomed)
    asks_before = len(asked)
    wi._confirm = lambda title, text: (asked.append((title, text)), True)[1]
    wi.kill_branch(leaf)
    app.processEvents()
    assert len(asked) == asks_before, "a leaf must not raise a dialog"
    assert leaf.id not in tree.nodes and len(tree.nodes) == total - 1

    canvas.select_node(deepest)
    siblings = len(parent.children)
    wi.kill_branch(victim)
    app.processEvents()
    assert not (doomed & set(tree.nodes)), "a killed node is still in the tree"
    assert parent.id in tree.nodes and len(parent.children) == siblings - 1
    assert victim.id not in {c.id for c in parent.children}
    assert set(canvas._items) == set(tree.nodes), "the canvas kept a dead item"
    assert canvas.item_for(victim) is None and canvas.item_for(deepest) is None
    assert wi._cursor is parent, "the cursor fell into a dead branch"
    assert canvas.cursor() is parent
    assert all(cid in tree.nodes
               for n in tree.nodes.values() for cid in n.spawned), \
        "a spawned lane id outlived the node it pointed at"
    tree.relayout()
    assert all(n.ply == len(tree.path_to(n)) - 1 for n in tree.nodes.values())

    # the root is the one node that cannot go: it is the tree's anchor
    keep_nodes, keep_fen = len(tree.nodes), wi.cursor_fen()
    wi.kill_branch(tree.root)
    assert len(tree.nodes) == keep_nodes and wi.cursor_fen() == keep_fen
    assert "the root" in wi._status.text(), wi._status.text()
    canvas.select_node(target)               # back where the drag block left it

    # ... and the wires are actually drawn: a child that inherits its parent's
    # lane gets a *straight* wire, whose bounding rect has zero height — which
    # QRectF.intersects calls empty, so the old culling test dropped exactly
    # the wires on a main line and left the elbows. Sample the gap between the
    # two boxes instead of trusting the code to look right.
    canvas.reset_zoom(tree.root)
    app.processEvents()
    shot = canvas.grab().toImage()
    root_a = canvas.item_for(tree.root).mapToScene(
        canvas.item_for(tree.root).box()).boundingRect()
    kid_b = canvas.item_for(tree.root.children[0]).mapToScene(
        canvas.item_for(tree.root.children[0]).box()).boundingRect()
    assert abs(root_a.center().y() - kid_b.center().y()) < 0.5, "not a main line"
    mid = canvas.mapFromScene(
        QPointF((root_a.right() + kid_b.left()) / 2.0, root_a.center().y()))
    assert canvas.viewport().rect().contains(mid), "the wire is off screen"
    column = {shot.pixelColor(mid.x(), mid.y() + dy).name()
              for dy in range(-2, 3)}
    assert column != {theme.GRAPH_BG.lower()}, \
        "the straight wire on the main line was not painted"

    # a dragged move branches, and inserting it must not move the cursor node
    pos = canvas.item_for(target).scenePos()
    move = next(iter(chess.Board(target.fen).legal_moves))
    before = len(tree.nodes)
    edits: list[str] = []
    wi._board.boardEdited.connect(edits.append)
    wi._board.set_mode(BoardMode.BRANCH)
    wi._board.set_armed(chess.Piece(chess.QUEEN, chess.WHITE))
    assert wi._board._armed is None, "BRANCH mode must refuse a palette piece"
    wi._board._try_branch(move.from_square, move.to_square)
    assert len(tree.nodes) == before + 1, "the drag did not branch"
    assert wi._cursor.parent is target and wi._cursor.source == "user"
    assert wi._cursor.move == move and wi._cursor.note == "your branch"
    assert not edits, "BRANCH mode must never edit the position"
    assert wi._board.fen() == wi._cursor.fen, "the board follows the new node"
    assert canvas.item_for(target).scenePos() == pos, \
        "an insertion moved the cursor node on screen"
    if win._engine.thread:
        win._engine.thread.wait(20000)
    app.processEvents()
    assert wi._cursor.lit, "a new branch is searched straight away"
    assert wi._cursor.delta_cp is not None, "Δ needs the parent's eval"
    # ... while a wrong-colour drag is simply ignored
    n_before = len(tree.nodes)
    wi._board.board.set_piece_at(chess.E4, chess.Piece(chess.PAWN, chess.BLACK))
    wi._board._try_branch(chess.E4, chess.E5)
    assert len(tree.nodes) == n_before
    wi._board.board.set_piece_at(chess.E4, None)
    # ... and so is an illegal one
    empty = next(s for s in chess.SQUARES if wi._board.board.piece_at(s) is None)
    wi._board._try_branch(move.from_square, empty)
    assert len(tree.nodes) == n_before

    # navigation: ←/→ walk the line, ↑/↓ change lanes, Home goes home
    holder = next(n for n in tree.nodes.values() if len(n.children) >= 2)
    row = holder.children
    canvas.select_node(row[0])
    assert canvas.cursor() is row[0]
    wi._nav_back()
    assert wi._cursor is holder
    wi._nav_forward()
    assert wi._cursor is row[0]
    wi._nav_sibling(1)
    assert wi._cursor is row[1]
    wi._nav_sibling(-1)
    assert wi._cursor is row[0]
    wi._nav_sibling(-1)
    assert wi._cursor is row[0], "the end of a sibling row must not wrap"
    wi._nav_home()
    assert wi._cursor is tree.root
    assert len(canvas._scene.path_ids) == 1        # only the root is on the path
    assert canvas.item_for(tree.root).zValue() > canvas.item_for(row[0]).zValue()

    # zoom tiers: every level of detail has to paint at every size
    for factor in (1.4, 0.8, 0.45, 0.2):
        canvas.reset_zoom()
        canvas.zoom_by(factor)
        app.processEvents()
        shot = canvas.grab()
        assert not shot.isNull() and shot.width() > 100, factor
        assert abs(canvas.zoom_factor() - factor) < 0.01, canvas.zoom_factor()
    assert wi._zoom_label.text().endswith("%"), wi._zoom_label.text()
    canvas.fit()
    assert Gv.ZOOM_MIN <= canvas.zoom_factor() <= Gv.ZOOM_MAX

    # PGN of the current path
    canvas.select_node(row[0])
    wi.copy_pgn()
    pgn = QApplication.clipboard().text()
    assert '[SetUp "1"]' in pgn and pgn.rstrip().endswith("*"), pgn[:120]
    assert wi._status.text().startswith("copied"), wi._status.text()

    # the Follow switch is the one control whose effect is a viewport move, so
    # it says what it does when it is thrown — otherwise it reads as a mystery
    wi._follow_check.setChecked(False)
    assert "Follow off" in wi._status.text(), wi._status.text()
    wi._follow_check.setChecked(True)
    assert "Follow on" in wi._status.text(), wi._status.text()

    # leaving puts the cursor position on the board *without* dropping the
    # analysis or the tree — a display move, like replaying a line
    nodes = len(tree.nodes)
    cursor_fen = wi.cursor_fen()
    win._exit_whatif()
    app.processEvents()
    assert win._stack.currentIndex() == 0 and not wi.is_active()
    assert win._board.fen() == cursor_fen, win._board.fen()
    assert win._fen_edit.text() == cursor_fen, win._fen_edit.text()
    assert win._panel.analyzed_fen() == FEN, "the analysis must survive"
    assert win._whatif_btn.isVisible()
    win._enter_whatif()
    app.processEvents()
    assert len(wi._tree.nodes) == nodes, "re-entry must reuse the tree"
    assert wi.cursor_fen() == cursor_fen, "re-entry must keep the cursor"

    # the light theme repaints the canvas from the palette: a QGraphicsItem
    # ignores the QSS entirely, so this is the only thing that can
    theme.apply("light", app)
    wi.refresh_theme()
    app.processEvents()
    light = canvas.grab().toImage()
    assert light.pixelColor(6, 6).name() == theme.GRAPH_BG.lower(), \
        light.pixelColor(6, 6).name()
    theme.apply("dark", app)
    wi.refresh_theme()
    app.processEvents()
    dark = canvas.grab().toImage()
    assert dark.pixelColor(6, 6).name() == theme.GRAPH_BG.lower()
    assert dark.pixelColor(6, 6) != light.pixelColor(6, 6)

    # a real position change does drop it
    win._exit_whatif()
    win._board.board.set_piece_at(chess.E4, chess.Piece(chess.QUEEN, chess.WHITE))
    win._board.boardEdited.emit(win._board.fen())
    app.processEvents()
    assert wi._tree is None and not win._whatif_btn.isVisible()
    assert canvas.cursor() is None and len(canvas._items) == 0, "dangling nodes"
    win.close()
    print(f"what-if view OK: 3 lanes on entry, searched/expanded/branched, "
          f"re-entry kept {nodes} nodes, both themes paint")


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
    if panel._engine.thread:
        panel._engine.thread.wait(15000)
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

    # 5g. what-if tree: the model's rules first, then the view on top of them
    _tree_checks()
    _whatif_checks(app)

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
