"""WebBridge FEN pipeline.

Reads the live position of the user's open chess.com tab through the Kimi
WebBridge daemon (http://127.0.0.1:10086). Follows the verified approach
documented in ``webbridge-fen-pipeline.md``:

    find_tab (borrow the active chess.com tab)
      -> evaluate JS on ``wc-chess-board`` game model
      -> return {fen, beforeFen, san, isAtEnd, playingAs}

This module is pure stdlib (urllib) and GUI-free, so it can run in any
worker thread. Requests are posted as JSON files/bodies via urllib because
the daemon rejects inline shell-quoted bodies (doc gotcha #7).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import chess

from .config import WEBBRIDGE_SESSION, WEBBRIDGE_URL

# Reads the board component's own game model (doc: Attempt 3, authoritative).
# The shadow root is closed, but `state.selectedNode.fen` is exact.
_EVAL_JS = (
    "(() => { const cb=document.querySelector('wc-chess-board'); "
    "if (!cb || !cb.state || !cb.state.selectedNode) return JSON.stringify({error:'no wc-chess-board'}); "
    "const st=cb.state; const n=st.selectedNode; "
    "return JSON.stringify({"
    "fen:n.fen, beforeFen:n.beforeFen, san:n.san, "
    "isAtEnd:st.isAtEndOfLine, "
    "playingAs:cb.game.getPlayingAs?cb.game.getPlayingAs():null"
    "}); })()"
)


class FenFetchError(Exception):
    """Raised when the live FEN cannot be obtained (daemon down, no tab, etc.)."""


def _post(payload: dict, timeout: float = 8.0, _attempts: int = 3) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBBRIDGE_URL, data=body, headers={"Content-Type": "application/json"}
    )
    # The daemon is a LOCALHOST service — never route it through a proxy.
    # Corporate environments export HTTP_PROXY/NO_PROXY without 127.0.0.1,
    # which makes urllib send localhost traffic to the proxy, where it fails
    # (503/407). An empty ProxyHandler disables proxying for this client.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_exc: FenFetchError | None = None
    for attempt in range(_attempts):
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            if exc.code == 503 and attempt < _attempts - 1:
                # 503 = daemon alive but momentarily unavailable (e.g. the
                # browser extension is reconnecting). Retry briefly.
                time.sleep(0.5 * (attempt + 1))
                last_exc = FenFetchError(
                    f"WebBridge returned HTTP 503: {detail[:200]}"
                )
                continue
            raise FenFetchError(
                f"WebBridge returned HTTP {exc.code}: {detail[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise FenFetchError(f"WebBridge daemon not reachable at {WEBBRIDGE_URL}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise FenFetchError("WebBridge request timed out") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FenFetchError(f"WebBridge returned non-JSON response: {raw[:200]!r}") from exc

        # The daemon wraps every response in {"ok": …, "data": {…}}; the
        # action-specific payload lives under "data". Accept the flat form too
        # for robustness.
        if isinstance(parsed, dict) and "ok" in parsed:
            if parsed.get("ok") is False:
                data = parsed.get("data") or {}
                raise FenFetchError(
                    f"WebBridge command failed: {data.get('error') or parsed}"
                )
            return parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        return parsed

    # All retries exhausted on 503.
    raise last_exc if last_exc is not None else FenFetchError("WebBridge request failed")


def fetch_live_fen(timeout: float = 8.0) -> dict:
    """Borrow the active chess.com tab and return its live position.

    Returns a dict with keys: fen, beforeFen, san, isAtEnd, playingAs.
    Raises FenFetchError on any failure.
    """
    # 1. borrow the tab the user is currently viewing (no new tab).
    tab = _post(
        {
            "action": "find_tab",
            "args": {"url": "https://www.chess.com", "active": True},
            "session": WEBBRIDGE_SESSION,
        },
        timeout=timeout,
    )
    if not tab.get("success"):
        raise FenFetchError(f"find_tab failed: {tab.get('error') or tab}")

    # 2. read the board model state. The daemon reports evaluate errors as
    #    ok:false (already raised in _post); a successful response has no
    #    explicit success flag, just {"type": "string", "value": "..."}.
    result = _post(
        {"action": "evaluate", "args": {"code": _EVAL_JS}, "session": WEBBRIDGE_SESSION},
        timeout=timeout,
    )
    if not isinstance(result, dict) or "value" not in result:
        raise FenFetchError(f"evaluate failed: {result.get('error') or result}")

    payload = result.get("result", result.get("value"))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FenFetchError(f"evaluate returned non-JSON: {payload[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise FenFetchError(f"unexpected evaluate result: {payload!r}")
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
