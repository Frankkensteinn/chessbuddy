"""Read Duolingo's board canvas as a grid of squares — and use it to catch the
ply Duolingo has not recorded yet.

Why this exists
---------------
A lesson / PvE chess match (``challenge challenge-chessMatch``) commits the
user's own move to ``challengeState.guess.moveHistory`` immediately, but the
opponent's scripted reply only lands in that list when the user submits their
*next* move. The canvas, meanwhile, is painted from the live game as soon as
the reply is played. So for the whole of the user's turn the two readings
disagree by exactly one ply, and a position derived from the history alone is a
move behind *and* names the wrong side to move.

Verified live on 2026-09-19 (lesson match, user playing white)::

    moveHistory   [d2d4, d7d5, c1f4, c8f5, e2e3]      <- 5 plies, "black to move"
    canvas        same position + ...a6                <- what the user sees

The reply is nowhere else in the page. Checked and found empty on that date:
the board component's own props (``inBoard``/``aboveBoard``/``belowBoard``),
``challengeState`` (only ``matchState`` + ``moveHistory``), the challenge
object's ``opponentMove``, ``challenge.match`` (never leaves its initial
state), the redux store (``player.challengeStates`` mirrors the same stale
list), every ``useRef`` on the canvas's ancestor chain, every ``boardFen`` /
UCI array anywhere in the fiber tree, and the DOM (Duolingo renders no move
list — only "Previous move" / "Next move" buttons with no notation). The canvas
is the only place the reply exists, so it is read here.

What is read, and why that is safe
---------------------------------
Only *occupancy*: which square is empty, which holds a white piece, which holds
a dark one. Piece identity is never guessed. The grid is used to pick, among
the **legal** moves of the position the history already describes, the single
one whose resulting occupancy reproduces the canvas exactly, square for square.
A misread can therefore never invent a move — it can only fail to match, which
is reported rather than papered over.

The canvas is drawn from the user's own side, so the same position can appear
as-drawn, rotated 180°, or mirrored. All four mappings are tried; the one that
fits the history's position best is used for the whole reconciliation, so a
per-candidate orientation can never mix two readings together.

Decoding uses ``QImage`` (PyQt6 is already a dependency and ``QImage`` is
thread-safe, unlike ``QPixmap``); it is imported lazily so this module stays
importable — and the rest of it testable — without Qt. Nothing here touches a
widget, so it can run in the fetch worker thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import chess

__all__ = [
    "Reconciliation", "STATE_CANVAS_BEHIND", "STATE_IN_SYNC", "STATE_RECOVERED",
    "STATE_UNREADABLE", "STATE_UNRECONCILED", "read_grid", "reconcile",
]

# --- reconciliation states -------------------------------------------------

STATE_IN_SYNC = "in-sync"
"""The canvas already shows the position the move history describes."""
STATE_CANVAS_BEHIND = "canvas-behind"
"""The canvas shows the position *before* the history's last ply (the board is
mid-animation). The history is the newer reading, so nothing is recovered."""
STATE_RECOVERED = "recovered"
"""The canvas showed exactly one legal ply more than the history — in practice
the opponent's reply. ``Reconciliation.uci`` holds it."""
STATE_UNRECONCILED = "unreconciled"
"""The canvas matches neither reading — a piece is probably mid-animation or in
the user's hand. The history's position is returned untouched, because a
guessed ply is worse than a known-stale one."""
STATE_UNREADABLE = "unreadable"
"""The canvas could not be read at all (no image, or no board found in it)."""

# --- how the canvas is measured --------------------------------------------

# The board is drawn inset from the canvas edge; measured live at two canvas
# sizes (1012 and 1118 square) the frame's left/top land at 0.0948 / 0.0957 of
# the canvas, and the squares sit a further 0.0066 of the frame width inside
# it. Sampling the middle half of each square tolerates being a few pixels off,
# so the exact ratios are not critical; the bounds are checked against
# _CELL_SANITY before they are trusted.
_FRAME_TO_SQUARES = 0.0066
_CELL_SANITY = (0.085, 0.115)     # cell / canvas size
_SAMPLE_LO, _SAMPLE_HI = 0.25, 0.75
_SAMPLE_TARGET = 13               # samples per axis inside one square

# Duolingo's palette, measured off the live canvas (2026-09-19): empty squares
# are #f2f4f5 and #ffffff, dark pieces #3d3d3d..#888888, white pieces #dae4eb
# with a #9fafba outline, and the last-move highlight #ffedaf.
#
# The white pieces' outline is the trap: #9fafba is dark enough (r=b=186) to
# pass any plain "is it dark" test, so the *light* test has to run first. It is
# keyed on the pieces' blue tint (b - r >= 8) instead, which no empty square and
# no highlight pixel has.
_DARK_MAX = 190            # every channel; black pieces top out at #888888
_LIGHT_MAX_R, _LIGHT_MIN_B_R, _LIGHT_MIN_B = 235, 8, 150
_DARK_FRAC, _LIGHT_FRAC = 0.05, 0.08

