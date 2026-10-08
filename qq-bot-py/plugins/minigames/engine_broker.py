"""One serial lane and one resident external game engine for this process."""
import threading
from .ai import SearchCancelled


class EngineBroker:
    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._engine = None
        self._timer = None
        self._generation = 0

    def _discard(self):
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        engine = self._engine
        if engine is not None:
            engine.close()
        # Keep the old lease if process termination failed: a later request
        # must retry closing it, never launch a second resident engine.
        self._engine = None
        self._key = None

    def run(self, key, factory, operation, cancel, idle_seconds=60):
        while not self._lock.acquire(timeout=.05):
            if cancel.is_set():
                raise SearchCancelled()
        try:
            if cancel.is_set():
                raise SearchCancelled()
            if self._key != key:
                self._discard()  # Never overlap the outgoing and incoming child.
                self._engine, self._key = factory(), key
            self._generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            try:
                result = operation(self._engine, cancel)
                if cancel.is_set():
                    raise SearchCancelled()
            except BaseException:
                self._discard()
                raise
            generation = self._generation
            def expire():
                with self._lock:
                    if generation == self._generation:
                        self._discard()
            self._timer = threading.Timer(idle_seconds, expire)
            self._timer.daemon = True
            self._timer.start()
            return result
        finally:
            self._lock.release()

    def close_owner(self, owner):
        with self._lock:
            if self._key is not None and self._key[0] == owner:
                self._discard()


GLOBAL_BROKER = EngineBroker()
