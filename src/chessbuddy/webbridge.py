"""Shared client for the Kimi WebBridge daemon (http://127.0.0.1:10086).

Every position source borrows the user's own tab and then runs one
``evaluate`` on it:

    chess.com  -> read the live game model on <wc-chess-board>
    duolingo   -> read the board canvas + the React ``moveHistory``

This module is pure stdlib (urllib) and GUI-free, so it can run in any
worker thread. The daemon rejects inline shell-quoted request bodies, so
each request is posted as a prepared JSON body instead.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .config import WEBBRIDGE_SESSION, WEBBRIDGE_URL


class WebBridgeError(Exception):
    """The daemon is unreachable, failed the command, or returned junk."""


# The daemon is a LOCALHOST service — never route it through a proxy.
# Corporate environments export HTTP_PROXY/NO_PROXY without 127.0.0.1,
# which makes urllib send localhost traffic to the proxy, where it fails
# (503/407). An empty ProxyHandler disables proxying for this client.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(payload: dict, timeout: float = 8.0, attempts: int = 3) -> dict:
    """POST one command to the daemon and return its ``data`` payload.

    Raises :class:`WebBridgeError` on any transport failure, non-2xx status
    (after retrying a transient 503), non-JSON body, or ``ok:false`` reply.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBBRIDGE_URL, data=body, headers={"Content-Type": "application/json"}
    )
    last_exc: WebBridgeError | None = None
    for attempt in range(attempts):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()
            if exc.code == 503 and attempt < attempts - 1:
                # 503 = daemon alive but momentarily unavailable (e.g. the
                # browser extension is reconnecting). Retry briefly.
                time.sleep(0.5 * (attempt + 1))
                last_exc = WebBridgeError(
                    f"WebBridge returned HTTP 503: {detail[:200]}"
                )
                continue
            raise WebBridgeError(
                f"WebBridge returned HTTP {exc.code}: {detail[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise WebBridgeError(
                f"WebBridge daemon not reachable at {WEBBRIDGE_URL}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise WebBridgeError("WebBridge request timed out") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WebBridgeError(
                f"WebBridge returned non-JSON response: {raw[:200]!r}"
            ) from exc

        # The daemon wraps every response in {"ok": …, "data": {…}}; the
        # action-specific payload lives under "data". Accept the flat form too
        # for robustness.
        if isinstance(parsed, dict) and "ok" in parsed:
            if parsed.get("ok") is False:
                data = parsed.get("data") or {}
                raise WebBridgeError(
                    f"WebBridge command failed: {data.get('error') or parsed}"
                )
            return parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        return parsed

    # All retries exhausted on 503.
    raise last_exc if last_exc is not None else WebBridgeError("WebBridge request failed")


def find_active_tab(url: str, timeout: float = 8.0) -> None:
    """Borrow the tab the user is actually looking at for ``url``.

    ``active: True`` means "the tab the user is viewing right now", so the
    match has to be in the foreground; with several matching tabs open the
    choice is ambiguous. Raises :class:`WebBridgeError` when no tab matches.
    """
    tab = post(
        {"action": "find_tab", "args": {"url": url, "active": True},
         "session": WEBBRIDGE_SESSION},
        timeout=timeout,
    )
    if not isinstance(tab, dict) or not tab.get("success"):
        err = tab.get("error") if isinstance(tab, dict) else tab
        raise WebBridgeError(f"no active {url} tab: {err}")


def eval_json(code: str, timeout: float = 8.0) -> dict:
    """Run ``code`` in the borrowed tab; parse its return value as JSON.

    The snippet is expected to hand back a JSON *string* (see the pipelines),
    which is decoded here. A returned ``{"error": …}`` object is NOT treated
    as a failure — callers decide what an app-level error means, since the
    message differs per site.
    """
    result = post(
        {"action": "evaluate", "args": {"code": code}, "session": WEBBRIDGE_SESSION},
        timeout=timeout,
    )
    if not isinstance(result, dict) or "value" not in result:
        err = result.get("error") if isinstance(result, dict) else result
        raise WebBridgeError(f"evaluate failed: {err}")
    payload = result.get("result", result.get("value"))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WebBridgeError(
                f"evaluate returned non-JSON: {payload[:200]!r}"
            ) from exc
    if not isinstance(payload, dict):
        raise WebBridgeError(f"evaluate returned {payload!r}, expected an object")
    return payload
