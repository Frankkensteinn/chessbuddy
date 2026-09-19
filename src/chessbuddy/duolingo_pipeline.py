"""Duolingo chess-match position pipeline.

Duolingo never hands over a game model as such — only a <canvas> the board is
rasterised onto and the React props of the board component. So one ``evaluate``
grabs everything at once:

    find_tab (borrow the active duolingo tab)
      -> evaluate JS (one call, atomically)
      -> base64 PNG + UCI moveHistory (+ Duolingo's own boardFen on PvP)
      -> replay the history from the standard start position -> FEN

**The move history is the source of truth for the FEN**; the PNG is only ever a
picture for the user to look at — with one exception. In a lesson match the
history is committed per *user* move, so while the user is thinking it is
missing the opponent's scripted reply that the canvas already shows (measured
live on 2026-09-19: history 5 plies ending on the user's move, canvas showing
the reply as well). ``duolingo_canvas`` reads that one ply back off the canvas;
see its module docstring for why the canvas is the only place the reply exists.

Two match kinds draw onto the same canvas, with different anchors:

* lesson / course challenges — container ``challenge challenge-chessMatch``,
  history under ``challengeState.guess.moveHistory``
* PvP matches — container ``challenge challenge-chessPvpMatch``, where
  ``challengeState.guess.moveHistory`` is **always empty**. The real history
  and a ready-made ``boardFen`` live in the board component's
  ``aboveBoard`` / ``belowBoard`` / ``inBoard`` props instead. So the container
  is matched by prefix and those props win over ``challengeState``. The two
  readings are cross-checked rather than trusted blindly — see
  :func:`_check_board_fen`, and :func:`_position_key` for the one FEN field
  that legitimately differs between them.

One call, not two: fetching the image and the history separately races
against live play (observed: the PNG showed the position after ``...e5``
while ``moveHistory`` already contained ``d4e5``).

Everything here is GUI-free, so it can run in any worker thread. The DOM
anchors below are fragile by nature — a Duolingo front-end refactor breaks
them, and they are meant to fail loudly rather than return a wrong position.
Verified live on 2026-09-19 against both match kinds.
"""
from __future__ import annotations

import base64
import binascii
import time
from dataclasses import dataclass, field
from datetime import datetime

import chess

from .config import DUO_IMAGE_TTL_S, DUO_TAB_URL
from .duolingo_canvas import (
    STATE_RECOVERED, STATE_UNREADABLE, STATE_UNRECONCILED, Reconciliation,
    read_grid, reconcile,
)
from .webbridge import WebBridgeError, eval_json, find_active_tab

# One atomic read of the board. The container is matched by prefix because a
# lesson challenge and a PvP match use different data-test values, and the
# largest canvas is the board either way (Duolingo also paints small 256x256
# avatar canvases inside the same container, dpr=2, against 1118 course /
# 862 PvP for the board).
_SNAP_JS = (
    "(() => {"
    " const box=document.querySelector('[data-test^=\"challenge challenge-chess\"]');"
    " if (!box) return JSON.stringify({error:'no-chess-match'});"
    " const el=[...box.querySelectorAll('canvas')]"
    ".sort((a,b)=>b.width*b.height-a.width*a.height)[0];"
    " if (!el) return JSON.stringify({error:'no-board-canvas'});"
    " const fk=Object.keys(el).find(k=>k.startsWith('__reactFiber$'));"
    " let f=fk?el[fk]:null, cs=null, st=null, nav=null, n=0;"
    " while (f && n<400 && !(cs && st)) {"
    "   const p=f.memoizedProps||{};"
    "   if (!cs && p.challengeState) cs=p.challengeState;"
    "   for (const k of ['inBoard','aboveBoard','belowBoard']) {"
    "     const e=p[k];"
    "     const s=(e && e.props && e.props.state) || (e && e.state);"
    "     const boardish=e && e.props && (e.props.kind||s&&s.boardFen);"
    "     if (!st && s && (s.boardFen||boardish)) st=s;"
    "   }"
    "   if (!nav && p.navigationState) nav=p.navigationState;"
    "   f=f.return; n++;"
    " }"
    " const g=(cs && cs.guess) || {};"
    " const history=(st && Array.isArray(st.moveHistory))"
    "   ? st.moveHistory : g.moveHistory;"
    " let dataUrl=null;"
    " try { dataUrl=el.toDataURL('image/png'); }"
    " catch (e) { return JSON.stringify({error:'canvas-tainted',"
    "   detail:String(e)}); }"
    " return JSON.stringify({"
    "   dataUrl:dataUrl, width:el.width, height:el.height,"
    "   kind:(cs && cs.type) || null,"
    "   status:(g.matchState||{}).status, moveHistory:history,"
    "   boardFen:(st && st.boardFen) || null,"
    "   viewingHistory:!!(nav && nav.isViewingHistory)"
    " });"
    "})()"
)

