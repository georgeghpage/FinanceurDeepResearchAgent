"""Cooperative cancellation with interruptible waits for provider I/O."""

import threading
from contextvars import copy_context
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class ResearchCancelled(Exception):
    """The user explicitly terminated this run."""


class Cancellation:
    def __init__(self) -> None:
        self.event = threading.Event()

    def cancel(self) -> None:
        self.event.set()

    def check(self) -> None:
        if self.event.is_set():
            raise ResearchCancelled("Research stopped by the user.")

    def call(self, operation: Callable[[], T]) -> T:
        """Release the workflow promptly even when a socket is waiting for data.

        Transports observe the same signal and close their resources. A late
        response cannot enter the workflow or start another request.
        """
        self.check()
        completed = threading.Event()
        result: list[T] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                self.check()
                result.append(operation())
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        context = copy_context()
        threading.Thread(target=lambda: context.run(work), daemon=True).start()
        while not completed.wait(0.025):
            self.check()
        self.check()
        if errors:
            raise errors[0]
        return result[0]


class CancellableBackend:
    """Keep blocking provider calls outside the graph's cancellation wait."""

    def __init__(self, backend, cancellation: Cancellation) -> None:
        self.backend = backend
        self.cancellation = cancellation
        self._local = threading.local()

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def structured(self, *args, **kwargs):
        captured = {}

        def operation():
            try:
                return self.backend.structured(*args, **kwargs)
            finally:
                reader = getattr(self.backend, "usage_for_thread", None)
                if reader:
                    captured.update(reader())

        try:
            return self.cancellation.call(operation)
        finally:
            # Usage belongs to the provider's worker thread, then follows its result
            # back to the reservation-owning caller thread.
            self._local.usage = captured or {"usage_observed": False}

    def usage_for_thread(self):
        return getattr(self._local, "usage", {"usage_observed": False})
