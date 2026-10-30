"""Context-local switches. ContextVars (not module globals or thread-locals) are safe under threads and async."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_suppressed: ContextVar[bool] = ContextVar("beckn_suppressed", default=False)
_reschedules: ContextVar[dict[Any, Any] | None] = ContextVar("beckn_reschedules", default=None)


@contextmanager
def suppress_beckn_signals() -> Iterator[None]:
    """Wrap every plugin-initiated write so our own saves never re-trigger Beckn signal receivers."""
    token = _suppressed.set(True)
    try:
        yield
    finally:
        _suppressed.reset(token)


def beckn_signals_suppressed() -> bool:
    return _suppressed.get()


def remember_reschedule(patient_id: Any, old_booking_id: Any) -> None:
    pending = _reschedules.get()
    if pending is None:
        pending = {}
        _reschedules.set(pending)
    pending[patient_id] = old_booking_id


def pop_reschedule(patient_id: Any) -> Any | None:
    pending = _reschedules.get()
    return pending.pop(patient_id, None) if pending else None


def reset_reschedules(*args: Any, **kwargs: Any) -> None:
    _reschedules.set(None)