# What each JS-side error code means to the user.
_JS_ERRORS = {
    "no-chess-match": (
        "No chess match found on this page. Open the duolingo chess match "
        "(the course path has no chess board) or a PvP chess match in the "
        "foreground tab."
    ),
    "no-board-canvas": (
        "Duolingo's chess match has no board canvas yet — the board is "
        "probably still loading. Try again in a moment."
    ),
    "canvas-tainted": (
        "Duolingo's board canvas cannot be exported: it now contains "
        "cross-origin pixels, so toDataURL() is refused. "
        "(This is a preview-image problem, not a position problem.)"
    ),
}


class DuolingoFetchError(WebBridgeError):
    """Raised when the Duolingo board snapshot cannot be obtained."""


# The board is painted over a few hundred milliseconds, and a fetch that lands
# inside that window — or while a piece is being dragged — cannot be lined up
# with the move history. Re-read a couple of times before falling back.
_SETTLE_ATTEMPTS = 3
_SETTLE_WAIT_S = 0.25


@dataclass(frozen=True)
class BoardImage:
    """A captured picture of the *live* Duolingo board, bound to its ply.

    Duolingo paints only the current position, so there is exactly one image
    per fetch and none for historical plies. ``ply`` identifies which snapshot
    the picture belongs to: an image from a different ply is stale even if it
    is a few seconds old, which is why a newer fetch always replaces it
    outright and the TTL only acts as a backstop.

    ``recovered`` names the ply that was read back off the canvas itself when
    Duolingo's own history had not committed it yet (the opponent's reply —
    see :mod:`chessbuddy.duolingo_canvas`); ``moves`` already contains it.
    """

    png: bytes
    width: int
    height: int
    moves: tuple[str, ...] = ()
    captured_at: datetime = field(default_factory=datetime.now)
    status: str = ""
    recovered: str = ""

    @property
    def ply(self) -> int:
        """Plies played in the captured position (0 = start position)."""
        return len(self.moves)

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or datetime.now()) - self.captured_at).total_seconds()

    def is_stale(self, ttl_s: float = DUO_IMAGE_TTL_S,
                 now: datetime | None = None) -> bool:
        return self.age_s(now) > ttl_s

    def caption(self) -> str:
        """Human label — never claims to show a historical ply."""
        note = f" · reply {self.recovered} read off the board" if self.recovered else ""
        return (f"live position · ply {self.ply}{note} · "
                f"captured {self.captured_at:%H:%M:%S}")


def derive_fen(moves: list[str]) -> str:
    """Replay UCI moves from the standard start position and return the FEN.

    Raises :class:`DuolingoFetchError` naming the ply that failed, so a broken
    history is never silently shortened into a plausible-looking position.
    """
    board = chess.Board()
    for i, uci in enumerate(moves):
        if not isinstance(uci, str):
            raise DuolingoFetchError(
                f"Duolingo move history is malformed at ply {i + 1}: {uci!r}"
            )
        try:
            board.push_uci(uci)
        except ValueError as exc:
            raise DuolingoFetchError(
                f"Duolingo move history failed to replay at ply {i + 1} "
                f"({uci}): {exc}"
            ) from exc
    return board.fen()


def _decode_png(data_url: object) -> bytes:
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        raise DuolingoFetchError(
            "Duolingo returned no PNG data URL for the board canvas."
        )
    try:
        png = base64.b64decode(data_url.split(",", 1)[1])
    except (binascii.Error, ValueError) as exc:
        raise DuolingoFetchError(f"board PNG could not be decoded: {exc}") from exc
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise DuolingoFetchError("board canvas bytes are not a PNG.")
    return png


