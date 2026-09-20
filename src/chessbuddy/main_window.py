"""Main window: fetch the live position from chess.com or duolingo (via
WebBridge), render and edit the board, tweak window opacity / always-on-top,
and run quick Stockfish analysis in the right-hand panel (clickable lines with
board playback, plus a blunder-check mode that evaluates a move proposed on the
board).

After an analysis the window also offers the **What-if** view: the same
position, but as an expandable move tree on an infinite canvas (see
``graph_view``). The two views are pages of
one ``QStackedWidget`` and share a single cursor position, so leaving the graph
puts the board on the node you were looking at — and, because that is a
*display* move like replaying a line, the analysis and the tree both survive,
so going back picks up exactly where you were.

The source picker before the Fetch button chooses how a position is read:

* ``chess.com`` — one live FEN off the board's game model (``fen_pipeline``).
* ``duolingo``  — one atomic snapshot: the board canvas as a PNG *and* the
  whole UCI move history, from which the FEN is derived (``duolingo_pipeline``).
  The canvas is also read back to recover the opponent's reply while Duolingo
  has not committed it yet, so the position is never a move behind the board.
  The history can then be scrubbed on the board, and the picture viewed on
  demand — note that duolingo paints only the live position, so the picture is
  never the step being scrubbed."""
from __future__ import annotations

import chess
from PyQt6.QtCore import QObject, QSettings, QThread, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QSlider, QStackedWidget, QStatusBar,
    QVBoxLayout, QWidget,
)

from . import theme
from .analysis_panel import AnalysisPanel, EvalBar
from .board_widget import BoardWidget
from .config import ASSETS_DIR, DUO_IMAGE_TTL_S, Source
from .duolingo_pipeline import BoardImage, fetch_duolingo_snapshot, image_from_snapshot
from .engine_service import EngineService
from .fen_pipeline import fetch_live_fen
from .graph_view import WhatIfView
from .image_preview import ImagePreviewDialog
from .palette import PiecePalette
from .webbridge import WebBridgeError

_SIDE_LABEL = {chess.WHITE: "White to move", chess.BLACK: "Black to move"}

_FETCH_TIP = {
    Source.CHESSCOM: "Reads the live position from the active chess.com tab via WebBridge",
    Source.DUOLINGO: "Reads the active duolingo chess match via WebBridge: board image + the "
                     "whole move history, with the ply Duolingo has not recorded yet read "
                     "back off the board",
}


