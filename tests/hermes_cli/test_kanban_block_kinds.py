"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import _task_to_dict


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_does_not_reset_recurrence_counter(kanban_home: Path) -> None:
    """The crux of the fix: unblock must preserve the loop counter."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 1  # NOT reset to 0
        assert t.block_kind == "needs_input"  # kind preserved for comparison


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


def test_initially_blocked_task_stays_quarantined_on_same_capability_loop(
    kanban_home: Path,
) -> None:
    """An approval-gated task must not enter triage and trigger auto-decompose."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="approval-gated",
            assignee="worker",
            initial_status="blocked",
        )
        # Generic unblock has no actor/evidence and may be called by a cron;
        # only the audited coordinator-promotion path opens this gate.
        assert not kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.promote_task(conn, tid, actor="coordinator") == (True, None)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="no confined backend", kind="capability")
        assert kb.promote_task(conn, tid, actor="coordinator") == (True, None)
        assert kb.claim_task(conn, tid, claimer="worker") is not None

        assert kb.block_task(conn, tid, reason="still no confined backend", kind="capability")

        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task.status == "blocked"
    assert _task_to_dict(task)["requires_manual_promotion"] is True
    assert any(event.kind == "block_loop_manual_review_required" for event in events)
    assert not any(event.kind == "block_loop_detected" for event in events)


def test_initially_blocked_task_is_not_auto_promoted_after_dependencies_clear(
    kanban_home: Path,
) -> None:
    """Recovery must require a fresh explicit promotion, never recompute-ready."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn,
            title="approval-gated-child",
            assignee="worker",
            parents=[parent],
            initial_status="blocked",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_block_can_quarantine_a_todo_task_after_unsafe_graph_mutation(
    kanban_home: Path,
) -> None:
    """The coordinator needs a durable containment operation after fan-out."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="mutated-root", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))

        assert kb.block_task(conn, tid, reason="containment", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"


def test_legacy_initial_block_event_backfills_manual_promotion_gate(tmp_path: Path) -> None:
    """The approval contract survives migration of an already-queued task."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE task_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'approval-gated', 'blocked', 1)"
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES ('legacy', 'blocked', ?, 1)",
        (json.dumps({"reason": "initial_status_blocked"}),),
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)
    row = conn.execute(
        "SELECT requires_manual_promotion FROM tasks WHERE id = 'legacy'"
    ).fetchone()
    assert row["requires_manual_promotion"] == 1

    # Reopening the board reruns the additive migration; the backfill must be
    # harmless and retain the gate.
    kb._migrate_add_optional_columns(conn)
    again = conn.execute(
        "SELECT requires_manual_promotion FROM tasks WHERE id = 'legacy'"
    ).fetchone()
    assert again["requires_manual_promotion"] == 1
    conn.close()


def test_legacy_task_backfills_worker_execution_mode(tmp_path: Path) -> None:
    """A missing mode on a historic row remains explicitly worker-dispatched."""
    db_path = tmp_path / "legacy-execution-mode.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE task_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'historic worker task', 'ready', 1)"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)
    row = conn.execute(
        "SELECT execution_mode FROM tasks WHERE id = 'legacy'"
    ).fetchone()

    assert row["execution_mode"] == "worker"
    conn.close()


def test_coordinator_only_requires_initial_manual_block(kanban_home: Path) -> None:
    """A no-worker contract cannot enter the generic ready queue at creation."""
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="initial_status='blocked'"):
            kb.create_task(
                conn,
                title="coordinator task",
                assignee="worker",
                execution_mode="coordinator_only",
            )


def test_coordinator_only_manual_promotion_refuses_dispatch_before_popen(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual promotion must not let a coordinator-only task reach Popen."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    popen_calls: list[tuple[object, ...]] = []

    def popen_sentinel(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError("coordinator-only task reached subprocess.Popen")

    monkeypatch.setattr(subprocess, "Popen", popen_sentinel)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="no worker",
            assignee="worker",
            execution_mode="coordinator_only",
            initial_status="blocked",
        )
        assert kb.promote_task(conn, tid, actor="coordinator") == (True, None)

        result = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert result.skipped_coordinator_only == [tid]
    assert task is not None
    assert task.status == "ready"
    assert task.workspace_path is None
    assert not popen_calls
    assert not any(event.kind == "claimed" for event in events)
    refusal = [event for event in events if event.kind == "dispatch_refused_coordinator_only"]
    assert len(refusal) == 1
    assert refusal[0].payload == {"execution_mode": "coordinator_only"}


def test_coordinator_only_refuses_direct_claim_after_manual_promotion(
    kanban_home: Path,
) -> None:
    """The public claim primitive cannot bypass the dispatcher admission gate."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="no direct worker claim",
            assignee="worker",
            execution_mode="coordinator_only",
            initial_status="blocked",
        )
        assert kb.promote_task(conn, tid, actor="coordinator") == (True, None)

        assert kb.claim_task(conn, tid, claimer="worker") is None
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert task is not None
    assert task.status == "ready"
    assert not task.current_run_id
    refusal = [event for event in events if event.kind == "claim_refused_coordinator_only"]
    assert len(refusal) == 1
    assert refusal[0].payload == {"execution_mode": "coordinator_only"}


def test_coordinator_only_review_never_claims_or_spawns(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The separate review-worker path is covered by the same hard gate."""
    popen_calls: list[tuple[object, ...]] = []

    def popen_sentinel(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError("coordinator-only review reached subprocess.Popen")

    monkeypatch.setattr(subprocess, "Popen", popen_sentinel)

    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="no review worker",
            assignee="reviewer",
            execution_mode="coordinator_only",
            initial_status="blocked",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))

        assert kb.claim_review_task(conn, tid, claimer="reviewer") is None
        result = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert result.skipped_coordinator_only == [tid]
    assert task is not None
    assert task.status == "review"
    assert not task.current_run_id
    assert not popen_calls
    assert any(event.kind == "claim_refused_coordinator_only" for event in events)
    assert any(event.kind == "dispatch_refused_coordinator_only" for event in events)


def test_blocked_manual_task_can_be_enrolled_coordinator_only_once(
    kanban_home: Path,
) -> None:
    """The bootstrap enrollment is one-way, blocked-only, and auditable."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="legacy coordinator task",
            assignee="worker",
            initial_status="blocked",
        )

        ok, error = kb.set_coordinator_only(
            conn,
            tid,
            actor="coordinator",
            reason="recorded bootstrap exception",
        )
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert (ok, error) == (True, None)
    assert task is not None
    assert task.execution_mode == "coordinator_only"
    enrollment = [event for event in events if event.kind == "coordinator_only_enrolled"]
    assert len(enrollment) == 1
    assert enrollment[0].payload == {
        "actor": "coordinator",
        "reason": "recorded bootstrap exception",
        "previous_execution_mode": "worker",
    }


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """Dependency waits never enter the human 'blocked' bucket."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None
