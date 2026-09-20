"""One engine, one job at a time — shared by the analysis panel and the
what-if graph.

``StockfishClient`` is deliberately single-consumer: it owns one subprocess
and one output queue, so two overlapping ``analyze`` calls would interleave
their ``info`` lines. Both views therefore go through this service, which
keeps the "one thread, one worker" shape the analysis panel already had and
adds nothing else — no queueing, no policy about *which* request wins. The
caller decides that (the graph remembers its last click; the panel simply
refuses while busy).
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .engine import EngineError, StockfishClient, pick_stockfish


class _JobWorker(QObject):
    """Runs one ``fn(stop_event, info_signal)`` off the GUI thread."""

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


class EngineService(QObject):
    """Owns the Stockfish subprocess and the single job slot.

    Signals
    -------
    busyChanged(bool)
        A job is in flight. Emitted before ``done``/``failed`` handlers run
        (True) and after they return (False).
    idle()
        The job slot is free *and* the worker thread is fully torn down. This
        is the safe point to submit the next job — unlike ``busyChanged``,
        which still fires while the old ``QThread`` is being reaped.
    """

    busyChanged = pyqtSignal(bool)
    idle = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._client: StockfishClient | None = None
        self._worker: _JobWorker | None = None
        self._thread: QThread | None = None
        self._stop_event: threading.Event | None = None
        self._busy = False
        # Callbacks of the job in flight, called from the GUI thread.
        self._on_info = None
        self._on_done = None
        self._on_failed = None

    # ------------------------------------------------------------- state
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def client(self) -> StockfishClient | None:
        return self._client

    @property
    def thread(self) -> QThread | None:
        return self._thread

    def engine_name(self) -> str:
        return self._client.engine_name if self._client is not None else "Stockfish"

    def ensure_client(self) -> StockfishClient:
        """Start the engine on first use. Raises ``EngineError`` if none works."""
        if self._client is None:
            path, _name = pick_stockfish()
            client = StockfishClient(path)
            client.handshake()
            self._client = client
        return self._client

    # --------------------------------------------------------------- jobs
    def submit(self, fn, on_info=None, on_done=None, on_failed=None) -> None:
        """Run ``fn(stop_event, info_signal)`` on a worker thread.

        ``on_info`` receives every engine info dict; exactly one of
        ``on_done`` / ``on_failed`` follows. Callers must not submit while a
        job is in flight — cancel it first and wait for :attr:`idle`.

        The callbacks are invoked from the GUI thread through this object's
        own bound slots rather than connected directly to the worker. PyQt
        cannot tell which thread a *plain* callable belongs to and would run
        it in the worker, so a lambda here would silently touch widgets off
        the GUI thread; marshalling through a bound method of a QObject that
        lives in the main thread is what makes any callable safe.
        """
        self._on_info, self._on_done, self._on_failed = on_info, on_done, on_failed
        self._stop_event = threading.Event()
        self._thread = QThread(self)
        self._worker = _JobWorker(fn, self._stop_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.info.connect(self._handle_info)
        self._worker.done.connect(self._handle_done)
        self._worker.failed.connect(self._handle_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_cleared)
        self._thread.start()
        self._set_busy(True)

    def cancel(self) -> None:
        """Ask the running job to stop (it still reports its partial result)."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._client is not None:
            self._client.stop()

    def shutdown(self) -> None:
        """Cancel the running job, join the thread and quit the engine."""
        self.cancel()
        client, self._client = self._client, None
        if self._thread is not None:
            self._thread.quit()
        # Quit the engine *before* waiting: a worker parked on a blocking read
        # is only unblocked by the process dying (its reader thread turns that
        # into an EOF sentinel), and quitting it afterwards would leave the
        # thread running while Qt tears the window down.
        if client is not None:
            client.quit()
        if self._thread is not None:
            self._thread.wait(3000)

    # ----------------------------------------------------------- plumbing
    def _handle_info(self, info: dict) -> None:
        if self._on_info is not None:
            self._on_info(info)

    def _handle_done(self, result) -> None:
        callback, self._on_info = self._on_done, None
        self._on_done = self._on_failed = None
        if callback is not None:
            callback(result)
        self._set_busy(False)

    def _handle_failed(self, message: str) -> None:
        callback, self._on_info = self._on_failed, None
        self._on_done = self._on_failed = None
        if callback is not None:
            callback(message)
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        if self._busy != busy:
            self._busy = busy
            self.busyChanged.emit(busy)

    def _on_cleared(self) -> None:
        """The worker thread has really finished — the slot is safe to reuse."""
        self._worker = None
        self._thread = None
        self._stop_event = None
        self.idle.emit()


__all__ = ["EngineError", "EngineService"]
