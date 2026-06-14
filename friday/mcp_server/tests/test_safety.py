import time
import pytest
from friday.mcp_server import safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


def test_stage_returns_confirm_required_with_id():
    msg = safety.stage("delete foo.txt", lambda: "did it")
    assert msg.startswith("CONFIRM_REQUIRED [id=")
    assert "delete foo.txt" in msg


def test_confirm_with_correct_id_runs_fn():
    calls = []
    safety.stage("do thing", lambda: calls.append(1) or "ran")
    action_id = safety._pending.id
    result = safety.confirm_action(action_id)
    assert result == "ran"
    assert calls == [1]
    assert safety._pending is None  # slot cleared after run


def test_confirm_with_wrong_id_rejected_and_slot_kept():
    safety.stage("do thing", lambda: "ran")
    result = safety.confirm_action("zzzzzz")
    assert "mismatch" in result.lower()
    assert safety._pending is not None  # still armed


def test_confirm_with_no_pending_is_clean():
    result = safety.confirm_action("anything")
    assert "no action" in result.lower()


def test_cancel_clears_slot():
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    assert "cancel" in safety.cancel_action().lower()
    assert safety._pending is None
    assert "no action" in safety.confirm_action(action_id).lower()


def test_second_stage_replaces_first():
    safety.stage("first", lambda: "first-ran")
    first_id = safety._pending.id
    safety.stage("second", lambda: "second-ran")
    assert safety._pending.id != first_id
    assert "mismatch" in safety.confirm_action(first_id).lower()


def test_unrelated_call_leaves_slot_armed():
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    # Simulate an unrelated command happening (no gate interaction).
    assert safety._pending is not None
    assert safety.confirm_action(action_id) == "ran"


def test_expiry_boundary():
    # 89s elapsed -> accepted
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 89
    assert safety.confirm_action(action_id) == "ran"

    # 90s elapsed -> rejected
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 90
    assert "expired" in safety.confirm_action(action_id).lower()

    # 91s elapsed -> rejected
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 91
    assert "expired" in safety.confirm_action(action_id).lower()
