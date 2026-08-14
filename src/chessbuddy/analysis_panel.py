"""Right-hand analysis panel: quick Stockfish eval with clickable lines,
board playback of any line, and a blunder-check mode where the user proposes
a move on the board and sees the centipawn loss.

Design
------
* ``analyze(fen)`` starts a 1-second, 3-line MultiPV search in a worker
  thread and updates the panel live. No parameter prompts.
* Clicking a line enters *playback*: the main board steps through that
  line's PV with previous / play / next controls (board editing disabled).
* The "Blunder check" toggle puts the board into propose mode; the user
  clicks a piece then its destination, and the engine evaluates the
  candidate move vs. the best move (two short searches) and reports the
  centipawn loss.
* A single engine job runs at a time; ``busyChanged`` lets the caller
  disable its buttons while a search is in flight.
"""
from __future__ import annotations

import threading

import chess
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from .board_widget import BoardWidget
from .engine import EngineError, StockfishClient, pick_stockfish, score_text

_PV_LIMIT = 12            # max plies shown / replayed per line
_MOVETIME_MS = 1000       # quick analysis budget
_MULTIPV = 3              # lines shown
_EVALTIME_MS = 400        # per-position budget for blunder check
_PLAY_INTERVAL_MS = 900   # playback auto-advance


def san_pv(fen: str, uci_moves: list[str], limit: int = _PV_LIMIT) -> list[str]:
    """Translate a UCI PV into SAN (falls back to UCI on any anomaly)."""
    board = chess.Board(fen)
    out: list[str] = []
    for uci in uci_moves[:limit]:
        try:
            move = board.parse_uci(uci)
            out.append(board.san(move))
            board.push(move)
        except Exception:
            out.append(uci)
    return out


class EvalBar(QWidget):
    """Horizontal bar: left = White advantage, right = Black advantage."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self._frac = 0.5
        self._text = ""
        self.setMinimumWidth(160)

    def set_eval(self, info: dict, side_to_move: bool) -> None:
        mate = info.get("score_mate")
        if mate is not None:
            white_wins = (mate > 0) == (side_to_move == chess.WHITE)
            self._frac = 1.0 if white_wins else 0.0
        else:
            cp = info.get("score_cp")
            if cp is None:
                return
            if side_to_move == chess.BLACK:
                cp = -cp
            self._frac = max(0.0, min(1.0, 0.5 + cp / 1000.0))
        self._text = score_text(info)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#8a8a8a"))
        white_w = int(w * self._frac)
        if white_w > 0:
            p.fillRect(0, 0, white_w, h, QColor("#f5f5f5"))
        p.setPen(QColor("#444444"))
        p.setFont(QFont(self.font().family(), 9, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        p.setPen(QColor("#222222"))
        p.drawLine(white_w, 0, white_w, h)
        p.end()


class _AnalyzeWorker(QObject):
    """Runs one engine job (``fn(stop, info_signal)``) off the GUI thread."""

    info = pyqtSignal(object)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, fn, stop_event: threading.Event):
        super().__init__()
        self._fn = fn
        self._stop = stop_event

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.done.emit(self._fn(self._stop, self.info))
        except Exception as exc:                  # noqa: BLE001 - surface any failure
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


def _cp_of(info: dict, color: bool) -> float | None:
    """Centipawns of ``info`` from ``color``'s perspective (mate -> +-100000)."""
    m = info.get("score_mate")
    if m is not None:
        raw = 100000 if m > 0 else -100000
    else:
        raw = info.get("score_cp")
        if raw is None:
            return None
    return raw if info.get("_stm") == color else -raw


def _loss_label(loss: float | None) -> str:
    if loss is None:
        return "—"
    if loss < 30:
        return "good move ✓"
    if loss < 100:
        return "inaccuracy"
    if loss < 300:
        return "mistake"
    return "BLUNDER ❌"


def _cp_text(cp: float) -> str:
    """Format centipawns from a fixed colour's perspective, mate-aware."""
    if abs(cp) >= 90000:
        return "M" if cp > 0 else "-M"
    return f"{cp / 100.0:+.2f}"


