"""Interactive chess board: renders a python-chess position from SVG pieces
and lets the user fix the position by dragging pieces around.

Editing model
-------------
* Left-drag a piece from one square onto another  -> move it (captures whatever
  is on the destination square). This is the primary "fix the board" gesture.
* Arm a piece from the palette (``set_armed``) and click any square to place
  it; right-click (or Esc, or a successful placement) disarms.
* Right-click a square that has a piece -> remove that piece.
* Every edit emits ``boardEdited(fen)`` so the caller can keep its FEN in sync.

Extra modes
-----------
* ``set_flipped`` / ``toggle_flip``   -> mirror the board (Black's view).
* ``set_mode(BoardMode)``             -> the one switch for the four mutually
  exclusive input modes below. ``interactive`` / ``propose_mode`` are kept as
  the flags the painting code reads, but they are now *derived* from ``mode``
  by every setter, so a new mode can never end up fighting an old boolean:

  | mode | behaviour |
  |---|---|
  | ``EDIT``    | the default: palette placement, free piece dragging |
  | ``PROPOSE`` | drags become move proposals: emit ``moveProposed(move)`` |
  | ``LOCKED``  | ignore all mouse edits (line playback on the board) |
  | ``BRANCH``  | drag a legal move of the side to move -> ``branchMove``; the
  |             | position itself can never be edited (the what-if tree) |

* ``set_propose_mode(True)``          -> ``PROPOSE`` (kept for the blunder
  check toggle, which predates the enum).
* ``set_interactive(False)``          -> ``LOCKED``.
* ``set_last_move``                   -> highlight the from/to squares of the
  most recent move (used by line playback).
* ``set_move_overlays``               -> faint numbered arrows for candidate
  moves (used by the analysis panel to draw each engine line's first move).
* ``animate_move``                    -> slide the moving piece(s) to their
  destination with an ease-out animation instead of teleporting (used by
  line playback); ``stop_animation`` cancels an in-flight slide.
"""
from __future__ import annotations

import math
import time
from enum import Enum

import chess
from PyQt6.QtCore import QPointF, Qt, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from . import theme
from .pieces import PieceRenderer


class BoardMode(str, Enum):
    """What a drag on the board means (see the module docstring)."""

    EDIT = "edit"
    PROPOSE = "propose"
    LOCKED = "locked"
    BRANCH = "branch"


LIGHT = QColor("#f0d9b5")
DARK = QColor("#b58863")
DRAG_HL = QColor(255, 205, 60, 210)          # source square while dragging
HOVER_HL = QColor(255, 255, 255, 46)
DROP_HL = QColor(120, 220, 120, 190)         # valid target under the cursor
CHECK_RED = QColor(232, 66, 66, 200)         # king in check
LAST_MOVE_HL = QColor(205, 210, 106, 190)    # from/to squares of last move

# faint numbered arrows for the engine's candidate moves (analysis overlay)
OVERLAY_COLORS = (
    QColor(0x8F, 0xC0, 0x53),   # line 1 — soft green
    QColor(0x5B, 0xA3, 0xE8),   # line 2 — soft blue
    QColor(0xE8, 0x9B, 0x4D),   # line 3 — soft orange
)

FILES = "abcdefgh"


