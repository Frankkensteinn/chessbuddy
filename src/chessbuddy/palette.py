"""Piece palette: 12 buttons (6 piece types x 2 colours) used to place or
replace pieces on the board after a fetch.

Click a button to "arm" that piece (the button stays checked). Clicking a
square on the board then places it. Click again, right-click on the board or
press Esc to disarm.
"""
from __future__ import annotations

import chess
from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QButtonGroup, QGridLayout, QPushButton, QWidget

from .pieces import PieceRenderer

_TYPES = (chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN)
_LABELS = {chess.KING: "K", chess.QUEEN: "Q", chess.ROOK: "R",
           chess.BISHOP: "B", chess.KNIGHT: "N", chess.PAWN: "P"}


class PiecePalette(QWidget):
    piecePicked = pyqtSignal(object)   # chess.Piece or None

    def __init__(self, renderer: PieceRenderer, parent: QWidget | None = None):
        super().__init__(parent)
        self._renderer = renderer
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[tuple[bool, int], QPushButton] = {}

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        icon_size = 34

        for row, color in enumerate((chess.WHITE, chess.BLACK)):
            for col, ptype in enumerate(_TYPES):
                piece = chess.Piece(ptype, color)
                btn = QPushButton(QIcon(self._renderer.pixmap(piece, icon_size)), "")
                btn.setCheckable(True)
                btn.setToolTip(f"{'White' if color == chess.WHITE else 'Black'} {_LABELS[ptype]}")
                btn.setFixedSize(icon_size + 10, icon_size + 10)
                btn.setIconSize(QSize(int((icon_size + 10) * 0.72), int((icon_size + 10) * 0.72)))
                self._group.addButton(btn)
                layout.addWidget(btn, row, col)
                self._buttons[(color, ptype)] = btn

        self._group.buttonToggled.connect(self._on_toggled)
        self._clear_all()

    def _clear_all(self) -> None:
        self._group.setExclusive(False)
        for btn in self._buttons.values():
            btn.setChecked(False)
        self._group.setExclusive(True)

    def _on_toggled(self, btn: QPushButton, checked: bool) -> None:
        if not checked:
            self.piecePicked.emit(None)
            return
        # uncheck the others
        for key, other in self._buttons.items():
            if other is not btn and other.isChecked():
                other.setChecked(False)
        piece = next((chess.Piece(pt, c) for (c, pt), b in self._buttons.items() if b is btn), None)
        self.piecePicked.emit(piece)

    def sync(self, piece: chess.Piece | None) -> None:
        """Reflect an external disarm (e.g. Esc or a placement on the board)."""
        if piece is None:
            self._clear_all()
            return
        btn = self._buttons.get((piece.color, piece.piece_type))
        if btn and not btn.isChecked():
            btn.setChecked(True)