class AnalysisPanel(QWidget):
    busyChanged = pyqtSignal(bool)

    def __init__(self, board: BoardWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._board = board
        self.setFixedWidth(330)

        self._client: StockfishClient | None = None
        self._engine_name = "Stockfish"
        self._worker: _AnalyzeWorker | None = None
        self._thread: QThread | None = None
        self._stop_event: threading.Event | None = None
        self._busy = False
        self._lines: dict[int, dict] = {}
        self._analyzed_fen: str | None = None
        self._fen_side = chess.WHITE

        # playback state
        self._base_fen: str | None = None
        self._play_moves: list[chess.Move] = []
        self._ply = -1
        self._timer = QTimer(self)
        self._timer.setInterval(_PLAY_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_tick)

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Stockfish")
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self._blunder_btn = QPushButton("⚠ Blunder check")
        self._blunder_btn.setCheckable(True)
        self._blunder_btn.setToolTip(
            "Propose a move on the board (click piece, then target) and see "
            "how many centipawns it loses vs. the engine's best."
        )
        self._blunder_btn.toggled.connect(self._on_blunder_toggled)
        header.addWidget(self._blunder_btn)
        layout.addLayout(header)

        self._eval_bar = EvalBar()
        layout.addWidget(self._eval_bar)

        self._status_label = QLabel("Idle — press Analyze")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._status_label)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setWordWrap(False)
        self._list.itemClicked.connect(self._on_line_clicked)
        self._list.setToolTip("Click a line to replay it on the board")
        layout.addWidget(self._list, 1)

        # -- playback controls --------------------------------------------
        self._play_row = QWidget()
        play_l = QHBoxLayout(self._play_row)
        play_l.setContentsMargins(0, 0, 0, 0)
        play_l.setSpacing(4)
        self._prev_btn = QPushButton("⏮")
        self._prev_btn.setToolTip("Previous move")
        self._prev_btn.clicked.connect(self._on_prev)
        self._play_btn = QPushButton("▶")
        self._play_btn.setToolTip("Play / pause the line")
        self._play_btn.clicked.connect(self._on_play)
        self._next_btn = QPushButton("⏭")
        self._next_btn.setToolTip("Next move")
        self._next_btn.clicked.connect(self._on_next)
        self._exit_btn = QPushButton("✕ Exit")
        self._exit_btn.setToolTip("Return to the analysed position")
        self._exit_btn.clicked.connect(self._exit_playback)
        for b in (self._prev_btn, self._play_btn, self._next_btn):
            b.setFixedWidth(40)
        play_l.addWidget(self._prev_btn)
        play_l.addWidget(self._play_btn)
        play_l.addWidget(self._next_btn)
        play_l.addStretch(1)
        play_l.addWidget(self._exit_btn)
        self._play_row.setVisible(False)
        layout.addWidget(self._play_row)

        # -- blunder check result ------------------------------------------
        self._blunder_result = QLabel("")
        self._blunder_result.setWordWrap(True)
        self._blunder_result.setStyleSheet("font-size: 12px;")
        layout.addWidget(self._blunder_result)

    # ------------------------------------------------------------- engine
    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        path, name = pick_stockfish()
        client = StockfishClient(path)
        client.handshake()
        self._client = client
        self._engine_name = name

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busyChanged.emit(busy)

    def _start_job(self, fn, on_done) -> None:
        self._stop_event = threading.Event()
        self._thread = QThread(self)
        self._worker = _AnalyzeWorker(fn, self._stop_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.info.connect(self._on_live_info)
        self._worker.done.connect(on_done)
        self._worker.done.connect(self._on_job_finished)
        self._worker.failed.connect(self._on_job_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_cleared)
        self._thread.start()
        self._set_busy(True)

    def _cancel_job(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._client is not None:
            self._client.stop()

    # ------------------------------------------------------------ analysis
    def analyze(self, fen: str) -> None:
        """Start a 1-second MultiPV search on ``fen`` (no dialogs, no params)."""
        if self._busy:
            self.status("Busy — wait for the current search")
            return
        try:
            board = chess.Board(fen)
        except ValueError:
            return
        try:
            self._ensure_client()
        except EngineError as exc:
            self._flash_error(str(exc))
            return

        self._exit_playback()
        self._lines.clear()
        self._list.clear()
        self._analyzed_fen = fen
        self._fen_side = board.turn
        self._eval_bar.set_eval({"score_cp": 0, "score_mate": None}, self._fen_side)
        self.status(f"{self._engine_name} · analyzing (1s)…")

        def run(stop, info_sig):
            return self._client.analyze(
                fen, movetime_ms=_MOVETIME_MS, multipv=_MULTIPV,
                on_info=lambda info: info_sig.emit(info), stop=stop,
            )

        self._start_job(run, self._on_analyze_done)

    def _on_live_info(self, info: dict) -> None:
        if self._analyzed_fen is None:
            return
        if info.get("pv"):
            self._lines[info["multipv"]] = info
            self._populate_list()
            if info["multipv"] == 1:
                self._eval_bar.set_eval(info, self._fen_side)
        d, nps = info.get("depth"), info.get("nps")
        bits = [f"{self._engine_name} · depth {d}" if d else self._engine_name]
        if nps:
            bits.append(f"{nps / 1e6:.1f}M nps")
        self.status(" · ".join(bits))

    def _on_analyze_done(self, result: dict) -> None:
        if self._analyzed_fen is None:
            return
        if result.get("bestmove"):
            self.status(f"{self._engine_name} · best {result['bestmove']}")
        else:
            self.status("search stopped")

    def _populate_list(self) -> None:
        fen = self._analyzed_fen
        if not fen:
            return
        self._list.clear()
        for pv_no in sorted(self._lines):
            info = self._lines[pv_no]
            san_moves = san_pv(fen, info.get("pv", []))
            move_san = san_moves[0] if san_moves else "-"
            cont = " ".join(san_moves[1:])
            depth = info.get("depth")
            head = f"#{pv_no}  {move_san}   {score_text(info)}"
            if depth:
                head += f"  (d{depth})"
            item = QListWidgetItem(head + ("\n    " + cont if cont else ""))
            item.setData(Qt.ItemDataRole.UserRole, pv_no)
            self._list.addItem(item)

    # ------------------------------------------------------------ playback
    def _on_line_clicked(self, item: QListWidgetItem) -> None:
        pv_no = item.data(Qt.ItemDataRole.UserRole)
        if pv_no is None or self._analyzed_fen is None:
            return
        info = self._lines.get(pv_no)
        if not info or not info.get("pv"):
            return
        try:
            board = chess.Board(self._analyzed_fen)
            moves = []
            for uci in info["pv"]:
                m = board.parse_uci(uci)
                moves.append(m)
                board.push(m)
        except Exception:
            return
        if self._blunder_btn.isChecked():
            self._blunder_btn.setChecked(False)
        self._enter_playback(moves)

    def _enter_playback(self, moves: list[chess.Move]) -> None:
        self._exit_playback(restore=False)
        self._base_fen = self._analyzed_fen or self._board.fen()
        self._play_moves = moves
        self._board.set_interactive(False)
        self._board.set_propose_mode(False)
        self._play_row.setVisible(True)
        self._show_ply(-1)

    def _show_ply(self, i: int) -> None:
        if not self._play_moves:
            return
        self._ply = i
        b = chess.Board(self._base_fen)
        last = None
        last_san = None
        for k in range(i + 1):
            last = self._play_moves[k]
            last_san = b.san(last)
            b.push(last)
        self._board.set_fen(b.fen())
        if last is not None:
            self._board.set_last_move(last.from_square, last.to_square)
        else:
            self._board.clear_last_move()
        total = len(self._play_moves)
        if self._ply >= 0:
            self.status(f"Line {self._ply + 1}/{total} · {last_san}")
        else:
            self.status(f"Line · base position (0/{total})")
        self._prev_btn.setEnabled(self._ply >= 0)
        self._next_btn.setEnabled(self._ply < total - 1)
        self._play_btn.setText("⏸" if self._timer.isActive() else "▶")

    def _on_prev(self) -> None:
        if self._ply >= 0:
            self._show_ply(self._ply - 1)

    def _on_next(self) -> None:
        if self._ply < len(self._play_moves) - 1:
            self._show_ply(self._ply + 1)

    def _on_play(self) -> None:
        if not self._play_moves:
            return
        if self._timer.isActive():
            self._timer.stop()
            self._play_btn.setText("▶")
            return
        if self._ply >= len(self._play_moves) - 1:
            self._show_ply(-1)
        self._timer.start()
        self._play_btn.setText("⏸")

    def _on_timer_tick(self) -> None:
        if self._ply >= len(self._play_moves) - 1:
            self._timer.stop()
            self._play_btn.setText("▶")
            return
        self._show_ply(self._ply + 1)

    def _exit_playback(self, restore: bool = True) -> None:
        self._timer.stop()
        if restore and self._base_fen and self._play_moves:
            try:
                self._board.set_fen(self._base_fen)
            except ValueError:
                pass
            self._board.clear_last_move()
        self._board.set_interactive(True)
        self._board.set_propose_mode(self._blunder_btn.isChecked())
        self._play_moves = []
        self._base_fen = None
        self._ply = -1
        self._play_row.setVisible(False)

    # ------------------------------------------------------- blunder check
    def _on_blunder_toggled(self, on: bool) -> None:
        if on:
            self._exit_playback()
            self._board.set_propose_mode(True)
            self._blunder_result.setText(
                "Click a piece of the side to move, then its destination square."
            )
            self.status("Blunder check on — propose a move on the board")
        else:
            self._board.set_propose_mode(False)
            self._blunder_result.setText("")
            self.status("Blunder check off")

    def on_move_proposed(self, move: chess.Move) -> None:
        """Evaluate a move proposed on the board vs. the engine's best."""
        if self._busy:
            self.status("Busy — wait for the current search")
            return
        fen = self._board.fen()
        try:
            board = chess.Board(fen)
            if not board.is_legal(move):
                return
            san = board.san(move)
        except ValueError:
            return
        try:
            self._ensure_client()
        except EngineError as exc:
            self._flash_error(str(exc))
            return

        after = chess.Board(fen)
        after.push(move)
        after_fen = after.fen()
        self._blunder_result.setText(f"Checking {san} …")
        self.status(f"Evaluating {san} vs. best…")

        def run(stop, _info_sig):
            stm = board.turn
            root = self._client.analyze(
                fen, movetime_ms=_EVALTIME_MS, multipv=1, stop=stop
            )["lines"].get(1)
            if stop.is_set() or root is None:
                return {"cancelled": True}
            cand = self._client.analyze(
                after_fen, movetime_ms=_EVALTIME_MS, multipv=1, stop=stop
            )["lines"].get(1)
            if stop.is_set() or cand is None:
                return {"cancelled": True}
            root = dict(root, _stm=stm)
            cand = dict(cand, _stm=after.turn)
            return {"root": root, "cand": cand, "san": san, "stm": stm}

        self._start_job(run, self._on_blunder_done)

    def _on_blunder_done(self, result: dict) -> None:
        if result.get("cancelled"):
            self.status("blunder check cancelled")
            return
        root, cand, san = result["root"], result["cand"], result["san"]
        best_cp = _cp_of(root, result["stm"])
        cand_cp = _cp_of(cand, result["stm"])
        if best_cp is None or cand_cp is None:
            self._blunder_result.setText(f"{san}: engine returned no evaluation")
            return
        loss = best_cp - cand_cp
        label = _loss_label(loss)
        self._blunder_result.setText(
            f"{san}: best {_cp_text(best_cp)} → after {_cp_text(cand_cp)} · "
            f"loss {loss / 100.0:+.2f} · {label}"
        )
        self.status(f"{san}: {label} (−{loss / 100.0:.2f})")

    # ------------------------------------------------------------- plumbing
    def status(self, text: str) -> None:
        self._status_label.setText(text)

    def on_position_changed(self) -> None:
        """The board was edited/fetched/applied: drop stale analysis and exit
        playback without overwriting the user's position."""
        self._lines.clear()
        self._list.clear()
        self._analyzed_fen = None
        self._exit_playback(restore=False)
        self._eval_bar.set_eval({"score_cp": 0, "score_mate": None}, chess.WHITE)
        self._blunder_result.setText("")
        self.status("Position changed — press Analyze")

    def _on_job_finished(self, _result) -> None:
        self._set_busy(False)

    def _on_job_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status("engine error")
        self._flash_error(message)

    def _on_worker_cleared(self) -> None:
        self._worker = None
        self._thread = None
        self._stop_event = None

    def _flash_error(self, message: str) -> None:
        QMessageBox.warning(self, "Stockfish", message)

    def shutdown(self) -> None:
        """Stop any running job and release the engine (call on window close)."""
        self._timer.stop()
        self._cancel_job()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        if self._client is not None:
            self._client.quit()
            self._client = None