# A grid is accepted as "this position" only on an exact match; the alignment
# search needs a clear winner, so the runner-up must be this much worse.
_ALIGN_MARGIN = 4

_VIEWS = (
    ("as-drawn", lambda f, r: (f, r)),          # white at the bottom
    ("rotated", lambda f, r: (7 - f, 7 - r)),   # 180°: the other side's view
    ("white-up", lambda f, r: (f, 7 - r)),
    ("mirrored", lambda f, r: (7 - f, r)),
)
_FILES = "abcdefgh"


def _square_name(file_index: int, rank_index: int) -> str:
    return _FILES[file_index] + str(8 - rank_index)


@dataclass(frozen=True)
class Reconciliation:
    """Verdict of comparing the canvas against a move history."""

    state: str
    uci: str = ""                       # the recovered ply, when state is RECOVERED
    orientation: str = ""               # which canvas mapping was used
    mismatches: tuple[str, ...] = ()    # squares that did not line up
    detail: str = ""                    # human-readable extra context

    @property
    def settled(self) -> bool:
        """True when the snapshot does not need re-reading after the board
        settles: either it lined up, or re-reading cannot help."""
        return self.state != STATE_UNRECONCILED


# --- canvas -> grid --------------------------------------------------------

def _rgb_rows(png: bytes) -> tuple[int, int, int, bytes] | None:
    """``(width, height, stride, rgb_bytes)`` for a PNG, or None if unreadable."""
    try:
        from PyQt6.QtGui import QImage
    except ImportError:                                    # pragma: no cover
        return None
    image = QImage.fromData(png, "PNG")
    if image.isNull():
        return None
    # Format_RGB888 is tightly ordered per pixel (R, G, B) but rows are padded,
    # so the stride is kept and used for every row lookup.
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return None
    stride = image.bytesPerLine()
    pointer = image.constBits()
    pointer.setsize(stride * height)
    return width, height, stride, bytes(pointer)


def _is_background(r: int, g: int, b: int) -> bool:
    """The canvas is transparent outside the board (QImage turns that black)."""
    return r < 60 and g < 60 and b < 60


def _board_box(width: int, height: int, stride: int,
               raw: bytes) -> tuple[int, int, float] | None:
    """``(left, top, cell)`` of the 8x8 squares, or None if no board is drawn.

    The frame's edge is found by walking in from the middle of the canvas on
    both axes — the middle row/column crosses empty squares or pieces, never
    the padded edge — then the squares are inset from that frame.
    """
    def pixel(x: int, y: int) -> tuple[int, int, int]:
        i = y * stride + x * 3
        return raw[i], raw[i + 1], raw[i + 2]

    mid_y, mid_x = height // 2, width // 2
    left = next((x for x in range(width) if not _is_background(*pixel(x, mid_y))), None)
    right = next((x for x in range(width - 1, -1, -1)
                  if not _is_background(*pixel(x, mid_y))), None)
    top = next((y for y in range(height) if not _is_background(*pixel(mid_x, y))), None)
    if left is None or right is None or top is None or right - left < 64:
        return None
    inset = round((right - left) * _FRAME_TO_SQUARES)
    cell = (right - left - 2 * inset) / 8.0
    if not _CELL_SANITY[0] * width <= cell <= _CELL_SANITY[1] * width:
        # The frame was not found where it was expected — fall back to the
        # ratios the live canvas actually uses rather than sampling blind.
        cell, inset = 0.0993 * width, _FRAME_TO_SQUARES * width
    return int(left + inset), int(top + inset), cell