class FetchWorker(QObject):
    """Runs the WebBridge position fetch off the GUI thread."""

    success = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, source: Source = Source.CHESSCOM):
        super().__init__()
        self.source = source

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self.source is Source.DUOLINGO:
                self.success.emit(fetch_duolingo_snapshot())
            else:
                self.success.emit(fetch_live_fen())
        except WebBridgeError as exc:          # both pipelines share this base
            self.failed.emit(str(exc))
        except Exception as exc:               # noqa: BLE001 - report anything
            self.failed.emit(f"Unexpected error: {exc}")
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ChessBuddy")
        self.resize(1180, 860)
        self.setMinimumSize(980, 680)
        self._fetch_thread: QThread | None = None
        self._fetch_worker: FetchWorker | None = None
        self._source = Source.CHESSCOM
        # One engine for the whole window: the analysis panel and the what-if
        # graph take turns on it, never in parallel.
        self._engine = EngineService(self)
        self._board_image: BoardImage | None = None
        # TTL backstop: a snapshots stops being offered as "current" if the
        # app has been left running (see BoardImage.is_stale).
        self._image_timer = QTimer(self)
        self._image_timer.setSingleShot(True)
        self._image_timer.setInterval(int(DUO_IMAGE_TTL_S * 1000))
        self._image_timer.timeout.connect(self._on_image_ttl)
        self._build_ui()

        saved_source = Source.from_value(QSettings().value("source", Source.CHESSCOM.value))
        self._restore_source(saved_source)

        # restore the saved theme (defaults to dark)
        saved = QSettings().value("theme", "dark")
        if saved in theme.PALETTES and saved != theme.current():
            theme.apply(saved, QApplication.instance())
        self._refresh_theme_btn()

        if not ASSETS_DIR.is_dir():
            self.statusBar().showMessage(
                f"Warning: piece assets not found at {ASSETS_DIR}", 8000
            )

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        board_page = QWidget()
        layout = QVBoxLayout(board_page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # -- board first (referenced by the top bar) -------------------------
        self._board = BoardWidget(ASSETS_DIR)

        # -- top bar: source / fetch / analyze / flip | opacity / pin --------
        top = QHBoxLayout()
        top.setSpacing(8)
        self._source_box = QComboBox()
        for src in (Source.CHESSCOM, Source.DUOLINGO):
            self._source_box.addItem(src.label, src.value)
        self._source_box.setToolTip("Where to read the position from")
        self._source_box.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self._source_box)

        self._fetch_btn = QPushButton("⬇  Fetch position")
        self._fetch_btn.setProperty("accent", True)
        self._fetch_btn.setToolTip(_FETCH_TIP[Source.CHESSCOM])
        self._fetch_btn.clicked.connect(self._on_fetch_clicked)
        top.addWidget(self._fetch_btn)

        self._preview_btn = QPushButton("🖼  Preview image")
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self._on_preview_image)
        top.addWidget(self._preview_btn)

        self._history_btn = QPushButton("⏱  History")
        self._history_btn.setEnabled(False)
        self._history_btn.clicked.connect(self._on_history_clicked)
        top.addWidget(self._history_btn)
        self._refresh_snapshot_buttons()

        self._analyze_btn = QPushButton("⚡  Analyze")
        self._analyze_btn.setToolTip("1-second quick analysis, 3 lines")
        self._analyze_btn.clicked.connect(self._on_analyze)
        top.addWidget(self._analyze_btn)

        self._whatif_btn = QPushButton("⑂  What-if")
        self._whatif_btn.setToolTip(
            "Open the analysed position as an expandable move tree — branch, "
            "re-branch and zoom around it"
        )
        self._whatif_btn.clicked.connect(self._enter_whatif)
        # Hidden until an analysis has actually finished: the tree grows out
        # of those three lines and out of nothing else.
        self._whatif_btn.setVisible(False)
        top.addWidget(self._whatif_btn)

        self._flip_btn = QPushButton("⇅  Flip")
        self._flip_btn.setToolTip("Toggle White's / Black's point of view")
        self._flip_btn.clicked.connect(self._board.toggle_flip)
        top.addWidget(self._flip_btn)

        self._theme_btn = QPushButton()
        self._theme_btn.clicked.connect(self._on_theme_toggle)
        top.addWidget(self._theme_btn)
        top.addStretch(1)

        sep = QFrame()
        sep.setObjectName("vSep")
        sep.setFrameShape(QFrame.Shape.VLine)
        top.addWidget(sep)

        opacity_caption = QLabel("Opacity")
        opacity_caption.setObjectName("muted")
        top.addWidget(opacity_caption)
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_slider.setValue(100)
        self._opacity_slider.setFixedWidth(110)
        self._opacity_slider.valueChanged.connect(self._on_opacity)
        top.addWidget(self._opacity_slider)
        self._opacity_label = QLabel("100%")
        self._opacity_label.setObjectName("muted")
        self._opacity_label.setFixedWidth(36)
        top.addWidget(self._opacity_label)

        self._pin_check = QCheckBox("Always on top")
        self._pin_check.toggled.connect(self._on_pin_toggled)
        top.addWidget(self._pin_check)
        layout.addLayout(top)

        # -- middle: palette | eval bar | board | analysis panel -------------
        middle = QHBoxLayout()
        middle.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(6)
        caption = QLabel("PIECES")
        caption.setObjectName("caption")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(caption)
        self._palette = PiecePalette(self._board.renderer)
        left.addWidget(self._palette, 0, Qt.AlignmentFlag.AlignHCenter)
        left.addStretch(1)
        middle.addLayout(left, 0)

        self._eval_bar = EvalBar(Qt.Orientation.Vertical)
        middle.addWidget(self._eval_bar, 0)

        middle.addWidget(self._board, 1)
        self._panel = AnalysisPanel(self._board, eval_bar=self._eval_bar,
                                    engine=self._engine)
        middle.addWidget(self._panel, 0)
        layout.addLayout(middle, 1)

        # -- bottom: FEN + side-to-move --------------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        fen_caption = QLabel("FEN")
        fen_caption.setObjectName("muted")
        bottom.addWidget(fen_caption)
        self._fen_edit = QLineEdit(self._board.fen())
        self._fen_edit.setObjectName("fenEdit")
        self._fen_edit.returnPressed.connect(self._on_apply_fen)
        bottom.addWidget(self._fen_edit, 1)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self._on_apply_fen)
        bottom.addWidget(self._apply_btn)
        self._side_btn = QPushButton()
        self._side_btn.setObjectName("sideChip")
        self._side_btn.setToolTip("Click to switch the side to move")
        self._side_btn.clicked.connect(self._board.toggle_side_to_move)
        self._refresh_side()
        bottom.addWidget(self._side_btn)
        layout.addLayout(bottom)

        # -- signals ----------------------------------------------------------
        self._board.boardEdited.connect(self._on_board_edited)
        self._board.armedChanged.connect(self._palette.sync)
        self._palette.piecePicked.connect(self._board.set_armed)
        self._board.moveProposed.connect(self._panel.on_move_proposed)
        self._engine.busyChanged.connect(self._on_engine_busy)
        self._panel.positionChanged.connect(self._on_playback_position)
        self._panel.analyzedChanged.connect(self._on_analyzed_changed)

        # -- pages: board (0) and the what-if graph (1) -----------------------
        self._whatif = WhatIfView(self._engine, self._board.renderer)
        self._whatif.exited.connect(self._exit_whatif)
        self._stack = QStackedWidget()
        self._stack.addWidget(board_page)
        self._stack.addWidget(self._whatif)
        self.setCentralWidget(self._stack)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — fetch a live position or edit the board", 6000)

    # ----------------------------------------------------------------- source
    def _restore_source(self, source: Source) -> None:
        """Select ``source`` without writing it back to QSettings."""
        idx = self._source_box.findData(source.value)
        self._source_box.blockSignals(True)
        self._source_box.setCurrentIndex(idx if idx >= 0 else 0)
        self._source_box.blockSignals(False)
        self._apply_source(source)

    def _apply_source(self, source: Source) -> None:
        self._source = source
        self._fetch_btn.setToolTip(_FETCH_TIP[source])
        self._refresh_snapshot_buttons()

    def _on_source_changed(self, index: int) -> None:
        source = Source.from_value(self._source_box.itemData(index))
        self._apply_source(source)
        QSettings().setValue("source", source.value)
        self.statusBar().showMessage(f"Position source: {source.label}", 4000)

    # ------------------------------------------------------------- snapshots
    def _set_board_image(self, image: BoardImage | None) -> None:
        """Replace the stored board image unconditionally.

        A newer fetch always wins: an image from a different ply is stale even
        if it is only seconds old, so the ply — not the TTL — is the primary
        invalidation rule. The TTL timer is only a backstop against showing a
        long-forgotten snapshot as if it were current.
        """
        self._board_image = image
        self._image_timer.stop()
        if image is not None:
            self._image_timer.start()
        self._refresh_snapshot_buttons()

    def _on_image_ttl(self) -> None:
        image = self._board_image
        if image is not None and image.is_stale():
            self._board_image = None
            self._refresh_snapshot_buttons()

    def _refresh_snapshot_buttons(self) -> None:
        """Gate the duolingo-only buttons.

        Two different kinds of "not now":

        * the chess.com source is selected — the control does not apply at all,
          so it is struck through as well as greyed out (a hover tooltip says
          why). Any snapshot already in memory is kept, so switching back to
          duolingo makes it available again;
        * duolingo is selected but nothing has been fetched yet — plain disabled.
        """
        if self._source is not Source.DUOLINGO:
            self._set_snapshot_button(
                self._preview_btn, False, struck=True,
                tip="Board-image preview is duolingo-only — chess.com gives a "
                    "position, not a board picture",
            )
            self._set_snapshot_button(
                self._history_btn, False, struck=True,
                tip="Game-history scrubbing is duolingo-only — chess.com sends "
                    "the current position, not the move list",
            )
            return

        image = self._board_image
        if image is None:
            self._set_snapshot_button(
                self._preview_btn, False,
                tip=f"Fetch from {Source.DUOLINGO.label} to capture the board image",
            )
            self._set_snapshot_button(
                self._history_btn, False,
                tip=f"Fetch from {Source.DUOLINGO.label} to get the move history",
            )
            return

        self._set_snapshot_button(
            self._preview_btn, True,
            tip=f"{image.width}×{image.height} PNG · {image.caption()}",
        )
        self._set_snapshot_button(
            self._history_btn, bool(image.moves),
            tip=(f"Scrub all {image.ply} plies of the fetched game"
                 if image.moves else "No moves played yet"),
        )

    @staticmethod
    def _set_snapshot_button(button: QPushButton, available: bool,
                             tip: str, struck: bool = False) -> None:
        button.setEnabled(available)
        button.setToolTip(tip)
        font = button.font()
        if font.strikeOut() != struck:
            font.setStrikeOut(struck)
            button.setFont(font)

    def _on_preview_image(self) -> None:
        if self._board_image is None:
            return
        ImagePreviewDialog(self._board_image, self).exec()

    def _on_history_clicked(self) -> None:
        image = self._board_image
        if image is None or not image.moves:
            return
        if self._panel.enter_full_game(list(image.moves)):
            self.statusBar().showMessage(
                f"Browsing {image.ply} plies from the start position — "
                "Esc returns to the live position", 8000
            )

    # ---------------------------------------------------------------- actions
    def _refresh_side(self) -> None:
        white = self._board.board.turn == chess.WHITE
        self._side_btn.setText(_SIDE_LABEL[self._board.board.turn])
        self._side_btn.setProperty("side", "w" if white else "b")
        theme.repolish(self._side_btn)

    def _on_fetch_clicked(self) -> None:
        if self._fetch_thread is not None:
            return
        source = self._source
        self._fetch_btn.setEnabled(False)
        self._source_box.setEnabled(False)
        if source is Source.DUOLINGO:
            self.statusBar().showMessage(
                "Fetching the duolingo board image + move history via WebBridge…"
            )
        else:
            self.statusBar().showMessage("Fetching live FEN from chess.com via WebBridge…")

        self._fetch_thread = QThread(self)
        self._fetch_worker = FetchWorker(source)
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
        self._source_box.setEnabled(True)

    def _on_fetch_ok(self, data: dict) -> None:
        if "png" in data:                      # duolingo: snapshot + history
            self._on_duolingo_ok(data)
        else:                                  # chess.com: a bare live FEN
            self._on_fen_ok(data)

    def _on_fen_ok(self, data: dict) -> None:
        fen = data["fen"]
        try:
            self._board.set_fen(fen)
        except ValueError as exc:
            self._on_fetch_err(f"Invalid FEN from browser: {exc}")
            return
        self._fen_edit.setText(fen)
        self._refresh_side()
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

    def _on_duolingo_ok(self, data: dict) -> None:
        fen = data["fen"]
        try:
            self._board.set_fen(fen)
        except ValueError as exc:
            self._on_fetch_err(f"Invalid FEN derived from the duolingo history: {exc}")
            return
        self._fen_edit.setText(fen)
        self._refresh_side()
        self._panel.on_position_changed()
        # The image and the history came back from one evaluate, so the PNG
        # belongs to exactly this ply; a new fetch replaces it outright.
        self._set_board_image(image_from_snapshot(data))

        who = "White" if self._board.board.turn == chess.WHITE else "Black"
        parts = [f"duolingo · ply {data['ply']}", f"{who} to move"]
        if data.get("recovered"):
            # Duolingo commits a ply to its move history only when the next user
            # move is submitted (in practice the opponent's reply), so the ply
            # was read off the board instead of leaving the position behind it.
            parts.append(f"{data['recovered']} read off the board")
        if data.get("status"):
            parts.append(f"status {data['status']}")
        parts.append("image + history ready")
        message = "Position loaded · " + " · ".join(parts)
        if data.get("canvas_state") == "unreconciled":
            message += ("  ⚠ the board on screen could not be lined up with "
                        "Duolingo's move history — fetch again once it settles")
        self.statusBar().showMessage(message, 12000)

    def _on_fetch_err(self, message: str) -> None:
        self.statusBar().showMessage("Fetch failed", 8000)
        if self._source is Source.DUOLINGO:
            hint = (
                "Make sure the WebBridge daemon is running and a duolingo chess "
                "match is open in the active tab. You can also edit the board "
                "below manually."
            )
            title = "Fetch failed · duolingo"
        else:
            hint = (
                "Make sure the WebBridge daemon is running and you have a chess.com "
                "game open in the active tab. You can also edit the board below manually."
            )
            title = "Fetch failed · chess.com"
        QMessageBox.warning(self, title, f"Could not read the live position.\n\n{message}\n\n{hint}")

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

    # ------------------------------------------------------------- what-if
    def _on_analyzed_changed(self, ready: bool) -> None:
        """An analysis finished (or went stale): offer the tree, or take it away."""
        self._whatif_btn.setVisible(ready)
        if ready:
            return
        if self._whatif.is_active():
            self._exit_whatif()
        self._whatif.on_anchor_lost()

    def _enter_whatif(self) -> None:
        fen = self._panel.analyzed_fen()
        lines = self._panel.lines()
        if not fen or not lines:
            self.statusBar().showMessage(
                "What-if needs an analysis — press Analyze first", 5000)
            return
        # Never anchor the tree to a replayed ply of an engine line: close the
        # explorer first so the analysed position really is what is on the board.
        self._panel.leave_playback()
        self._whatif.enter(fen, lines, self._board.flipped)
        self._stack.setCurrentIndex(1)
        self._whatif.set_active(True)
        self.statusBar().showMessage(
            "What-if — click a node to see that position, Enter to lay out its "
            "candidates, drag a piece to branch, Esc returns to the board", 12000)

    def _exit_whatif(self) -> None:
        """Back to the board, on the node the cursor was last on.

        This only *shows* another position — exactly like replaying a line —
        so the analysis and the tree both survive and the view can be entered
        again where you left it. A tree is only dropped when the analysed
        position itself changes.
        """
        if not self._whatif.is_active():
            return
        self._whatif.set_active(False)
        self._stack.setCurrentIndex(0)
        fen = self._whatif.cursor_fen()
        if fen and fen != self._board.fen():
            try:
                self._board.set_fen(fen)
            except ValueError:
                fen = None
            if fen:
                self._on_playback_position(fen)
                self.statusBar().showMessage(
                    "Board moved to the what-if cursor — the analysis and the "
                    "tree are still there, so What-if resumes where you were",
                    10000)
        self._board.setFocus()

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

    def _refresh_theme_btn(self) -> None:
        """The button shows the theme you switch *to* by clicking it."""
        if theme.current() == "dark":
            self._theme_btn.setText("☀  Light")
            self._theme_btn.setToolTip("Switch to the light theme")
        else:
            self._theme_btn.setText("🌙  Dark")
            self._theme_btn.setToolTip("Switch to the dark theme")

    def _on_theme_toggle(self) -> None:
        new = "light" if theme.current() == "dark" else "dark"
        theme.apply(new, QApplication.instance())
        QSettings().setValue("theme", new)
        self._refresh_theme_btn()
        # painted widgets read the palette at paint time; force a repaint
        self._board.update()
        self._eval_bar.update()
        self._whatif.refresh_theme()

    def _on_board_edited(self, fen: str) -> None:
        self._fen_edit.setText(fen)
        self._refresh_side()
        self._panel.on_position_changed()

    def _on_playback_position(self, fen: str) -> None:
        """Playback (an engine line or a game history) moved the board to some
        ply: mirror it in the FEN box, without disturbing the playback state."""
        self._fen_edit.setText(fen)
        self._refresh_side()

    def _on_apply_fen(self) -> None:
        fen = self._fen_edit.text().strip()
        try:
            self._board.set_fen(fen)
        except ValueError as exc:
            self.statusBar().showMessage(f"Invalid FEN: {exc}", 8000)
            self._fen_edit.setStyleSheet("border: 1px solid #d9534f;")
            return
        self._fen_edit.setStyleSheet("")
        self._refresh_side()
        self._panel.on_position_changed()
        self.statusBar().showMessage("Position applied", 4000)

    def closeEvent(self, event) -> None:
        self._image_timer.stop()
        if self._fetch_thread is not None:
            self._fetch_thread.quit()
            self._fetch_thread.wait(1000)
        self._whatif.shutdown()
        self._panel.shutdown()
        super().closeEvent(event)
