"""Cached QSvgRenderer instances for the piece SVGs in ../assets."""
from __future__ import annotations

from pathlib import Path

import chess
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from .config import PIECE_NAMES, asset_path


class PieceRenderer:
    """Loads <repo>/assets/{white,black}/{name}.svg once and reuses renderers."""

    def __init__(self, assets_dir: Path | None = None):
        self.assets_dir = Path(assets_dir) if assets_dir is not None else None
        self._cache: dict[tuple[bool, int], QSvgRenderer] = {}

    def _resolve(self, piece: chess.Piece) -> Path:
        if self.assets_dir is None:
            raise FileNotFoundError("no assets directory configured")
        color = "white" if piece.color == chess.WHITE else "black"
        return self.assets_dir / color / f"{PIECE_NAMES[piece.piece_type]}.svg"

    def renderer(self, piece: chess.Piece) -> QSvgRenderer:
        key = (piece.color, piece.piece_type)
        r = self._cache.get(key)
        if r is None:
            r = QSvgRenderer(str(self._resolve(piece)))
            self._cache[key] = r
        return r

    def draw(self, painter: QPainter, piece: chess.Piece, rect: QRectF) -> None:
        """Render the piece into `rect` with the given painter (GUI thread only)."""
        self.renderer(piece).render(painter, rect)

    def pixmap(self, piece: chess.Piece, size: int) -> QPixmap:
        """Small standalone pixmap for palette buttons / icons."""
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            self.draw(painter, piece, QRectF(0, 0, size, size))
        finally:
            painter.end()
        return pm
