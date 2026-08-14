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
* ``set_propose_mode(True)``          -> drags become move proposals: emit
  ``moveProposed(move)`` for a legal move of the side to move, never edit.
* ``set_interactive(False)``          -> ignore all mouse edits (used while
  replaying an engine line on the board).
* ``set_last_move``                   -> highlight the from/to squares of the
  most recent move (used by line playback).
"""
from __future__ import annotations

import chess
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QWidget

from .pieces import PieceRenderer

LIGHT = QColor("#f0d9b5")
DARK = QColor("#b58863")
DRAG_HL = QColor(255, 205, 60, 210)          # source square while dragging
HOVER_HL = QColor(255, 255, 255, 46)
DROP_HL = QColor(120, 220, 120, 190)         # valid target under the cursor
CHECK_RED = QColor(232, 66, 66, 200)         # king in check
LAST_MOVE_HL = QColor(205, 210, 106, 190)    # from/to squares of last move
COORD_COLOR = QColor(0, 0, 0, 110)

FILES = "abcdefgh"


class BoardWidget(QWidget):
    boardEdited = pyqtSignal(str)      # FEN after any edit
    armedChanged = pyqtSignal(object)  # chess.Piece or None
    moveProposed = pyqtSignal(object)  # chess.Move (blunder-check mode)

    def __init__(self, assets_dir=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(400, 400)
        self.setMouseTracking(True)
        self.renderer = PieceRenderer(assets_dir)
        self.board = chess.Board()
        self._armed: chess.Piece | None = None
        self._drag: dict | None = None      # {"kind": "move"|"place"|"propose", ...}
        self._hover_square: int | None = None
        self.show_coords = True
        self.flipped = False
        self.interactive = True
        self.propose_mode = False
        self._pending_from: int | None = None
        self._last_move: tuple[int, int] | None = None

    # ------------------------------------------------------------------ state
    def set_fen(self, fen: str) -> None:
        """Replace the position. Raises ValueError on a bad FEN."""
        self.board = chess.Board(fen)
        self._drag = None
        self._pending_from = None
        self._last_move = None
        self.update()

    def fen(self) -> str:
        return self.board.fen()

    def set_armed(self, piece: chess.Piece | None) -> None:
        if self.propose_mode and piece is not None:
            return                      # no palette placement while proposing
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
        if not on:
            self._drag = None
            self._pending_from = None
            self.unsetCursor()

    def set_propose_mode(self, on: bool) -> None:
        self.propose_mode = on
        self._drag = None
        self._pending_from = None
        if on:
            self._armed = None
            self.armedChanged.emit(None)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_last_move(self, from_square: int, to_square: int) -> None:
        self._last_move = (from_square, to_square)
        self.update()

    def clear_last_move(self) -> None:
        self._last_move = None
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

    def _square_at(self, pos) -> int | None:
        ox, oy, sq = self._layout()
        fx = int((pos.x() - ox) / sq)
        fy = int((pos.y() - oy) / sq)
        if 0 <= fx < 8 and 0 <= fy < 8:
            if self.flipped:
                return chess.square(7 - fx, fy)   # rank 1 at the top, h-a files
            return chess.square(fx, 7 - fy)       # rank 8 at the top, a-h files
        return None

    # ------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(60, 60, 66))
        ox, oy, sq = self._layout()

        drag_from = self._drag["from_square"] if self._drag else None

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

                # piece (hidden on the source square while dragging)
                if piece and sq_i != drag_from:
                    self.renderer.draw(p, piece, rect)

        if self.show_coords:
            self._draw_coords(p, ox, oy, sq)

        if self._drag:
            self._draw_drag_ghost(p, ox, oy, sq)

        p.end()

    def _draw_coords(self, p: QPainter, ox: float, oy: float, sq: float) -> None:
        f = QFont(self.font())
        f.setPointSizeF(max(6.0, sq * 0.16))
        p.setFont(f)
        p.setPen(COORD_COLOR)
        w = sq * 0.24
        if self.flipped:
            # files along the top edge, ranks along the right edge
            for file in range(8):
                x = ox + file * sq
                p.drawText(QRectF(x + 2, oy + 2, sq, w), FILES[7 - file])
            for rank in range(8):
                y = oy + rank * sq
                p.drawText(QRectF(ox + 8 * sq - w - 4, y + 2, w, w), str(rank + 1))
        else:
            # files along the bottom edge, ranks along the left edge
            for file in range(8):
                x = ox + file * sq
                p.drawText(QRectF(x + 2, oy + 8 * sq - w, sq, w), FILES[file])
            for rank in range(8):
                y = oy + rank * sq
                p.drawText(QRectF(ox + 2, y + 2, w, w), str(8 - rank))

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

    # ------------------------------------------------------------- mouse
    def _pos(self, event):
        return event.position()

    def mousePressEvent(self, event) -> None:
        if not self.interactive:
            return
        sq = self._square_at(self._pos(event))
        if event.button() == Qt.MouseButton.LeftButton:
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
            if self.propose_mode:
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
        if self._drag and self._drag.get("kind") == "propose":
            drag, self._drag = self._drag, None
            target = self._square_at(self._pos(event))
            self.unsetCursor()
            if target is not None and target != drag["from_square"]:
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
    def _try_proposal(self, from_square: int, to_square: int) -> None:
        """Emit moveProposed for a legal move; auto-append a queen for
        pawn underpromotion-free moves. Never mutates the board."""
        if from_square == to_square:
            return
        move = chess.Move(from_square, to_square)
        if self.board.is_legal(move):
            self.moveProposed.emit(move)
            return
        promo = chess.Move(from_square, to_square, promotion=chess.QUEEN)
        if self.board.is_legal(promo):
            self.moveProposed.emit(promo)

    def _clear_proposal(self) -> None:
        self._pending_from = None
        self._drag = None
        self.unsetCursor()

    # ------------------------------------------------------------- helpers
    def sizeHint(self):
        return self.minimumSize()