class BoardWidget(QWidget):
    boardEdited = pyqtSignal(str)      # FEN after any edit
    armedChanged = pyqtSignal(object)  # chess.Piece or None
    moveProposed = pyqtSignal(object)  # chess.Move (blunder-check mode)
    branchMove = pyqtSignal(object)    # chess.Move (what-if branch mode)

    def __init__(self, assets_dir=None, renderer: PieceRenderer | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(400, 400)
        self.setMouseTracking(True)
        # A second board (the what-if view has its own) can share this one,
        # which keeps the SVG set parsed once per window.
        self.renderer = renderer if renderer is not None else PieceRenderer(assets_dir)
        self.board = chess.Board("r1bq1rk1/p1pnppbp/1p1p1np1/8/3P1B2/2PBPN2/PP1N1PPP/R2QK2R w KQ - 0 8")
        self._armed: chess.Piece | None = None
        self._drag: dict | None = None      # {"kind": "move"|"place"|"propose"|"branch", ...}
        self._hover_square: int | None = None
        self.show_coords = True
        self.flipped = False
        self.mode = BoardMode.EDIT
        self.interactive = True             # derived from ``mode``
        self.propose_mode = False           # derived from ``mode``
        self._pending_from: int | None = None
        self._last_move: tuple[int, int] | None = None
        self._arrow: tuple[int, int] | None = None   # from_square, to_square
        self._move_overlays: list[tuple[int, int, str]] = []  # (from, to, label)
        # smooth piece-slide animation (set by animate_move)
        self._anim: dict | None = None
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)            # ~60 fps
        self._anim_timer.timeout.connect(self._on_anim_tick)

    # ------------------------------------------------------------------ state
    def set_fen(self, fen: str) -> None:
        """Replace the position. Raises ValueError on a bad FEN."""
        self.board = chess.Board(fen)
        self._anim = None
        self._anim_timer.stop()
        self._drag = None
        self._pending_from = None
        self._last_move = None
        self._arrow = None
        self.update()

    def fen(self) -> str:
        return self.board.fen()

    def set_armed(self, piece: chess.Piece | None) -> None:
        if piece is not None and self.mode in (BoardMode.PROPOSE, BoardMode.BRANCH):
            return                      # no placement while proposing/branching
        self._armed = piece
        self.armedChanged.emit(piece)
        self.setCursor(
            Qt.CursorShape.CrossCursor if piece is not None else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def toggle_side_to_move(self) -> None:
        self.board.turn = chess.WHITE if self.board.turn == chess.BLACK else chess.BLACK
        self.boardEdited.emit(self.board.fen())
        self.update()

    # ------------------------------------------------------------- view state
    def toggle_flip(self) -> None:
        self.flipped = not self.flipped
        self._drag = None
        self._pending_from = None
        self.update()

    def set_flipped(self, flipped: bool) -> None:
        if flipped != self.flipped:
            self.toggle_flip()

    def set_interactive(self, on: bool) -> None:
        self.interactive = on
        # BRANCH is only ever entered through ``set_mode``; the legacy flags
        # must not be able to fall out of it by accident.
        if self.mode is not BoardMode.BRANCH:
            self.mode = (BoardMode.EDIT if on else BoardMode.LOCKED) \
                if not self.propose_mode else BoardMode.PROPOSE
        if not on:
            self._drag = None
            self._pending_from = None
            self.unsetCursor()

    def set_propose_mode(self, on: bool) -> None:
        self.propose_mode = on
        self._drag = None
        self._pending_from = None
        if self.mode is not BoardMode.BRANCH:
            self.mode = BoardMode.PROPOSE if on else BoardMode.EDIT
        if on:
            self._armed = None
            self.armedChanged.emit(None)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_mode(self, mode: BoardMode) -> None:
        """Switch input mode (see the module docstring).

        The two legacy booleans stay the flags the painting and mouse code
        read, but they are re-derived here, so no mode can be entered with
        a stale flag left behind — which is exactly the failure a third
        boolean would have introduced.
        """
        self.mode = mode
        self.interactive = mode is not BoardMode.LOCKED
        self.propose_mode = mode is BoardMode.PROPOSE
        self._drag = None
        self._pending_from = None
        self.unsetCursor()
        if mode in (BoardMode.PROPOSE, BoardMode.BRANCH):
            self._armed = None
            self.armedChanged.emit(None)
        self.update()

    def set_last_move(self, from_square: int, to_square: int) -> None:
        self._last_move = (from_square, to_square)
        self.update()

    def clear_last_move(self) -> None:
        self._last_move = None
        self.update()

    def set_arrow(self, from_square: int, to_square: int) -> None:
        """Show an accent arrow from ``from_square`` to ``to_square``."""
        self._arrow = (from_square, to_square)
        self.update()

    def clear_arrow(self) -> None:
        self._arrow = None
        self.update()

    def set_move_overlays(self, items: list[tuple[int, int, str]]) -> None:
        """Show faint numbered arrows for the engine's candidate first moves.

        ``items`` is a list of ``(from_square, to_square, label)`` triples;
        each label (e.g. the line number) is drawn on the middle of its arrow.
        Pass an empty list to hide them.
        """
        self._move_overlays = list(items)
        self.update()

    def clear_move_overlays(self) -> None:
        self.set_move_overlays([])

    # ---------------------------------------------------------- animation
    def animate_move(self, move: chess.Move, duration_ms: int = 260) -> None:
        """Push ``move`` and slide the moving piece(s) to their destination.

        Castling animates both the king and the rook. Falls back to an
        instant update if the source square is empty (position mismatch).
        """
        self._anim = None
        self._anim_timer.stop()

        movers: list[tuple[chess.Piece, int, int]] = []
        piece = self.board.piece_at(move.from_square)
        if piece is not None:
            movers.append((piece, move.from_square, move.to_square))
        if self._is_castle(move):
            to = move.to_square
            if chess.square_file(to) == 6:          # kingside (g-file)
                rook_from, rook_to = to + 1, to - 1
            else:                                   # queenside (c-file)
                rook_from, rook_to = to - 2, to + 1
            rook = self.board.piece_at(rook_from)
            if rook is not None:
                movers.append((rook, rook_from, rook_to))

        self.board.push(move)
        if movers:
            self._anim = {
                "movers": movers,
                "t0": time.monotonic(),
                "dur": max(0.05, duration_ms / 1000.0),
                "t": 0.0,
            }
            self._anim_timer.start()
        self.update()

    def stop_animation(self) -> None:
        """Cancel any in-flight piece slide (the position stays as-is)."""
        self._anim = None
        self._anim_timer.stop()
        self.update()

    def _is_castle(self, move: chess.Move) -> bool:
        """True when ``move`` is a castling move in the current position."""
        piece = self.board.piece_at(move.from_square)
        if piece is None or piece.piece_type != chess.KING:
            return False
        return abs(chess.square_file(move.to_square)
                   - chess.square_file(move.from_square)) == 2

    def _on_anim_tick(self) -> None:
        anim = self._anim
        if anim is None:
            self._anim_timer.stop()
            return
        anim["t"] = min(1.0, (time.monotonic() - anim["t0"]) / anim["dur"])
        if anim["t"] >= 1.0:
            self._anim = None
            self._anim_timer.stop()
        self.update()

    # ------------------------------------------------------------- geometry
    def _layout(self) -> tuple[float, float, float]:
        """Return (origin_x, origin_y, square_size) for the centred board."""
        w, h = self.width(), self.height()
        sq = max(20.0, min(w, h) / 8.0)
        ox = (w - sq * 8.0) / 2.0
        oy = (h - sq * 8.0) / 2.0
        return ox, oy, sq

    def _screen(self, sq: int) -> tuple[int, int]:
        """Screen grid (file_index, rank_index) for a square, 0-based, honouring flip."""
        if self.flipped:
            return 7 - chess.square_file(sq), chess.square_rank(sq)
        return chess.square_file(sq), 7 - chess.square_rank(sq)

    def _square_at_grid(self, fx: int, fy: int) -> int:
        """Board square at screen grid position (fx, fy), honouring flip."""
        if self.flipped:
            return chess.square(7 - fx, fy)
        return chess.square(fx, 7 - fy)

    def _square_at(self, pos) -> int | None:
        ox, oy, sq = self._layout()
        fx = int((pos.x() - ox) / sq)
        fy = int((pos.y() - oy) / sq)
        if 0 <= fx < 8 and 0 <= fy < 8:
            return self._square_at_grid(fx, fy)
        return None

    # ------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.BOARD_BG))
        ox, oy, sq = self._layout()

        # rounded frame behind the grid
        m = max(4.0, sq * 0.07)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.BOARD_FRAME))
        p.drawRoundedRect(QRectF(ox - m, oy - m, sq * 8 + 2 * m, sq * 8 + 2 * m), m, m)

        drag_from = self._drag["from_square"] if self._drag else None
        anim_dests = {ts for _p, _fs, ts in self._anim["movers"]} if self._anim else set()

        for rank in range(8):
            for file in range(8):
                if self.flipped:
                    sq_i = chess.square(7 - file, rank)
                else:
                    sq_i = chess.square(file, 7 - rank)
                rect = QRectF(ox + file * sq, oy + rank * sq, sq, sq)
                p.fillRect(rect, LIGHT if (file + rank) % 2 == 0 else DARK)

                piece = self.board.piece_at(sq_i)

                # source square of a drag / pending proposal
                if drag_from == sq_i or self._pending_from == sq_i:
                    p.fillRect(rect, DRAG_HL)
                # hover
                elif self._hover_square == sq_i and not self._drag and self.interactive:
                    p.fillRect(rect, HOVER_HL)
                # king in check (side to move)
                if (
                    piece
                    and piece.piece_type == chess.KING
                    and piece.color == self.board.turn
                    and self.board.is_check()
                    and self.board.king(self.board.turn) == sq_i
                ):
                    p.fillRect(rect, CHECK_RED)

                # last move highlight (line playback)
                if self._last_move and sq_i in self._last_move:
                    p.fillRect(rect, LAST_MOVE_HL)

                # piece (hidden on the source square while dragging, and on
                # the destination square while it is being animated in)
                if piece and sq_i != drag_from and sq_i not in anim_dests:
                    self.renderer.draw(p, piece, rect)

        if self.show_coords:
            self._draw_coords(p, ox, oy, sq)

        if self._arrow:
            self._draw_arrow(p, ox, oy, sq)

        if self._move_overlays:
            self._draw_move_overlays(p, ox, oy, sq)

        if self._anim:
            self._draw_anim_movers(p, ox, oy, sq)

        if self._drag:
            self._draw_drag_ghost(p, ox, oy, sq)

        p.end()

    def _draw_coords(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        """File letters on the bottom screen row, rank numbers on the left
        screen column — each drawn in the opposite square colour."""
        f = QFont(self.font())
        f.setPointSizeF(max(6.5, sq * 0.15))
        f.setBold(True)
        p.setFont(f)
        pad = sq * 0.07
        w = sq * 0.32
        for fx in range(8):                       # bottom row -> file letters
            sq_i = self._square_at_grid(fx, 7)
            p.setPen(DARK if (fx + 7) % 2 == 0 else LIGHT)
            rect = QRectF(ox + fx * sq, oy + 7 * sq + sq - w - pad * 0.5, sq - pad, w)
            p.drawText(rect, Qt.AlignmentFlag.AlignRight, FILES[chess.square_file(sq_i)])
        for fy in range(8):                       # left column -> rank numbers
            sq_i = self._square_at_grid(0, fy)
            p.setPen(DARK if fy % 2 == 0 else LIGHT)
            rect = QRectF(ox + pad, oy + fy * sq + pad * 0.5, w, w)
            p.drawText(rect, str(chess.square_rank(sq_i) + 1))

    def _draw_arrow(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        """Accent arrow from the source to the target square (move guidance)."""
        fs, ts = self._arrow
        fx1, fy1 = self._screen(fs)
        fx2, fy2 = self._screen(ts)
        x1 = ox + (fx1 + 0.5) * sq
        y1 = oy + (fy1 + 0.5) * sq
        x2 = ox + (fx2 + 0.5) * sq
        y2 = oy + (fy2 + 0.5) * sq
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return
        ux, uy = dx / dist, dy / dist

        # shaft: from just past the source piece, stopping short of the target
        start = sq * 0.30
        head = sq * 0.42
        sx, sy = x1 + ux * start, y1 + uy * start
        ex, ey = x2 - ux * head, y2 - uy * head

        color = QColor(theme.ACCENT)
        color.setAlpha(200)
        p.save()
        pen = QPen(color, max(3.0, sq * 0.13), Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(sx, sy), QPointF(ex, ey))

        # arrowhead
        nx, ny = -uy, ux
        w = sq * 0.16
        tip = QPainterPath()
        tip.moveTo(x2, y2)
        tip.lineTo(ex + nx * w, ey + ny * w)
        tip.lineTo(ex - nx * w, ey - ny * w)
        tip.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(tip)
        p.restore()

    def _draw_move_overlays(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        """Faint semi-transparent arrows, one per engine line, each labelled
        with its line number on the middle of the arrow."""
        if not self._move_overlays:
            return
        r = max(7.0, sq * 0.19)                     # number badge radius
        for i, (fs, ts, label) in enumerate(self._move_overlays):
            fx1, fy1 = self._screen(fs)
            fx2, fy2 = self._screen(ts)
            x1 = ox + (fx1 + 0.5) * sq
            y1 = oy + (fy1 + 0.5) * sq
            x2 = ox + (fx2 + 0.5) * sq
            y2 = oy + (fy2 + 0.5) * sq
            dx, dy = x2 - x1, y2 - y1
            dist = math.hypot(dx, dy)
            if dist < 1e-6:
                continue
            ux, uy = dx / dist, dy / dist

            # shaft: from just past the source piece, stopping short of target
            start = sq * 0.30
            head = sq * 0.40
            sx, sy = x1 + ux * start, y1 + uy * start
            ex, ey = x2 - ux * head, y2 - uy * head

            color = QColor(OVERLAY_COLORS[i % len(OVERLAY_COLORS)])
            color.setAlpha(150)
            p.save()
            pen = QPen(color, max(2.5, sq * 0.09), Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(sx, sy), QPointF(ex, ey))

            # arrowhead
            nx, ny = -uy, ux
            w = sq * 0.13
            tip = QPainterPath()
            tip.moveTo(x2, y2)
            tip.lineTo(ex + nx * w, ey + ny * w)
            tip.lineTo(ex - nx * w, ey - ny * w)
            tip.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPath(tip)
            p.restore()

            # number badge on the middle of the line (slightly staggered per
            # line so parallel arrows don't stack their labels on top of each other)
            off = (i - 1) * sq * 0.05
            cx = (sx + ex) / 2.0 + nx * off
            cy = (sy + ey) / 2.0 + ny * off
            badge = QPainterPath()
            badge.addEllipse(QPointF(cx, cy), r, r)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(30, 28, 25, 185))
            p.drawPath(badge)
            f = QFont(self.font())
            f.setPointSizeF(max(6.0, r * 0.95))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                       Qt.AlignmentFlag.AlignCenter, label)

    def _draw_drag_ghost(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        piece = self._drag["piece"]
        target = self._square_at(self._drag["cursor"])
        if target is None:
            return
        fx, ry = self._screen(target)
        rect = QRectF(ox + fx * sq, oy + ry * sq, sq, sq)
        p.fillRect(rect, DROP_HL)
        p.save()
        p.setOpacity(0.75)
        self.renderer.draw(p, piece, rect)
        p.restore()

    def _draw_anim_movers(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        """Draw the sliding piece(s) at their eased, interpolated positions."""
        t = self._anim["t"]
        eased = 1.0 - (1.0 - t) ** 3        # ease-out cubic: slide, then settle
        for piece, fs, ts in self._anim["movers"]:
            fx1, fy1 = self._screen(fs)
            fx2, fy2 = self._screen(ts)
            x = ox + (fx1 + (fx2 - fx1) * eased) * sq
            y = oy + (fy1 + (fy2 - fy1) * eased) * sq
            self.renderer.draw(p, piece, QRectF(x, y, sq, sq))

    # ------------------------------------------------------------- mouse
    def _pos(self, event):
        return event.position()

    def mousePressEvent(self, event) -> None:
        if not self.interactive:
            return
        sq = self._square_at(self._pos(event))
        if event.button() == Qt.MouseButton.LeftButton:
            if self.mode is BoardMode.BRANCH:
                # Only the side to move may be dragged, and the move has to be
                # legal: the position itself is never edited here.
                if sq is None:
                    return
                if self._pending_from is not None:
                    self._try_branch(self._pending_from, sq)
                    self._clear_proposal()
                else:
                    piece = self.board.piece_at(sq)
                    if piece is not None and piece.color == self.board.turn:
                        self._pending_from = sq
                        self._drag = {"kind": "branch", "from_square": sq,
                                      "piece": piece, "cursor": self._pos(event)}
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            if self.propose_mode:
                if sq is None:
                    return
                if self._pending_from is not None:
                    self._try_proposal(self._pending_from, sq)
                    self._clear_proposal()
                else:
                    piece = self.board.piece_at(sq)
                    if piece is not None and piece.color == self.board.turn:
                        self._pending_from = sq
                        self._drag = {"kind": "propose", "from_square": sq,
                                      "piece": piece, "cursor": self._pos(event)}
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            if sq is None:
                return
            piece = self.board.piece_at(sq)
            if piece is not None:
                self._drag = {"kind": "move", "from_square": sq, "piece": piece,
                              "cursor": self._pos(event)}
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            elif self._armed is not None:
                self._drag = {"kind": "place", "from_square": None,
                              "piece": self._armed, "cursor": self._pos(event)}
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            if self.mode in (BoardMode.PROPOSE, BoardMode.BRANCH):
                self._clear_proposal()
                self.update()
            elif self._armed is not None:
                self.set_armed(None)          # right-click disarms the palette
            elif sq is not None and self.board.piece_at(sq) is not None:
                self.board.remove_piece_at(sq)
                self.boardEdited.emit(self.board.fen())
                self.update()

    def mouseMoveEvent(self, event) -> None:
        if not self.interactive:
            return
        if self._drag:
            self._drag["cursor"] = self._pos(event)
            self.update()
        else:
            self._hover_square = self._square_at(self._pos(event))
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if not self.interactive:
            return
        if self._drag and self._drag.get("kind") in ("propose", "branch"):
            drag, self._drag = self._drag, None
            target = self._square_at(self._pos(event))
            self.unsetCursor()
            if target is not None and target != drag["from_square"]:
                if drag["kind"] == "branch":
                    self._try_branch(drag["from_square"], target)
                else:
                    self._try_proposal(drag["from_square"], target)
                self._clear_proposal()
            self.update()
            return
        if not self._drag:
            return
        drag, self._drag = self._drag, None
        target = self._square_at(self._pos(event))
        changed = False

        if drag["kind"] == "move":
            if target is not None and target != drag["from_square"]:
                self.board.remove_piece_at(drag["from_square"])
                self.board.set_piece_at(target, drag["piece"])
                changed = True
        else:  # place
            if target is not None:
                self.board.set_piece_at(target, drag["piece"])
                changed = True
                if self._armed is not None:   # a placement consumes the armed piece
                    self.set_armed(None)

        self.unsetCursor()
        if changed:
            self.boardEdited.emit(self.board.fen())
        self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._drag:
                self._drag = None
                self._pending_from = None
                self.unsetCursor()
                self.update()
            elif self._pending_from is not None:
                self._clear_proposal()
                self.update()
            elif self._armed is not None:
                self.set_armed(None)

    # ------------------------------------------------------------- proposal
    def _legal_move(self, from_square: int, to_square: int) -> chess.Move | None:
        """The legal move from -> to, auto-queening a promotion. Never
        mutates the board; None when there is no such move."""
        if from_square == to_square:
            return None
        move = chess.Move(from_square, to_square)
        if self.board.is_legal(move):
            return move
        promo = chess.Move(from_square, to_square, promotion=chess.QUEEN)
        if self.board.is_legal(promo):
            return promo
        return None

    def _try_proposal(self, from_square: int, to_square: int) -> None:
        """Emit moveProposed for a legal move; never mutates the board."""
        move = self._legal_move(from_square, to_square)
        if move is not None:
            self.moveProposed.emit(move)

    def _try_branch(self, from_square: int, to_square: int) -> None:
        """Emit branchMove for a legal move; never mutates the board."""
        move = self._legal_move(from_square, to_square)
        if move is not None:
            self.branchMove.emit(move)

    def _clear_proposal(self) -> None:
        self._pending_from = None
        self._drag = None
        self.unsetCursor()

    # ------------------------------------------------------------- helpers
    def sizeHint(self):
        return self.minimumSize()
