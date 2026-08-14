"""Right-hand analysis panel: quick Stockfish eval with clickable lines,
board playback of any line, and a blunder-check mode where the user proposes
a move on the board and sees the centipawn loss.

Design
------
* ``analyze(fen)`` starts a 1-second, 3-line MultiPV search in a worker
  thread and updates the panel live. No parameter prompts.
* The board mirrors the search: each line's first move is drawn as a faint,
  semi-transparent numbered arrow (1/2/3) straight onto the board, so you can
  see at a glance where the engine wants to play. The arrows appear live as
  lines come in, hide while replaying a line, and come back when you exit
  playback.
* Each engine line is a card (rank badge, best move, eval chip, depth,
  preview). Clicking a card opens the *continuation explorer*: the full PV
  as a row of clickable move pills, a scrub slider, and compact transport
  buttons (or Left/Right/Esc keys) — no more one-ply-at-a-time stepping.
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
from PyQt6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QPainterPath, QShortcut,
)
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)

from . import theme
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


def _white_relative(info: dict, stm: bool) -> tuple[str, str]:
    """(chip text, tone) for an eval, always from White's perspective."""
    mate = info.get("score_mate")
    if mate is not None:
        white_better = (mate > 0) == (stm == chess.WHITE)
        return f"M{abs(mate)}", "white" if white_better else "black"
    cp = info.get("score_cp")
    if cp is None:
        return "?", "even"
    if stm == chess.BLACK:
        cp = -cp
    tone = "white" if cp > 15 else "black" if cp < -15 else "even"
    return f"{cp / 100.0:+.2f}", tone


class EvalBar(QWidget):
    """White-vs-Black advantage bar. Vertical (lichess-style, docked beside
    the board) or horizontal (standalone fallback)."""

    def __init__(self, orientation: Qt.Orientation = Qt.Orientation.Horizontal,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._vertical = orientation == Qt.Orientation.Vertical
        if self._vertical:
            self.setFixedWidth(30)
            self.setMinimumHeight(160)
        else:
            self.setFixedHeight(22)
            self.setMinimumWidth(160)
        self._frac = 0.5
        self._text = ""

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
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        radius = 7.0
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, w - 1.0, h - 1.0, radius, radius)
        p.setClipPath(path)
        p.fillRect(self.rect(), QColor(theme.EVAL_DARK))
        if self._vertical:                      # white grows from the bottom
            white_h = int(h * self._frac)
            if white_h > 0:
                p.fillRect(0, h - white_h, w, white_h, QColor(theme.EVAL_LIGHT))
        else:
            white_w = int(w * self._frac)
            if white_w > 0:
                p.fillRect(0, 0, white_w, h, QColor(theme.EVAL_LIGHT))
        p.setClipping(False)
        p.setPen(QColor(theme.BORDER_SOFT))
        p.drawPath(path)

        if self._text:
            f = QFont(self.font().family(), 8, QFont.Weight.Bold)
            p.setFont(f)
            tw = p.fontMetrics().horizontalAdvance(self._text) + 8
            th = p.fontMetrics().height() + 2
            chip = QPainterPath()
            chip.addRoundedRect((w - tw) / 2, (h - th) / 2, tw, th, 4, 4)
            p.setPen(Qt.PenStyle.NoPen)
            chip_color = QColor(theme.EVAL_CHIP_BG)
            chip_color.setAlpha(215)
            p.setBrush(chip_color)
            p.drawPath(chip)
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
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


def _loss_tone(loss: float | None) -> str:
    if loss is None or loss < 30:
        return "good"
    if loss < 100:
        return "inaccuracy"
    if loss < 300:
        return "mistake"
    return "blunder"


def _cp_text(cp: float) -> str:
    """Format centipawns from a fixed colour's perspective, mate-aware."""
    if abs(cp) >= 90000:
        return "M" if cp > 0 else "-M"
    return f"{cp / 100.0:+.2f}"


def _number_prefix(stm: bool, fullmove: int) -> str:
    """Move-number prefix: '5. ' when White is to move, '5… ' when Black
    is to move (the ellipsis fills the slot where White's move would go)."""
    return f"{fullmove}. " if stm == chess.WHITE else f"{fullmove}… "


