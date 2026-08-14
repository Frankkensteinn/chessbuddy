"""Main window: fetch the live FEN from chess.com (via WebBridge), render and
edit the board, tweak window opacity / always-on-top, and run quick Stockfish
analysis in the right-hand panel (clickable lines with board playback, plus a
blunder-check mode that evaluates a move proposed on the board)."""
from __future__ import annotations

import chess
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSlider, QStatusBar, QVBoxLayout, QWidget,
)

from .analysis_panel import AnalysisPanel
from .board_widget import BoardWidget
from .config import ASSETS_DIR
from .fen_pipeline import FenFetchError, fetch_live_fen
from .palette import PiecePalette

_SIDE_LABEL = {chess.WHITE: "White to move", chess.BLACK: "Black to move"}


class FetchWorker(QObject):
    """Runs the WebBridge FEN fetch off the GUI thread."""
    success = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.success.emit(fetch_live_fen())
        except FenFetchError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:               # noqa: BLE001 - report anything
            self.failed.emit(f"Unexpected error: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ChessBuddy — chess.com FEN → Stockfish")
        self.resize(1080, 880)
        self._fetch_thread: QThread | None = None
        self._fetch_worker: FetchWorker | None = None
        self._build_ui()

        if not ASSETS_DIR.is_dir():
            self.statusBar().showMessage(
                f"Warning: piece assets not found at {ASSETS_DIR}", 8000
            )

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)

        # -- board first (referenced by the top bar) -------------------------
        self._board = BoardWidget(ASSETS_DIR)

        # -- top bar: fetch / analyze / flip / opacity / pin ---------------
        top = QHBoxLayout()
        self._fetch_btn = QPushButton("⬇  Fetch FEN from browser")
        self._fetch_btn.setToolTip(
            "Reads the live position from the active chess.com tab via WebBridge"
        )
        self._fetch_btn.clicked.connect(self._on_fetch_clicked)
        top.addWidget(self._fetch_btn)

        self._analyze_btn = QPushButton("Analyze with Stockfish")
        self._analyze_btn.setToolTip("1-second quick analysis, 3 lines")
        self._analyze_btn.clicked.connect(self._on_analyze)
        top.addWidget(self._analyze_btn)

        self._flip_btn = QPushButton("⇅ Flip board")
        self._flip_btn.setToolTip("Toggle White's / Black's point of view")
        self._flip_btn.clicked.connect(self._board.toggle_flip)
        top.addWidget(self._flip_btn)
        top.addStretch(1)

        top.addWidget(QLabel("Opacity:"))
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_slider.setValue(100)
        self._opacity_slider.setFixedWidth(120)
        self._opacity_slider.valueChanged.connect(self._on_opacity)
        top.addWidget(self._opacity_slider)
        self._opacity_label = QLabel("100%")
        top.addWidget(self._opacity_label)

        self._pin_check = QCheckBox("Always on top")
        self._pin_check.toggled.connect(self._on_pin_toggled)
        top.addWidget(self._pin_check)
        layout.addLayout(top)

        # -- piece palette ---------------------------------------------------
        self._palette = PiecePalette(self._board.renderer)
        layout.addWidget(self._palette, 0, Qt.AlignmentFlag.AlignHCenter)

        # -- board (left) + analysis panel (right) ---------------------------
        middle = QHBoxLayout()
        middle.addWidget(self._board, 1)
        self._panel = AnalysisPanel(self._board)
        middle.addWidget(self._panel, 0)
        layout.addLayout(middle, 1)

        # -- bottom: FEN + side-to-move --------------------------------------
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("FEN:"))
        self._fen_edit = QLineEdit(self._board.fen())
        self._fen_edit.returnPressed.connect(self._on_apply_fen)
        bottom.addWidget(self._fen_edit, 1)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self._on_apply_fen)
        bottom.addWidget(self._apply_btn)
        self._side_btn = QPushButton(_SIDE_LABEL[self._board.board.turn])
        self._side_btn.clicked.connect(self._board.toggle_side_to_move)
        bottom.addWidget(self._side_btn)
        layout.addLayout(bottom)

        # -- signals ----------------------------------------------------------
        self._board.boardEdited.connect(self._on_board_edited)
        self._board.armedChanged.connect(self._palette.sync)
        self._palette.piecePicked.connect(self._board.set_armed)
        self._board.moveProposed.connect(self._panel.on_move_proposed)
        self._panel.busyChanged.connect(self._on_engine_busy)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — fetch a live position or edit the board", 6000)

    # ---------------------------------------------------------------- actions
    def _on_fetch_clicked(self) -> None:
        if self._fetch_thread is not None:
            return
        self._fetch_btn.setEnabled(False)
        self.statusBar().showMessage("Fetching live FEN from chess.com via WebBridge…")

        self._fetch_thread = QThread(self)
        self._fetch_worker = FetchWorker()
        self._fetch_worker.moveToThread(self._fetch_thread)
        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.success.connect(self._on_fetch_ok)
        self._fetch_worker.failed.connect(self._on_fetch_err)
        self._fetch_worker.finished.connect(self._fetch_thread.quit)
        self._fetch_worker.finished.connect(self._fetch_worker.deleteLater)
        self._fetch_thread.finished.connect(self._fetch_thread.deleteLater)
        self._fetch_thread.finished.connect(self._on_fetch_thread_done)
        self._fetch_thread.start()

    def _on_fetch_thread_done(self) -> None:
        self._fetch_thread = None
        self._fetch_worker = None
        self._fetch_btn.setEnabled(True)

    def _on_fetch_ok(self, data: dict) -> None:
        fen = data["fen"]
        try:
            self._board.set_fen(fen)
        except ValueError as exc:
            self._on_fetch_err(f"Invalid FEN from browser: {exc}")
            return
        self._fen_edit.setText(fen)
        self._side_btn.setText(_SIDE_LABEL[self._board.board.turn])
        self._panel.on_position_changed()

        parts = []
        if data.get("san"):
            parts.append(f"last move {data['san']}")
        if data.get("isAtEnd"):
            parts.append("live position")
        else:
            parts.append("history position (not at end)")
        who = "White" if self._board.board.turn == chess.WHITE else "Black"
        parts.append(f"{who} to move")
        self.statusBar().showMessage("FEN loaded · " + " · ".join(parts), 8000)

    def _on_fetch_err(self, message: str) -> None:
        self.statusBar().showMessage("Fetch failed", 8000)
        QMessageBox.warning(
            self,
            "Fetch failed",
            "Could not read the live FEN from the browser.\n\n"
            f"{message}\n\n"
            "Make sure the WebBridge daemon is running and you have a chess.com "
            "game open in the active tab. You can also edit the board below manually.",
        )

    def _on_analyze(self) -> None:
        fen = self._board.fen()
        try:
            chess.Board(fen)
        except ValueError:
            QMessageBox.warning(self, "Analyze", "The current position is not a valid FEN.")
            return
        self._panel.analyze(fen)

    def _on_engine_busy(self, busy: bool) -> None:
        self._analyze_btn.setEnabled(not busy)
        self._panel._blunder_btn.setEnabled(not busy)

    # ---------------------------------------------------------------- widgets
    def _on_opacity(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._opacity_label.setText(f"{value}%")

    def _on_pin_toggled(self, on: bool) -> None:
        flags = self.windowFlags()
        if on:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _on_board_edited(self, fen: str) -> None:
        self._fen_edit.setText(fen)
        self._side_btn.setText(_SIDE_LABEL[self._board.board.turn])
        self._panel.on_position_changed()

    def _on_apply_fen(self) -> None:
        fen = self._fen_edit.text().strip()
        try:
            self._board.set_fen(fen)
        except ValueError as exc:
            self.statusBar().showMessage(f"Invalid FEN: {exc}", 8000)
            self._fen_edit.setStyleSheet("border: 1px solid #d9534f;")
            return
        self._fen_edit.setStyleSheet("")
        self._side_btn.setText(_SIDE_LABEL[self._board.board.turn])
        self._panel.on_position_changed()
        self.statusBar().showMessage("Position applied", 4000)

    def closeEvent(self, event) -> None:
        if self._fetch_thread is not None:
            self._fetch_thread.quit()
            self._fetch_thread.wait(1000)
        self._panel.shutdown()
        super().closeEvent(event)
