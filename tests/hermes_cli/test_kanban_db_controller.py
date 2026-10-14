"""Behavior contracts for controller-owned Kanban task creation."""

from __future__ import annotations

import hashlib
import json
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


def test_controller_task_records_and_reads_handoff_contract(kanban_home):
    contract = {"kind": "metadata_namespace", "namespace": "quality_loop"}
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="typed handoff",
            body="return a typed handoff",
            handoff_contract=contract,
        )

        provenance = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == controller_tasks.PROVENANCE_EVENT_KIND
        ]
        persisted = controller_tasks.handoff_contract(conn, task_id)

    assert provenance[0].payload["handoff_contract"] == contract
    assert persisted == contract


def test_database_completion_enforces_authenticated_handoff_contract(kanban_home):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="typed handoff",
            handoff_contract={"kind": "metadata_namespace", "namespace": "quality_loop"},
        )

        with pytest.raises(ValueError, match="metadata.quality_loop"):
            kb.complete_task(conn, task_id, summary="untyped handoff")

        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.complete_task(
            conn,
            task_id,
            summary="typed handoff",
            metadata={"quality_loop": {"schema": "quality-loop/v2"}},
        )


def test_database_completion_accepts_either_authenticated_handoff_channel(kanban_home):
    contract = {
        "kind": "metadata_or_summary_json_namespace",
        "namespace": "quality_loop",
        "prefix": "QUALITY_LOOP_JSON: ",
    }
    with kbc.connect() as conn:
        metadata_task = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="metadata handoff",
            handoff_contract=contract,
        )
        summary_task = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="summary handoff",
            handoff_contract=contract,
        )

        assert kb.complete_task(
            conn,
            metadata_task,
            summary="metadata channel",
            metadata={"quality_loop": {"schema": "quality-loop/v2"}},
        )
        assert kb.complete_task(
            conn,
            summary_task,
            summary='QUALITY_LOOP_JSON: {"quality_loop":{"schema":"quality-loop/v2"}}',
        )


def test_summary_handoff_accepts_result_fallback(kanban_home):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="result handoff",
            handoff_contract={
                "kind": "summary_json_namespace",
                "namespace": "quality_loop",
                "prefix": "QUALITY_LOOP_JSON: ",
            },
        )
        assert kb.complete_task(
            conn,
            task_id,
            result='QUALITY_LOOP_JSON: {"quality_loop":{"schema":"quality-loop/v2"}}',
        )


def test_completion_revalidates_handoff_inside_terminal_transaction(kanban_home, monkeypatch):
    contract = {"kind": "metadata_namespace", "namespace": "quality_loop"}
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="race protected",
            body="authenticated body",
            handoff_contract=contract,
        )
        real_validate = controller_tasks.validate_completion_handoff
        calls = 0

        def mutate_after_first_validation(*args, **kwargs):
            nonlocal calls
            calls += 1
            real_validate(*args, **kwargs)
            if calls == 1:
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET body = ? WHERE id = ?",
                        ("forged body", task_id),
                    )

        monkeypatch.setattr(
            controller_tasks,
            "validate_completion_handoff",
            mutate_after_first_validation,
        )
        with pytest.raises(ValueError, match="provenance does not match"):
            kb.complete_task(
                conn,
                task_id,
                summary="typed handoff",
                metadata={"quality_loop": {"schema": "quality-loop/v2"}},
            )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert calls == 2


def test_controller_task_without_contract_retains_legacy_completion(kanban_home):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="legacy-controller",
            title="legacy handoff",
        )
        assert kb.complete_task(conn, task_id, summary="legacy prose handoff")


def test_tampered_legacy_provenance_shape_cannot_downgrade_contract(kanban_home):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="contract downgrade",
            body="authenticated body",
            handoff_contract={"kind": "metadata_namespace", "namespace": "quality_loop"},
        )
        provenance = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == controller_tasks.PROVENANCE_EVENT_KIND
        ]
        assert len(provenance) == 1
        assert isinstance(provenance[0].payload, dict)
        payload = dict(provenance[0].payload)
        payload.pop("handoff_contract")
        payload["unexpected"] = "tampered"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), provenance[0].id),
            )

        with pytest.raises(ValueError, match="provenance is malformed"):
            kb.complete_task(conn, task_id, summary="untyped prose")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_summary_handoff_rejects_nonstandard_json_constants(kanban_home, constant):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="strict JSON handoff",
            body="authenticated body",
            handoff_contract={
                "kind": "summary_json_namespace",
                "namespace": "quality_loop",
                "prefix": "QUALITY_LOOP_JSON: ",
            },
        )
        with pytest.raises(ValueError, match="invalid JSON"):
            kb.complete_task(
                conn,
                task_id,
                summary=(
                    'QUALITY_LOOP_JSON: {"quality_loop":{"value":'
                    + constant
                    + "}}"
                ),
            )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"


def test_controller_task_body_is_immutable(kanban_home):
    with kbc.connect() as conn:
        task_id = controller_tasks.create_controller_task(
            conn,
            controller="quality-loop",
            title="immutable body",
            body="authenticated body",
        )
        with pytest.raises(ValueError, match="body is immutable"):
            kb.edit_task(conn, task_id, body="forged body")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.body == "authenticated body"


@pytest.mark.parametrize(
    "contract",
    [
        {"kind": [], "namespace": "quality_loop"},
        {"kind": "metadata_namespace", "namespace": "Quality Loop"},
        {"kind": "summary_json_namespace", "namespace": "quality_loop"},
        {
            "kind": "summary_json_namespace",
            "namespace": "quality_loop",
            "prefix": "line one\nline two",
        },
        {"kind": "unknown", "namespace": "quality_loop"},
    ],
)
def test_controller_task_rejects_invalid_handoff_contract(kanban_home, contract):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="handoff contract"):
            controller_tasks.create_controller_task(
                conn,
                controller="quality-loop",
                title="invalid handoff",
                handoff_contract=contract,
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
