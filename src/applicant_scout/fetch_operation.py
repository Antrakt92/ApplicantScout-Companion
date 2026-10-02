"""Worker lifetime independent of the character and metric scope it requests."""

from threading import Event


class FetchOperation:
    """A unique operation with a thread-safe, irreversible retirement latch."""

    def __init__(self) -> None:
        self._retired = Event()

    def retire(self) -> None:
        self._retired.set()

    def is_active(self) -> bool:
        return not self._retired.is_set()