def _position_key(fen: object) -> str | None:
    """The position itself, re-emitted by python-chess; ``None`` if unusable.

    Both readings are normalised before they are compared, because the FEN
    en-passant field is written two different ways in practice:

    * :func:`derive_fen` replays the history, so the field comes from
      ``Board.fen()``, which only publishes an en-passant square while a
      capture is actually available (python-chess' default
      ``en_passant="legal"``);
    * Duolingo's ``boardFen`` is copied straight out of its own game model and
      keeps the square after *any* double pawn push — the letter of the FEN
      spec.

    So a page ``... b - f3`` and a replayed ``... b - -`` describe one and the
    same position (seen live on 2026-09-19: 1.f4 where no black pawn stood on
    e4/g4 to answer it). Re-emitting the page's FEN through python-chess
    compares the two under a single convention. The half-move / full-move
    counters are still dropped — they are not worth failing over — and a FEN
    python-chess refuses is not compared at all rather than guessed at.
    """
    parts = str(fen).split()
    if len(parts) < 4:
        return None
    try:
        return chess.Board(" ".join(parts[:4]) + " 0 1").fen()
    except ValueError:
        return None


def _check_board_fen(board_fen: str, derived: str) -> None:
    """Cross-check the replayed history against Duolingo's own ``boardFen``.

    PvP matches publish the authoritative FEN *and* a UCI move list. Two
    independent readings of one position that disagree mean an anchor has
    drifted, so this fails loudly rather than letting one silently win. See
    :func:`_position_key` for what "the same position" means here.
    """
    page, replayed = _position_key(board_fen), _position_key(derived)
    if page is not None and replayed is not None and page != replayed:
        # Report the raw readings, not the normalised ones: what each side
        # literally said is what a human has to see here.
        said = "boardFen %r, history %r" % (
            " ".join(str(board_fen).split()[:4]),
            " ".join(str(derived).split()[:4]),
        )
        raise DuolingoFetchError(
            f"Duolingo's boardFen and its replayed move history disagree "
            f"({said}, en-passant field normalised) — the anchors in "
            "duolingo_pipeline.py need re-checking."
        )


def _read_once(timeout: float) -> dict:
    """One evaluate, validated: the board canvas, the history, the page's FEN.

    Raises :class:`DuolingoFetchError` when the page cannot supply a reading at
    all (no tab, no board, no move history, a tainted canvas).
    """
    try:
        find_active_tab(DUO_TAB_URL, timeout=timeout)
        payload = eval_json(_SNAP_JS, timeout=timeout)
    except WebBridgeError as exc:
        raise DuolingoFetchError(str(exc)) from exc

    if "error" in payload:
        code = str(payload["error"])
        detail = payload.get("detail")
        message = _JS_ERRORS.get(code, f"Duolingo board not found in tab: {code}")
        if detail:
            message = f"{message} ({detail})"
        raise DuolingoFetchError(message)

    # Duolingo's own move list has a back-stepping UI; while it is showing an
    # earlier ply the board reflects that ply, not the live game. Refuse rather
    # than store a stale position under a live label.
    if payload.get("viewingHistory"):
        raise DuolingoFetchError(
            "Duolingo is showing a historical position (you scrubbed back in "
            "its move list). Return to the live position and fetch again."
        )

    # The history is React-internal: for a lesson it is reached by walking
    # __reactFiber$ up to memoizedProps.challengeState, for PvP it comes off the
    # board component's own props (see the module docstring). Either way it is
    # required — an empty one for a PvP match would look like ply 0.
    moves = payload.get("moveHistory")
    if not isinstance(moves, list):
        raise DuolingoFetchError(
            "Duolingo's React internals changed: no moveHistory under "
            "challengeState.guess or the board component's state. The anchors "
            f"in duolingo_pipeline.py need updating (got {type(moves).__name__})."
        )

    png = _decode_png(payload.get("dataUrl"))
    board_fen = payload.get("boardFen")
    kind = str(payload.get("kind") or "")

    # A PvP match always publishes boardFen; without it the props walk came up
    # empty and the empty challengeState history would masquerade as ply 0.
    if kind == "chessPvpMatch" and not (
        isinstance(board_fen, str) and board_fen.strip()
    ):
        raise DuolingoFetchError(
            "PvP match found, but its board props (boardFen) are gone — the "
            "anchors in duolingo_pipeline.py need updating."
        )

    fen = derive_fen(moves)
    if isinstance(board_fen, str) and board_fen.strip():
        _check_board_fen(board_fen, fen)

    return {
        "png": png,
        "width": int(payload.get("width") or 0),
        "height": int(payload.get("height") or 0),
        "moves": list(moves),
        "fen": fen,
        "ply": len(moves),
        "status": str(payload.get("status") or ""),
        "kind": kind,
        "board_fen": str(board_fen or ""),
        "recovered": "",
        "canvas_state": "",
    }


