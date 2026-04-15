"""Unit tests for daemon task action response routing."""

from aircd.client import Message
from aircd.daemon import SyncRequest, _task_action_result_from_message


def _message(content: str, *, tags: dict[str, str] | None = None) -> Message:
    return Message(
        seq=None,
        channel="#work",
        sender="aircd",
        content=content,
        raw="",
        tags=tags or {},
    )


def test_structured_task_success_requires_requested_action_and_self_nick():
    req = SyncRequest(task_id="task_1", action="claim")

    other_agent = _message(
        "TASK task_1 claimed by agent-b: title",
        tags={
            "type": "task",
            "task-id": "task_1",
            "task-action": "claim",
            "task-result": "success",
            "task-actor": "agent-b",
        },
    )
    assert _task_action_result_from_message(other_agent, req, "agent-a") is None

    wrong_action = _message(
        "TASK task_1 completed by agent-a: title",
        tags={
            "type": "task",
            "task-id": "task_1",
            "task-action": "done",
            "task-result": "success",
            "task-actor": "agent-a",
        },
    )
    assert _task_action_result_from_message(wrong_action, req, "agent-a") is None

    own_success = _message(
        "TASK task_1 claimed by agent-a: title",
        tags={
            "type": "task",
            "task-id": "task_1",
            "task-action": "claim",
            "task-result": "success",
            "task-actor": "agent-a",
        },
    )
    assert _task_action_result_from_message(own_success, req, "agent-a") == {
        "status": "claimed",
        "detail": "TASK task_1 claimed by agent-a: title",
    }


def test_structured_task_failure_requires_requested_action():
    req = SyncRequest(task_id="task_1", action="done")

    wrong_action = _message(
        "TASK CLAIM task_1 failed: already claimed",
        tags={
            "type": "task",
            "task-id": "task_1",
            "task-action": "claim",
            "task-result": "error",
            "task-actor": "agent-a",
            "task-error": "already claimed",
        },
    )
    assert _task_action_result_from_message(wrong_action, req, "agent-a") is None

    failure = _message(
        "TASK DONE task_1 failed: not owner",
        tags={
            "type": "task",
            "task-id": "task_1",
            "task-action": "done",
            "task-result": "error",
            "task-actor": "agent-a",
            "task-error": "not owner",
        },
    )
    assert _task_action_result_from_message(failure, req, "agent-a") == {
        "status": "failed",
        "error": "not owner",
        "detail": "TASK DONE task_1 failed: not owner",
    }


def test_legacy_task_regex_fallback_is_action_and_actor_scoped():
    claim_req = SyncRequest(task_id="task_1", action="claim")
    done_req = SyncRequest(task_id="task_1", action="done")

    assert (
        _task_action_result_from_message(
            _message("TASK task_1 claimed by agent-b: title"),
            claim_req,
            "agent-a",
        )
        is None
    )
    assert (
        _task_action_result_from_message(
            _message("TASK task_1 completed by agent-a: title"),
            claim_req,
            "agent-a",
        )
        is None
    )
    assert _task_action_result_from_message(
        _message("TASK task_1 completed by agent-a: title"),
        done_req,
        "agent-a",
    ) == {
        "status": "done",
        "detail": "TASK task_1 completed by agent-a: title",
    }
    assert _task_action_result_from_message(
        _message("TASK DONE task_1 failed: not owner"),
        done_req,
        "agent-a",
    ) == {
        "status": "failed",
        "error": "TASK DONE task_1 failed: not owner",
    }
