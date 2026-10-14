"""Controller-owned Kanban task creation with immutable provenance.

Installed controller plugins run in the trusted Hermes process but ordinary
Kanban surfaces must not be able to manufacture their provenance.  This module
keeps that narrow boundary out of the generic task-creation API.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

PROVENANCE_EVENT_KIND = "controller_provenance"
PROVENANCE_SCHEMA = "hermes/controller-task/v1"
_CONTROLLER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_METADATA_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ControllerHandoffError(ValueError):
    """A controller-owned card attempted an unauthenticated completion."""


def _validate_handoff_contract(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("handoff contract must be an object")
    kind = value.get("kind")
    namespace = value.get("namespace")
    if not isinstance(kind, str):
        raise ValueError("handoff contract kind must be a string")
    if not isinstance(namespace, str) or not _METADATA_NAMESPACE.fullmatch(namespace):
        raise ValueError(
            "handoff contract namespace must be 1-64 lowercase alphanumeric or underscore characters"
        )
    if kind == "metadata_namespace":
        if set(value) != {"kind", "namespace"}:
            raise ValueError("metadata handoff contract accepts only kind and namespace")
        return {"kind": kind, "namespace": namespace}
    if kind in {"summary_json_namespace", "metadata_or_summary_json_namespace"}:
        prefix = value.get("prefix")
        if (
            set(value) != {"kind", "namespace", "prefix"}
            or not isinstance(prefix, str)
            or not prefix
            or len(prefix) > 128
            or "\n" in prefix
            or "\r" in prefix
        ):
            raise ValueError(
                "summary JSON handoff contract requires a one-line prefix of 1-128 characters"
            )
        return {"kind": kind, "namespace": namespace, "prefix": prefix}
    raise ValueError(
        "handoff contract kind must be metadata_namespace, summary_json_namespace, "
        "or metadata_or_summary_json_namespace"
    )


def _provenance_payload(
    controller: str,
    body: str | None,
    handoff_contract: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "controller": controller,
        "body_sha256": hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
    }
    if handoff_contract is not None:
        payload["handoff_contract"] = handoff_contract
    return payload


def _require_matching_provenance(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    controller: str,
    body: str | None,
    handoff_contract: dict[str, str] | None,
) -> None:
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    expected = _provenance_payload(controller, body, handoff_contract)
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
    handoff_contract: dict[str, str] | None = None,
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
    handoff_contract = _validate_handoff_contract(handoff_contract)

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
                    handoff_contract=handoff_contract,
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
            _provenance_payload(controller, body, handoff_contract),
        )
        return task_id


def handoff_contract(
    conn: sqlite3.Connection,
    task_id: str,
) -> dict[str, str] | None:
    """Return a controller task's authenticated handoff contract, if any."""
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    events = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == PROVENANCE_EVENT_KIND
    ]
    if not events:
        return None
    if task is None or len(events) != 1 or not isinstance(events[0].payload, dict):
        raise ValueError("controller task provenance is malformed")
    payload = events[0].payload
    base_keys = {"schema", "controller", "body_sha256"}
    payload_keys = frozenset(payload)
    if payload_keys not in {frozenset(base_keys), frozenset(base_keys | {"handoff_contract"})}:
        raise ValueError("controller task provenance is malformed")
    if (
        payload.get("schema") != PROVENANCE_SCHEMA
        or payload.get("controller") != task.created_by
        or payload.get("body_sha256")
        != hashlib.sha256((task.body or "").encode("utf-8")).hexdigest()
    ):
        raise ValueError("controller task provenance does not match the task")
    contract = _validate_handoff_contract(payload.get("handoff_contract"))
    if "handoff_contract" in payload and contract is None:
        raise ValueError("controller task provenance is malformed")
    return contract


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def validate_body_update(
    conn: sqlite3.Connection,
    task_id: str,
    body: str | None,
) -> None:
    """Keep authenticated controller card bodies immutable on every edit surface."""
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    events = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == PROVENANCE_EVENT_KIND
    ]
    if not events:
        return
    handoff_contract(conn, task_id)
    if task is None or body != task.body:
        raise ValueError("controller task body is immutable")


def validate_completion_handoff(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: str | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Reject a malformed controller handoff before the task becomes terminal."""
    try:
        contract = handoff_contract(conn, task_id)
    except ValueError as exc:
        raise ControllerHandoffError(str(exc)) from exc
    if contract is None:
        return
    namespace = contract["namespace"]
    metadata_value = metadata.get(namespace) if isinstance(metadata, dict) else None
    if contract["kind"] == "metadata_namespace":
        value = metadata_value
        if not isinstance(value, dict) or not value:
            raise ControllerHandoffError(
                f"this controller-owned task requires a non-empty metadata.{namespace} object"
            )
        return

    if (
        contract["kind"] == "metadata_or_summary_json_namespace"
        and isinstance(metadata_value, dict)
        and metadata_value
    ):
        return

    prefix = contract["prefix"]
    if not isinstance(summary, str) or not summary.startswith(prefix):
        if contract["kind"] == "metadata_or_summary_json_namespace":
            raise ControllerHandoffError(
                f"this controller-owned task requires either a non-empty metadata.{namespace} "
                f"object or summary JSON prefixed by {prefix!r}"
            )
        raise ControllerHandoffError(
            f"this controller-owned task requires summary JSON prefixed by {prefix!r}"
        )
    try:
        envelope = json.loads(
            summary[len(prefix):],
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ControllerHandoffError("controller completion summary contains invalid JSON") from exc
    value = envelope.get(namespace) if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {namespace}
        or not isinstance(value, dict)
        or not value
    ):
        raise ControllerHandoffError(
            f"controller completion summary must be an object whose only key is {namespace!r} "
            "and whose value is a non-empty object"
        )