def _reconcile_canvas(snapshot: dict) -> Reconciliation:
    """Line the board canvas up with the move history, in place.

    When the canvas turns out to show exactly one legal ply more than the
    history — the opponent's reply Duolingo has not committed yet — that ply is
    appended to the snapshot (``moves``/``ply``/``fen``/``recovered``) so the
    position is not a move behind the board the user is looking at.

    A PvP match is left alone: it already publishes its own ``boardFen``, which
    passed :func:`_check_board_fen` above, so a canvas that disagrees with it is
    reported rather than allowed to add a ply to a reading that was already
    cross-checked.
    """
    verdict = reconcile(snapshot["moves"], read_grid(snapshot["png"]))
    if verdict.state == STATE_RECOVERED:
        if snapshot["board_fen"]:
            return Reconciliation(
                STATE_UNRECONCILED, orientation=verdict.orientation,
                detail=(f"the canvas shows {verdict.uci} beyond the history, but "
                        "the PvP boardFen already matched the history"),
            )
        snapshot["moves"].append(verdict.uci)
        snapshot["ply"] = len(snapshot["moves"])
        snapshot["fen"] = derive_fen(snapshot["moves"])
        snapshot["recovered"] = verdict.uci
    return verdict


def fetch_duolingo_snapshot(timeout: float = 8.0) -> dict:
    """Borrow the active Duolingo tab; return one atomic snapshot.

    Keys:
        png       bytes  — PNG of the live board canvas (1118 course / 862 PvP)
        width     int
        height    int
        moves     list[str] — UCI, whole game from the standard start position
        fen       str       — derived via python-chess; source of truth
        ply       int       — len(moves); 0 means the start position
        status    str       — challengeState.guess.matchState.status
        kind      str       — "chessMatch" (lesson) or "chessPvpMatch" (PvP)
        board_fen str       — Duolingo's own FEN, "" when the page has none
        recovered str       — reply read back off the canvas, "" when none was
        canvas_state str    — how the canvas lined up; see duolingo_canvas. The
                              GUI only has to warn about STATE_UNRECONCILED.

    The read is retried briefly when the canvas cannot be reconciled: the board
    is painted over a few hundred milliseconds, and a fetch that lands inside
    that window (or while a piece is being dragged) would otherwise come back a
    move behind. Raising instead is the last resort.

    Raises :class:`DuolingoFetchError` on any failure.
    """
    snapshot: dict = {}
    verdict = Reconciliation(STATE_UNREADABLE)
    for attempt in range(_SETTLE_ATTEMPTS):
        snapshot = _read_once(timeout)
        verdict = _reconcile_canvas(snapshot)
        if verdict.settled or attempt == _SETTLE_ATTEMPTS - 1:
            break
        time.sleep(_SETTLE_WAIT_S)
    snapshot["canvas_state"] = verdict.state
    return snapshot


def image_from_snapshot(data: dict) -> BoardImage:
    """Build a :class:`BoardImage` from a :func:`fetch_duolingo_snapshot` dict."""
    return BoardImage(
        png=data["png"],
        width=int(data.get("width") or 0),
        height=int(data.get("height") or 0),
        moves=tuple(data.get("moves") or ()),
        status=str(data.get("status") or ""),
        recovered=str(data.get("recovered") or ""),
    )


__all__ = [
    "BoardImage", "DuolingoFetchError", "derive_fen",
    "fetch_duolingo_snapshot", "image_from_snapshot",
]
