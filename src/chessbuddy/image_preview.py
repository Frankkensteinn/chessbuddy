"""Board image preview — the PNG captured from the duolingo board canvas.

Duolingo has no per-step images: only the live position is ever painted to its
canvas. So the dialog states exactly which snapshot this is ("live position ·
ply N · captured HH:MM:SS") and says out loud that scrubbing the history will
not change the picture.

No extra dependency: the PNG arrives as bytes and goes straight through
``QPixmap.loadFromData``.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from .duolingo_pipeline import BoardImage

_MAX_SIDE = 640


def _fit_side() -> int:
    """Largest square that comfortably fits on the current screen."""
    screen = QApplication.primaryScreen()
    if screen is None:
        return _MAX_SIDE
    geo = screen.availableGeometry()
    return max(240, min(_MAX_SIDE, int(geo.width() * 0.5), int(geo.height() * 0.72)))


class ImagePreviewDialog(QDialog):
    """Read-only view of a captured :class:`BoardImage`."""

    def __init__(self, image: BoardImage, parent: QWidget | None = None):
        super().__init__(parent)
        self._image = image
        self.setWindowTitle("Board image · duolingo")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        view = QLabel()
        view.setObjectName("previewImage")
        view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap()
        if image.png and pix.loadFromData(image.png, "PNG"):
            side = _fit_side()
            view.setPixmap(pix.scaled(
                side, side,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            view.setToolTip(f"{image.width}×{image.height} PNG")
        else:
            view.setText("the captured image could not be decoded")
            view.setMinimumSize(260, 140)
        lay.addWidget(view, 1)

        caption = QLabel(image.caption())
        caption.setObjectName("previewCaption")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(caption)

        notes = ["Only the live position is painted — history steps have no image."]
        if image.status:
            notes.append(f"match status: {image.status}")
        if image.is_stale():
            notes.append(f"this snapshot is {image.age_s():.0f}s old — fetch again to refresh")
        for text in notes:
            note = QLabel(text)
            note.setObjectName("previewNote")
            note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            note.setWordWrap(True)
            lay.addWidget(note)

        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        lay.addLayout(row)


__all__ = ["ImagePreviewDialog"]
