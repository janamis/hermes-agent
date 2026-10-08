"""Controller-owned Kanban task creation with immutable provenance.

Installed controller plugins run in the trusted Hermes process but ordinary
Kanban surfaces must not be able to manufacture their provenance.  This module
keeps that narrow boundary out of the generic task-creation API.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

PROVENANCE_EVENT_KIND = "controller_provenance"
PROVENANCE_SCHEMA = "hermes/controller-task/v1"
_CONTROLLER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _provenance_payload(controller: str, body: str | None) -> dict[str, str]:
    return {
        "schema": PROVENANCE_SCHEMA,
        "controller": controller,
        "body_sha256": hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
    }


def _require_matching_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    controller: str,
    body: str | None,
) -> None:
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    expected = _provenance_payload(controller, body)
    events = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == PROVENANCE_EVENT_KIND
    ]
    if (
        task is None
        or task.created_by != controller
        or task.body != body
        or len(events) != 1
        or events[0].payload != expected
    ):
        raise ValueError(
            "idempotency key belongs to a task without matching controller provenance"
        )


def create_controller_task(
    conn: sqlite3.Connection,
    *,
    controller: str,
    **task_kwargs: Any,
) -> str:
    """Create one controller-owned task and its provenance atomically.

    ``controller`` is a stable plugin/controller identifier.  The generic
    ``create_task`` entry point remains unchanged; only trusted in-process
    controllers call this function.  Reusing an idempotency key is allowed
    only when the existing task has the same controller, body, and immutable
    provenance event.
    """
    from hermes_cli import kanban_db as kb

    if not isinstance(controller, str) or not _CONTROLLER_ID.fullmatch(controller):
        raise ValueError(
            "controller must be 1-64 lowercase alphanumeric or hyphen characters"
        )
    if "created_by" in task_kwargs:
        raise ValueError("create_controller_task sets created_by itself")

    body = task_kwargs.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be a string or null")
    idempotency_key = task_kwargs.get("idempotency_key")

    with kb.write_txn(conn, allow_nested=True):
        if idempotency_key:
            existing = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? "
                "AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                task_id = str(existing["id"])
                _require_matching_provenance(
                    conn,
                    task_id,
                    controller=controller,
                    body=body,
                )
                return task_id

        task_id = kb.create_task(
            conn,
            created_by=controller,
            **task_kwargs,
        )
        kb._append_event(
            conn,
            task_id,
            PROVENANCE_EVENT_KIND,
            _provenance_payload(controller, body),
        )
        return task_id
