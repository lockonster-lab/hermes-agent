"""Tests for the narrow, offline self-repair bootstrap declaration."""

from __future__ import annotations

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


def test_create_task_persists_immutable_self_repair_declaration(
    kanban_home: Path,
) -> None:
    """Only the initial no-worker contract may carry bootstrap identity."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="self-repair bootstrap",
            assignee="coordinator",
            workspace_kind="worktree",
            workspace_path="/private/tmp/bootstrap-target",
            branch_name="codex/bootstrap-target",
            initial_status="blocked",
            execution_mode="coordinator_only",
            bootstrap_kind="self_repair",
            bootstrap_source_root="/private/tmp/bootstrap-source",
            bootstrap_base_revision="a" * 40,
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.bootstrap_kind == "self_repair"
    assert task.bootstrap_source_root == "/private/tmp/bootstrap-source"
    assert task.bootstrap_base_revision == "a" * 40
    assert _task_to_dict(task)["bootstrap_kind"] == "self_repair"


def test_self_repair_declaration_rejects_any_worker_lifecycle(
    kanban_home: Path,
) -> None:
    """A bootstrap contract cannot be created as a generic worker task."""
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="coordinator_only"):
            kb.create_task(
                conn,
                title="unsafe bootstrap",
                workspace_kind="worktree",
                workspace_path="/private/tmp/bootstrap-target",
                branch_name="codex/bootstrap-target",
                bootstrap_kind="self_repair",
                bootstrap_source_root="/private/tmp/bootstrap-source",
                bootstrap_base_revision="a" * 40,
            )


@pytest.mark.parametrize(
    ("source_root", "base_revision", "message"),
    [
        ("relative/source", "a" * 40, "absolute"),
        ("/private/tmp/bootstrap-source", "not-a-revision", "revision"),
    ],
)
def test_self_repair_declaration_rejects_ambiguous_identity(
    kanban_home: Path,
    source_root: str,
    base_revision: str,
    message: str,
) -> None:
    """The persisted authorization cannot contain a relative path or loose SHA."""
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match=message):
            kb.create_task(
                conn,
                title="ambiguous bootstrap",
                workspace_kind="worktree",
                workspace_path="/private/tmp/bootstrap-target",
                branch_name="codex/bootstrap-target",
                initial_status="blocked",
                execution_mode="coordinator_only",
                bootstrap_kind="self_repair",
                bootstrap_source_root=source_root,
                bootstrap_base_revision=base_revision,
            )
