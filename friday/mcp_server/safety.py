"""Server-side confirmation gate for destructive tool actions.

A single pending action is staged by a risky tool and only runs when the LLM
calls confirm_action() with the matching id, within the expiry window. The gate
is independent of the LLM: nothing destructive runs without the explicit,
id-targeted second call.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Callable

EXPIRY_SECONDS = 90


@dataclass
class PendingAction:
    id: str
    summary: str
    fn: Callable[[], str]
    created_at: float


_pending: PendingAction | None = None


def stage(summary: str, fn: Callable[[], str]) -> str:
    """Store a single pending action, replacing any existing one. Returns the
    CONFIRM_REQUIRED message the LLM should read back to the user."""
    global _pending
    action_id = uuid.uuid4().hex[:6]
    _pending = PendingAction(id=action_id, summary=summary, fn=fn, created_at=time.monotonic())
    return f"CONFIRM_REQUIRED [id={action_id}]: {summary}"


def confirm_action(action_id: str) -> str:
    """Execute the staged action if action_id matches the pending slot and it
    has not expired. The LLM must pass the id from the CONFIRM_REQUIRED message."""
    global _pending
    if _pending is None:
        return "No action is pending."
    if action_id != _pending.id:
        return f"action_id mismatch; the pending action id is {_pending.id}."
    if time.monotonic() - _pending.created_at >= EXPIRY_SECONDS:
        _pending = None
        return "Pending action expired; please ask again."
    action = _pending
    _pending = None
    return action.fn()


def cancel_action() -> str:
    """Discard the pending action, if any."""
    global _pending
    if _pending is None:
        return "Nothing to cancel."
    _pending = None
    return "Cancelled."


def _reset() -> None:
    """Test helper: clear the pending slot."""
    global _pending
    _pending = None