def _numbered_preview(san_moves: list[str], stm: bool, fullmove: int,
                      limit: int = 7) -> str:
    """Continuation as numbered pairs — one white + one black move per line.

    The best move is shown separately (in the card's big label), so this
    starts at the reply. When the position is White to move the first
    continuation move is Black's reply and gets the 'N… ' marker.
    """
    cont = san_moves[1:limit]
    if not cont:
        return ""
    lines: list[str] = []
    i, n = 0, fullmove
    if stm == chess.WHITE:
        lines.append(f"{n}… {cont[0]}")      # Black's reply to move n
        i, n = 1, n + 1                      # next move is White's n+1
    else:
        n += 1                               # continuation starts with White's move n+1
    while i < len(cont):
        w = cont[i]
        b = cont[i + 1] if i + 1 < len(cont) else None
        lines.append(f"{n}. {w}" + (f" {b}" if b else ""))
        i += 2
        n += 1
    if len(san_moves) > limit:
        lines[-1] += " …"
    return "\n".join(lines)


class LineCard(QFrame):
    """One engine line: rank badge, best move, eval chip, depth, preview."""

    clicked = pyqtSignal(int)          # multipv number

    def __init__(self, pv_no: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.pv_no = pv_no
        self.setObjectName("lineCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        self._badge = QLabel(f"#{pv_no}")
        self._badge.setObjectName("rankBadge")
        self._badge.setProperty("first", pv_no == 1)
        self._badge.setFixedSize(22, 22)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._move = QLabel("–")
        self._move.setObjectName("lineMove")
        self._chip = QLabel("?")
        self._chip.setObjectName("evalChip")
        self._preview = QLabel("")
        self._preview.setObjectName("linePreview")
        self._depth = QLabel("")
        self._depth.setObjectName("lineDepth")

        lay.addWidget(self._badge)
        lay.addWidget(self._move)
        lay.addWidget(self._chip)
        lay.addWidget(self._preview, 1)
        lay.addWidget(self._depth)

    def set_data(self, info: dict, san_moves: list[str], stm: bool,
                 fullmove: int = 1) -> None:
        if san_moves:
            self._move.setText(_number_prefix(stm, fullmove) + san_moves[0])
        else:
            self._move.setText("–")
        text, tone = _white_relative(info, stm)
        self._chip.setText(text)
        self._chip.setProperty("tone", tone)
        theme.repolish(self._chip)
        self._preview.setText(_numbered_preview(san_moves, stm, fullmove))
        depth = info.get("depth")
        self._depth.setText(f"d{depth}" if depth else "")

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", on)
        theme.repolish(self)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.pv_no)
        super().mouseReleaseEvent(event)


class LineList(QWidget):
    """Vertical stack of LineCards (keeps a ``count()`` for the smoke test)."""

    lineClicked = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)
        self._cards: dict[int, LineCard] = {}
        self._lay.addStretch(1)

    def clear(self) -> None:
        for card in self._cards.values():
            self._lay.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def set_lines(self, fen: str, lines: dict[int, dict], stm: bool) -> None:
        self.clear()
        try:
            fullmove = chess.Board(fen).fullmove_number
        except ValueError:
            fullmove = 1
        for pv_no in sorted(lines):
            info = lines[pv_no]
            card = LineCard(pv_no)
            card.set_data(info, san_pv(fen, info.get("pv", [])), stm, fullmove)
            card.clicked.connect(self.lineClicked)
            self._cards[pv_no] = card
            self._lay.insertWidget(self._lay.count() - 1, card)

    def set_selected(self, pv_no: int | None) -> None:
        for no, card in self._cards.items():
            card.set_selected(no == pv_no)

    def count(self) -> int:
        return len(self._cards)


