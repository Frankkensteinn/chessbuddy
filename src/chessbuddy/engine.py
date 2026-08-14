"""Local Stockfish UCI client.

Speaks the UCI protocol to a stockfish.exe subprocess over stdin/stdout:
handshake, set position, ``go`` (movetime or depth, MultiPV), incremental
``info`` callbacks, ``stop``/``quit``. Thread-safe for one analysis at a time
(single consumer pattern: one reader thread -> one queue).
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path

from .config import stockfish_candidates


class EngineError(Exception):
    """Raised for UCI-level failures (process died, handshake timeout, ...)."""


def parse_info(line: str) -> dict:
    """Parse one ``info ...`` line into a dict (unknown tokens skipped)."""
    toks = line.split()
    info = {
        "depth": None, "seldepth": None, "multipv": 1,
        "score_cp": None, "score_mate": None,
        "nodes": None, "nps": None, "hashfull": None, "time": None,
        "pv": [],
    }
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "depth" and i + 1 < len(toks):
            info["depth"] = int(toks[i + 1]); i += 2
        elif t == "seldepth" and i + 1 < len(toks):
            info["seldepth"] = int(toks[i + 1]); i += 2
        elif t == "multipv" and i + 1 < len(toks):
            info["multipv"] = int(toks[i + 1]); i += 2
        elif t == "score" and i + 3 < len(toks):
            kind, val = toks[i + 1], int(toks[i + 2])
            if kind == "cp":
                info["score_cp"] = val
            elif kind == "mate":
                info["score_mate"] = val
            i += 3
        elif t == "nodes" and i + 1 < len(toks):
            info["nodes"] = int(toks[i + 1]); i += 2
        elif t == "nps" and i + 1 < len(toks):
            info["nps"] = int(toks[i + 1]); i += 2
        elif t == "hashfull" and i + 1 < len(toks):
            info["hashfull"] = int(toks[i + 1]); i += 2
        elif t == "time" and i + 1 < len(toks):
            info["time"] = int(toks[i + 1]); i += 2
        elif t == "pv":
            info["pv"] = toks[i + 1:]
            break
        else:
            i += 1
    return info


def score_text(info: dict) -> str:
    """'+1.35' / '-0.42' / 'M3' / 'M-2' — relative to the side to move."""
    if info.get("score_mate") is not None:
        return f"M{info['score_mate']:+d}".replace("+-", "-")
    cp = info.get("score_cp")
    if cp is None:
        return "?"
    return f"{cp / 100.0:+.2f}"


class StockfishClient:
    """One subprocess; call ``handshake`` once, then ``analyze`` as needed."""

    def __init__(self, binary: str | Path, threads: int | None = None,
                 hash_mb: int = 128):
        self.binary = str(binary)
        self.threads = threads or min(8, os.cpu_count() or 4)
        self.hash_mb = hash_mb
        self.engine_name = "Stockfish"

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [self.binary],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=flags,
        )
        self._q: queue.Queue = queue.Queue()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------- plumbing
    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                self._q.put(line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            self._q.put(None)          # EOF sentinel

    def _send(self, cmd: str) -> None:
        with self._send_lock:
            if self.proc.poll() is not None:
                raise EngineError(f"engine process exited (code {self.proc.returncode})")
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()

    def _read_line(self, timeout: float | None = None) -> str | None:
        """Block for the next engine line (None = EOF). A finite ``timeout``
        raises EngineError if nothing arrives in time."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            raise EngineError("timed out waiting for engine output")

    def _read_until(self, needle: str, timeout: float) -> list[str]:
        lines = []
        while True:
            line = self._read_line(timeout)
            if line is None:
                raise EngineError("engine closed its output unexpectedly")
            lines.append(line)
            if needle in line:
                return lines

    # ------------------------------------------------------------- UCI api
    def handshake(self, timeout: float = 15.0) -> str:
        self._send("uci")
        lines = self._read_until("uciok", timeout)
        for line in lines:
            if line.startswith("id name"):
                self.engine_name = line[7:].strip()
        self._send("isready")
        self._read_until("readyok", timeout)
        return self.engine_name

    def _configure(self) -> None:
        self._send(f"setoption name Threads value {self.threads}")
        self._send(f"setoption name Hash value {self.hash_mb}")

    def analyze(self, fen: str, movetime_ms: int | None = None,
                depth: int | None = None, multipv: int = 1,
                on_info=None, stop: threading.Event | None = None) -> dict:
        """Run a search; call ``on_info(dict)`` for every PV update.

        Reads engine output with no artificial timeouts: deep iterations can
        legitimately take seconds. To cancel, call :meth:`stop` from another
        thread (or set ``stop``; the loop will also notice it per-line).
        Returns {"bestmove": uci|None, "lines": {multipv: info}}.
        """
        self._configure()
        self._send(f"setoption name MultiPV value {int(multipv)}")
        self._send(f"position fen {fen}")
        if movetime_ms and movetime_ms > 0:
            self._send(f"go movetime {int(movetime_ms)}")
        elif depth and depth > 0:
            self._send(f"go depth {int(depth)}")
        else:
            self._send("go movetime 3000")

        lines: dict[int, dict] = {}
        best = None
        while True:
            line = self._read_line()          # blocks; engine always answers
            if line is None:
                break                         # engine closed output
            if stop is not None and stop.is_set():
                self._send("stop")
            if line.startswith("info"):
                info = parse_info(line)
                if info["pv"]:
                    lines[info["multipv"]] = info
                    if on_info:
                        on_info(info)
            elif line.startswith("bestmove"):
                parts = line.split()
                best = parts[1] if len(parts) > 1 and parts[1] != "(none)" else None
                break
        return {"bestmove": best, "lines": lines}

    def stop(self) -> None:
        try:
            self._send("stop")
        except EngineError:
            pass

    def quit(self) -> None:
        try:
            self._send("quit")
        except EngineError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def probe_engine(path: str | Path, timeout: float = 6.0) -> str | None:
    """Return the engine name if ``path`` handshakes, else None."""
    try:
        client = StockfishClient(path)
        name = client.handshake(timeout=timeout)
        client.quit()
        return name
    except Exception:
        try:
            client.quit()
        except Exception:
            pass
        return None


def pick_stockfish() -> tuple[str, str]:
    """Return (binary_path, engine_name) of the first working binary.

    Tries AVX2 before baseline before anything else found locally.
    Raises EngineError if nothing works (check Stockfish/prebuilt).
    """
    candidates = stockfish_candidates()
    if not candidates:
        raise EngineError(
            "No Stockfish binary found. Expected one under Stockfish/prebuilt/ "
            "(run: uv run python scripts/install_stockfish.py) or set $STOCKFISH_BIN."
        )
    for path in candidates:
        name = probe_engine(path)
        if name:
            return str(path), name
    raise EngineError(
        "Found Stockfish binary(ies) but none responded to a UCI handshake: "
        + ", ".join(str(p) for p in candidates)
    )
