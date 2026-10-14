"""Controller-declared completion contracts fail closed before task mutation."""

from __future__ import annotations

import json
from pathlib import Path


def _controller_worker(monkeypatch, tmp_path, contract):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_controller as controller_tasks

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="quality loop stage",
            body="return the declared handoff",
            assignee="test-worker",
            handoff_contract=contract,
        )
        assert kb.claim_task(conn, task_id) is not None
        run_id = kb._current_run_id(conn, task_id)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return task_id


def _assert_running(task_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
    assert task.status == "running"
    assert run.outcome is None


def test_metadata_contract_rejects_missing_namespace_and_allows_retry(monkeypatch, tmp_path):
    task_id = _controller_worker(
        monkeypatch,
        tmp_path,
        {"kind": "metadata_namespace", "namespace": "quality_loop"},
    )
    from tools import kanban_tools as kt

    rejected = json.loads(kt._handle_complete({"summary": "done without typed metadata"}))
    assert "metadata.quality_loop" in rejected["error"]
    assert "still in-flight" in rejected["error"]
    _assert_running(task_id)

    accepted = json.loads(kt._handle_complete({
        "summary": "typed handoff",
        "metadata": {"quality_loop": {"schema": "quality-loop/v2", "role": "examine"}},
    }))
    assert accepted["ok"] is True


def test_summary_contract_rejects_unwrapped_json_and_allows_retry(monkeypatch, tmp_path):
    task_id = _controller_worker(
        monkeypatch,
        tmp_path,
        {
            "kind": "summary_json_namespace",
            "namespace": "quality_loop",
            "prefix": "QUALITY_LOOP_JSON: ",
        },
    )
    from tools import kanban_tools as kt

    rejected = json.loads(kt._handle_complete({
        "summary": 'QUALITY_LOOP_JSON: {"schema":"quality-loop/v2","role":"examine"}',
    }))
    assert "quality_loop" in rejected["error"]
    assert "still in-flight" in rejected["error"]
    _assert_running(task_id)

    accepted = json.loads(kt._handle_complete({
        "summary": (
            'QUALITY_LOOP_JSON: {"quality_loop":'
            '{"schema":"quality-loop/v2","role":"examine"}}'
        ),
    }))
    assert accepted["ok"] is True