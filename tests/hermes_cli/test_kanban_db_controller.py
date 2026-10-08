"""Behavior contracts for controller-owned Kanban task creation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_controller as controller_tasks


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_controller_task_records_immutable_body_provenance(kanban_home):
    body = "controller-owned instructions\n"
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="trusted task",
            body=body,
            assignee="worker",
        )

        task = kb.get_task(conn, task_id)
        provenance = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == controller_tasks.PROVENANCE_EVENT_KIND
        ]

    assert task is not None
    assert task.created_by == "quality-loop"
    assert len(provenance) == 1
    assert provenance[0].payload == {
        "schema": controller_tasks.PROVENANCE_SCHEMA,
        "controller": "quality-loop",
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def test_controller_task_rejects_invalid_controller_identity(kanban_home):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="controller"):
            controller_tasks.create_controller_task(
                conn,
                controller="Quality Loop/../../user",
                title="untrusted identity",
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_controller_task_idempotency_returns_only_matching_provenance(kanban_home):
    body = "same trusted body"
    with kbc.connect() as conn:
        first = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="trusted task",
            body=body,
            idempotency_key="controller:campaign:stage",
        )
        second = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="trusted task",
            body=body,
            idempotency_key="controller:campaign:stage",
        )
        provenance = [
            event for event in kb.list_events(conn, first)
            if event.kind == controller_tasks.PROVENANCE_EVENT_KIND
        ]

    assert second == first
    assert len(provenance) == 1


def test_controller_task_refuses_to_bless_existing_generic_task(kanban_home):
    with kbc.connect() as conn:
        existing = kb.create_task(
            conn,
            title="generic task",
            body="same trusted body",
            created_by="quality-loop",
            idempotency_key="controller:campaign:stage",
        )

        with pytest.raises(ValueError, match="provenance"):
            controller_tasks.create_controller_task(
                conn,
                controller="quality-loop",
                title="generic task",
                body="same trusted body",
                idempotency_key="controller:campaign:stage",
            )

        assert not any(
            event.kind == controller_tasks.PROVENANCE_EVENT_KIND
            for event in kb.list_events(conn, existing)
        )