class AnalysisPanel(QWidget):
    busyChanged = pyqtSignal(bool)

    def __init__(self, board: BoardWidget, eval_bar: EvalBar | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._board = board
        self.setFixedWidth(360)

        self._client: StockfishClient | None = None
        self._engine_name = "Stockfish"
        self._worker: _AnalyzeWorker | None = None
        self._thread: QThread | None = None
        self._stop_event: threading.Event | None = None
        self._busy = False
        self._lines: dict[int, dict] = {}
        self._analyzed_fen: str | None = None
        self._fen_side = chess.WHITE
        self._fen_fullmove = 1

        # playback state
        self._base_fen: str | None = None
        self._play_moves: list[chess.Move] = []
        self._pills: list[QPushButton] = []
        self._ply = -1
        self._timer = QTimer(self)
        self._timer.setInterval(_PLAY_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_tick)

        self._eval_bar = eval_bar
        self._eval_bar_owned = eval_bar is None
        if self._eval_bar_owned:
            self._eval_bar = EvalBar(Qt.Orientation.Horizontal)

        self._build_ui()

        # keyboard control while replaying a line (enabled on demand)
        self._shortcuts: list[QShortcut] = []
        for key, slot in (
            (Qt.Key.Key_Left, self._on_prev),
            (Qt.Key.Key_Right, self._on_next),
            (Qt.Key.Key_Escape, self._exit_playback),
        ):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)
            sc.setEnabled(False)
            self._shortcuts.append(sc)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 6, 8, 6)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._title = QLabel("Stockfish")
        self._title.setObjectName("panelTitle")
        header.addWidget(self._title)
        header.addStretch(1)
        self._blunder_btn = QPushButton("⚠ Blunder check")
        self._blunder_btn.setObjectName("blunderBtn")
        self._blunder_btn.setCheckable(True)
        self._blunder_btn.setToolTip(
            "Propose a move on the board (click piece, then target) and see "
            "how many centipawns it loses vs. the engine's best."
        )
        self._blunder_btn.toggled.connect(self._on_blunder_toggled)
        header.addWidget(self._blunder_btn)
        layout.addLayout(header)

        if self._eval_bar_owned:
            layout.addWidget(self._eval_bar)

        self._status_label = QLabel("Idle — press Analyze")
        self._status_label.setObjectName("statusLabel")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._list = LineList()
        self._list.lineClicked.connect(self._on_line_clicked)
        layout.addWidget(self._list, 1)

        layout.addWidget(self._build_explorer())

        self._blunder_result = QLabel("")
        self._blunder_result.setObjectName("blunderResult")
        self._blunder_result.setWordWrap(True)
        self._blunder_result.setVisible(False)
        layout.addWidget(self._blunder_result)

    def _build_explorer(self) -> QFrame:
        """Continuation explorer: clickable move pills + scrub transport."""
        self._explorer = QFrame()
        self._explorer.setObjectName("explorer")
        ex = QVBoxLayout(self._explorer)
        ex.setContentsMargins(10, 8, 10, 10)
        ex.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self._ex_title = QLabel("")
        self._ex_title.setObjectName("explorerTitle")
        head.addWidget(self._ex_title, 1)
        self._exit_btn = QPushButton("✕")
        self._exit_btn.setObjectName("iconBtn")
        self._exit_btn.setToolTip("Back to the analysed position (Esc)")
        self._exit_btn.clicked.connect(self._exit_playback)
        head.addWidget(self._exit_btn)
        ex.addLayout(head)

        self._pills_host = QWidget()
        self._pills_host.setProperty("transparent", True)
        self._pills_grid = QGridLayout(self._pills_host)
        self._pills_grid.setContentsMargins(0, 0, 0, 0)
        self._pills_grid.setSpacing(2)
        # number | white | black  →  1 : 4.5 : 4.5
        self._pills_grid.setColumnStretch(0, 2)
        self._pills_grid.setColumnStretch(1, 9)
        self._pills_grid.setColumnStretch(2, 9)
        ex.addWidget(self._pills_host)

        scrub = QHBoxLayout()
        scrub.setSpacing(6)
        self._prev_btn = QPushButton("‹")
        self._prev_btn.setObjectName("iconBtn")
        self._prev_btn.setToolTip("Previous move (←)")
        self._prev_btn.clicked.connect(self._on_prev)
        self._play_btn = QPushButton("▶")
        self._play_btn.setObjectName("iconBtn")
        self._play_btn.setToolTip("Play / pause the line")
        self._play_btn.clicked.connect(self._on_play)
        self._next_btn = QPushButton("›")
        self._next_btn.setObjectName("iconBtn")
        self._next_btn.setToolTip("Next move (→)")
        self._next_btn.clicked.connect(self._on_next)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setToolTip("Scrub through the line")
        self._slider.valueChanged.connect(self._on_slider)
        self._ply_label = QLabel("")
        self._ply_label.setObjectName("plyLabel")
        for b in (self._prev_btn, self._play_btn, self._next_btn):
            scrub.addWidget(b)
        scrub.addWidget(self._slider, 1)
        scrub.addWidget(self._ply_label)
        ex.addLayout(scrub)

        self._explorer.setVisible(False)
        return self._explorer

    # ------------------------------------------------------------- engine
    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        path, name = pick_stockfish()
        client = StockfishClient(path)
        client.handshake()
        self._client = client
        self._engine_name = name
        self._title.setText(name)

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
        self._apply_overlays()
        self._analyzed_fen = fen
        self._fen_side = board.turn
        self._fen_fullmove = board.fullmove_number
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
            self.status(f"{self._engine_name} · best {result['bestmove']} — click a line to explore it")
        else:
            self.status("search stopped")

    def _populate_list(self) -> None:
        fen = self._analyzed_fen
        if not fen:
            return
        self._list.set_lines(fen, self._lines, self._fen_side)
        self._apply_overlays()

    def _apply_overlays(self) -> None:
        """Board: one faint numbered arrow per engine line's first move."""
        items: list[tuple[int, int, str]] = []
        for pv_no in sorted(self._lines):
            pv = self._lines[pv_no].get("pv")
            if not pv:
                continue
            try:
                move = chess.Move.from_uci(pv[0])
            except ValueError:
                continue
            items.append((move.from_square, move.to_square, str(pv_no)))
        self._board.set_move_overlays(items)

    # ------------------------------------------------------------ playback
    def _on_line_clicked(self, pv_no: int) -> None:
        if self._analyzed_fen is None:
            return
        info = self._lines.get(pv_no)
        if not info or not info.get("pv"):
            return
        try:
            board = chess.Board(self._analyzed_fen)
            moves = []
            for uci in info["pv"][:_PV_LIMIT]:
                m = board.parse_uci(uci)
                moves.append(m)
                board.push(m)
        except Exception:
            return
        if self._blunder_btn.isChecked():
            self._blunder_btn.setChecked(False)
        san = san_pv(self._analyzed_fen, info["pv"])
        chip, _tone = _white_relative(info, self._fen_side)
        first = (_number_prefix(self._fen_side, self._fen_fullmove) + san[0]) if san else ""
        title = f"Line #{pv_no} · {first} · {chip}"
        self._list.set_selected(pv_no)
        self._enter_playback(moves, title)

    @staticmethod
    def _move_rows(base_fen: str, moves: list[chess.Move]):
        """Explorer rows — one per full move — as (number, white, black).

        Each move cell is ``(text, ply)``; ``None`` means the cell is
        empty (e.g. no Black reply yet). When the base position is Black
        to move, the first row's White cell is the placeholder
        ``("…", None)`` — the ellipsis only ever occurs there.
        """
        b = chess.Board(base_fen)
        rows = []
        n = b.fullmove_number
        i = 0
        if b.turn == chess.BLACK and moves:
            rows.append((f"{n}.", ("…", None), (b.san(moves[0]), 0)))
            b.push(moves[0])
            i, n = 1, n + 1
        while i < len(moves):
            white = (b.san(moves[i]), i)
            b.push(moves[i])
            black = (b.san(moves[i + 1]), i + 1) if i + 1 < len(moves) else None
            if black is not None:
                b.push(moves[i + 1])
            rows.append((f"{n}.", white, black))
            i += 2
            n += 1
        return rows

    def _enter_playback(self, moves: list[chess.Move], title: str) -> None:
        self._exit_playback(restore=False)
        self._board.set_move_overlays([])       # hide candidate arrows while replaying
        self._base_fen = self._analyzed_fen or self._board.fen()
        self._play_moves = moves
        self._board.set_fen(self._base_fen)     # show the base position first
        self._board.set_interactive(False)
        self._board.set_propose_mode(False)

        self._ex_title.setText(title)
        self._build_pills()
        self._slider.blockSignals(True)
        self._slider.setRange(0, len(moves))
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        self._explorer.setVisible(True)
        for sc in self._shortcuts:
            sc.setEnabled(True)
        self._show_ply(-1)

    def _build_pills(self) -> None:
        while self._pills_grid.count():
            item = self._pills_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._pills = []
        for r, (num, white, black) in enumerate(
            self._move_rows(self._base_fen, self._play_moves)
        ):
            num_lbl = QLabel(num)
            num_lbl.setObjectName("moveNum")
            num_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._pills_grid.addWidget(num_lbl, r, 0)
            for c, cell in ((1, white), (2, black)):
                if cell is None:
                    continue
                text, ply = cell
                if ply is None:                     # the '…' placeholder cell
                    ell = QLabel("…")
                    ell.setObjectName("moveNum")
                    ell.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    self._pills_grid.addWidget(ell, r, c)
                    continue
                pill = QPushButton(text)
                pill.setObjectName("movePill")
                pill.setToolTip("Jump to this move")
                pill.clicked.connect(lambda _checked=False, idx=ply: self._on_pill(idx))
                self._pills_grid.addWidget(pill, r, c)
                self._pills.append(pill)

    def _on_pill(self, idx: int) -> None:
        self._timer.stop()
        self._show_ply(idx)

    def _on_slider(self, value: int) -> None:
        self._timer.stop()
        self._show_ply(value - 1)       # slider 0 == base position (ply -1)

    def _show_ply(self, i: int) -> None:
        if not self._play_moves:
            return
        total = len(self._play_moves)
        target = max(-1, min(i, total - 1))
        if target != self._ply:
            self._apply_to_ply(target)
            self._ply = target
        last_san = self._san_at(target) if target >= 0 else None

        for idx, pill in enumerate(self._pills):
            current = idx == target
            if (pill.property("current") or False) != current:
                pill.setProperty("current", current)
                theme.repolish(pill)
        self._slider.blockSignals(True)
        self._slider.setValue(target + 1)
        self._slider.blockSignals(False)
        self._ply_label.setText(f"{target + 1}/{total}")

        if self._ply >= 0:
            self.status(f"Line · move {self._ply + 1}/{total} · {last_san}")
        else:
            self.status(f"Line · base position (0/{total})")
        self._prev_btn.setEnabled(self._ply >= 0)
        self._next_btn.setEnabled(self._ply < total - 1)
        self._play_btn.setText("⏸" if self._timer.isActive() else "▶")

    def _apply_to_ply(self, target: int) -> None:
        """Bring the board to ``target`` plies. A single forward step slides
        the piece with ``animate_move``; any other jump rebuilds the position
        from the base FEN."""
        if target == self._ply + 1:
            move = self._play_moves[target]
            try:
                self._board.animate_move(move)
            except Exception:                    # noqa: BLE001 - resync fallback
                b = chess.Board(self._base_fen)
                for k in range(target + 1):
                    b.push(self._play_moves[k])
                self._board.set_fen(b.fen())
            self._board.set_last_move(move.from_square, move.to_square)
            self._board.set_arrow(move.from_square, move.to_square)
            return
        b = chess.Board(self._base_fen)
        last = None
        for k in range(target + 1):
            last = self._play_moves[k]
            b.push(last)
        self._board.set_fen(b.fen())
        if last is not None:
            self._board.set_last_move(last.from_square, last.to_square)
            self._board.set_arrow(last.from_square, last.to_square)
        else:
            self._board.clear_last_move()
            self._board.clear_arrow()

    def _san_at(self, idx: int) -> str:
        """SAN of the move at ``idx`` in the current playback line."""
        b = chess.Board(self._base_fen)
        for k in range(idx):
            b.push(self._play_moves[k])
        return b.san(self._play_moves[idx])

    def _on_prev(self) -> None:
        if self._ply >= 0:
            self._timer.stop()
            self._show_ply(self._ply - 1)

    def _on_next(self) -> None:
        if self._ply < len(self._play_moves) - 1:
            self._timer.stop()
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
        self._board.stop_animation()
        if restore and self._base_fen and self._play_moves:
            try:
                self._board.set_fen(self._base_fen)
            except ValueError:
                pass
            self._board.clear_last_move()
        self._board.set_interactive(True)
        self._board.set_propose_mode(self._blunder_btn.isChecked())
        self._board.clear_arrow()
        self._play_moves = []
        self._base_fen = None
        self._ply = -1
        self._explorer.setVisible(False)
        self._list.set_selected(None)
        for sc in self._shortcuts:
            sc.setEnabled(False)
        if restore:
            self._apply_overlays()      # show the candidate arrows again

    # ------------------------------------------------------- blunder check
    def _on_blunder_toggled(self, on: bool) -> None:
        if on:
            self._exit_playback()
            self._board.set_propose_mode(True)
            self._set_blunder_text(
                "Click a piece of the side to move, then its destination square."
            )
            self.status("Blunder check on — propose a move on the board")
        else:
            self._board.set_propose_mode(False)
            self._set_blunder_text("")
            self.status("Blunder check off")

    def _set_blunder_text(self, text: str, tone: str | None = None) -> None:
        self._blunder_result.setText(text)
        self._blunder_result.setProperty("tone", tone or "")
        theme.repolish(self._blunder_result)
        self._blunder_result.setVisible(bool(text))

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
        self._set_blunder_text(f"Checking {san} …")
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
            self._set_blunder_text(f"{san}: engine returned no evaluation")
            return
        loss = best_cp - cand_cp
        label = _loss_label(loss)
        self._set_blunder_text(
            f"{san}: best {_cp_text(best_cp)} → after {_cp_text(cand_cp)} · "
            f"loss {loss / 100.0:+.2f} · {label}",
            tone=_loss_tone(loss),
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
        self._apply_overlays()
        self._exit_playback(restore=False)
        self._eval_bar.set_eval({"score_cp": 0, "score_mate": None}, chess.WHITE)
        self._set_blunder_text("")
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
