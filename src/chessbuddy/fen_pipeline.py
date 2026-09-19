"""chess.com FEN pipeline.

Reads the live position of the user's open chess.com tab through the Kimi
WebBridge daemon (http://127.0.0.1:10086):

    find_tab (borrow the active chess.com tab)
      -> evaluate JS on the ``wc-chess-board`` game model
      -> return {fen, beforeFen, san, isAtEnd, playingAs}

The daemon plumbing itself lives in :mod:`chessbuddy.webbridge`; this module
only knows about chess.com's DOM. It is GUI-free, so it can run in any worker
thread.
"""
from __future__ import annotations

import chess

from .config import CHESSCOM_TAB_URL
from .webbridge import WebBridgeError, eval_json, find_active_tab

# chess.com refactored the board component and removed the public `state`
# property on <wc-chess-board> (it used to be a LitElement reactive prop that
# held `.selectedNode.fen`). The live game model is now exposed via `cb.game`,
# which offers getFEN() / getSelectedNode() / isAtEndOfLine() / getPlayingAs().
# The selected node carries fen/beforeFen/san. This was verified live against a
# chess.com game on 2026-08-25 after the old `state.selectedNode` path started
# returning `no wc-chess-board`.
_EVAL_JS = (
    "(() => { const cb=document.querySelector('wc-chess-board'); "
    "if (!cb || !cb.game) return JSON.stringify({error:'no wc-chess-board'}); "
    "const g=cb.game; "
    "try { const n=g.getSelectedNode(); "
    "const fen=(n&&n.fen)?n.fen:g.getFEN(); "
    "return JSON.stringify({"
    "fen:fen, "
    "beforeFen:n?(n.beforeFen||null):null, "
    "san:n?(n.san||null):null, "
    "isAtEnd:g.isAtEndOfLine(), "
    "playingAs:g.getPlayingAs()"
    "}); } catch(e){ return JSON.stringify({error:'eval-error:'+String(e)}); } "
    "})()"
)


class FenFetchError(WebBridgeError):
    """Raised when the live FEN cannot be obtained (daemon down, no tab, etc.)."""


def fetch_live_fen(timeout: float = 8.0) -> dict:
    """Borrow the active chess.com tab and return its live position.

    Returns a dict with keys: fen, beforeFen, san, isAtEnd, playingAs.
    Raises FenFetchError on any failure.
    """
    try:
        # 1. borrow the tab the user is currently viewing (no new tab).
        find_active_tab(CHESSCOM_TAB_URL, timeout=timeout)
        # 2. read the board model state.
        payload = eval_json(_EVAL_JS, timeout=timeout)
    except WebBridgeError as exc:
        raise FenFetchError(str(exc)) from exc

    if "error" in payload:
        raise FenFetchError(f"chess.com board not found in tab: {payload['error']}")

    fen = payload.get("fen")
    if not fen:
        raise FenFetchError("board state had no FEN (is a game open?)")

    # 3. sanity-check the FEN with python-chess.
    try:
        chess.Board(fen)
    except ValueError as exc:
        raise FenFetchError(f"chess.com returned an invalid FEN {fen!r}: {exc}") from exc

    return payload