def read_grid(png: bytes) -> dict[str, str] | None:
    """Classify all 64 squares of the board canvas.

    Keys are square names, values ``""`` (empty), ``"w"`` (white piece) or
    ``"b"`` (dark piece), indexed as if the board were drawn white-side-down:
    ``a8`` is the top-left square. Which way the canvas really is drawn is
    resolved later, against a position we already trust. None when the canvas
    cannot be read at all.
    """
    decoded = _rgb_rows(png)
    if decoded is None:
        return None
    width, height, stride, raw = decoded
    box = _board_box(width, height, stride, raw)
    if box is None:
        return None
    left, top, cell = box
    step = max(2, int(cell / _SAMPLE_TARGET))
    lo, hi = _SAMPLE_LO, _SAMPLE_HI

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        # A geometry the sanity check let through can still run off the image on
        # the last rank; an out-of-range sample reads as "nothing here".
        if not (0 <= x < width and 0 <= y < height):
            return 0, 0, 0
        i = y * stride + x * 3
        return raw[i], raw[i + 1], raw[i + 2]

    grid: dict[str, str] = {}
    for rank_index in range(8):
        for file_index in range(8):
            x0 = int(left + (file_index + lo) * cell)
            x1 = int(left + (file_index + hi) * cell)
            y0 = int(top + (rank_index + lo) * cell)
            y1 = int(top + (rank_index + hi) * cell)
            dark = light = total = 0
            for x in range(x0, x1, step):
                for y in range(y0, y1, step):
                    r, g, b = pixel(x, y)
                    total += 1
                    if r <= _LIGHT_MAX_R and b >= _LIGHT_MIN_B and b - r >= _LIGHT_MIN_B_R:
                        light += 1
                    elif max(r, g, b) <= _DARK_MAX:
                        dark += 1
            if not total:
                grid[_square_name(file_index, rank_index)] = ""
            elif dark / total > _DARK_FRAC:
                grid[_square_name(file_index, rank_index)] = "b"
            elif light / total > _LIGHT_FRAC:
                grid[_square_name(file_index, rank_index)] = "w"
            else:
                grid[_square_name(file_index, rank_index)] = ""
    return grid


# --- grid vs a position ----------------------------------------------------

def _expected(board: chess.Board) -> dict[str, str]:
    out = {}
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        out[chess.square_name(square)] = (
            "" if piece is None else ("w" if piece.color == chess.WHITE else "b")
        )
    return out


def _view(grid: dict[str, str], transform) -> dict[str, str]:
    """Re-index a raw canvas grid into board coordinates."""
    out = {}
    for name, value in grid.items():
        file_index = _FILES.index(name[0])
        rank_index = 8 - int(name[1])
        vf, vr = transform(file_index, rank_index)
        out[_square_name(vf, vr)] = value
    return out


def _mismatches(expected: dict[str, str], measured: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted(sq for sq in expected if expected[sq] != measured.get(sq)))


def _replay(moves: list[str], upto: int | None = None) -> chess.Board | None:
    board = chess.Board()
    for uci in moves if upto is None else moves[:upto]:
        try:
            board.push_uci(uci)
        except (ValueError, TypeError):
            return None
    return board


def _align(grid: dict[str, str], board: chess.Board) -> tuple[str, dict[str, str]] | None:
    """Pick the canvas mapping that fits ``board``, or None if none stands out."""
    scored = sorted(
        ((len(_mismatches(_expected(board), _view(grid, fn))), name, fn)
         for name, fn in _VIEWS),
        key=lambda row: row[0],
    )
    (best, name, fn), second = scored[0], scored[1][0]
    if best > _ALIGN_MARGIN or second - best < _ALIGN_MARGIN:
        return None
    return name, _view(grid, fn)


def reconcile(moves: list[str], grid: dict[str, str] | None) -> Reconciliation:
    """Compare a move history against the board canvas.

    Returns the reconciled verdict; see the ``STATE_*`` constants. The caller
    decides what an unreconciled reading means — nothing here mutates ``moves``.
    """
    if grid is None:
        return Reconciliation(STATE_UNREADABLE, detail="no board found in the canvas")
    board = _replay(moves)
    if board is None:
        return Reconciliation(STATE_UNRECONCILED, detail="move history is not replayable")

    # An exact fit settles the orientation question on its own: whichever way
    # the canvas is drawn, the answer "the canvas is this position" is the same.
    for name, fn in _VIEWS:
        if not _mismatches(_expected(board), _view(grid, fn)):
            return Reconciliation(STATE_IN_SYNC, orientation=name)
    if moves:
        previous = _replay(moves, len(moves) - 1)
        if previous is not None:
            for name, fn in _VIEWS:
                if not _mismatches(_expected(previous), _view(grid, fn)):
                    return Reconciliation(
                        STATE_CANVAS_BEHIND, orientation=name,
                        detail="canvas still shows the position before the last ply",
                    )

    aligned = _align(grid, board)
    if aligned is None:
        return Reconciliation(
            STATE_UNRECONCILED,
            detail="canvas matches no orientation of the position",
        )
    name, measured = aligned
    wrong = _mismatches(_expected(board), measured)
    candidates = []
    for move in board.legal_moves:
        after = board.copy()
        after.push(move)
        if not _mismatches(_expected(after), measured):
            candidates.append(move.uci())
    if len(candidates) == 1:
        return Reconciliation(STATE_RECOVERED, uci=candidates[0], orientation=name)
    detail = (f"no legal move reproduces the canvas ({len(candidates)} do)"
              if candidates else
              f"canvas shows {len(wrong)} square(s) the position does not")
    return Reconciliation(STATE_UNRECONCILED, orientation=name,
                          mismatches=wrong, detail=detail)
