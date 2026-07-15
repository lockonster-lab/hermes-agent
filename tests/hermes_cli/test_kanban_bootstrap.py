"""Tests for the narrow, offline self-repair bootstrap declaration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

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


def test_self_repair_declaration_persists_expanded_workspace_path(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Immutable workspace identity never retains a home-relative alias."""
    monkeypatch.setenv("HOME", str(tmp_path))

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="expanded bootstrap target",
            workspace_kind="worktree",
            workspace_path="~/bootstrap-target",
            branch_name="codex/bootstrap-target",
            initial_status="blocked",
            execution_mode="coordinator_only",
            bootstrap_kind="self_repair",
            bootstrap_source_root="/private/tmp/bootstrap-source",
            bootstrap_base_revision="a" * 40,
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workspace_path == str(tmp_path / "bootstrap-target")


def _init_git_repo(repo: Path) -> Path:
    """Create one offline Git source checkout for bootstrap-path tests."""
    repo.mkdir()
    for command in (
        ["git", "init", "-b", "main", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("bootstrap test\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    return repo


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _create_self_repair_task(
    conn: object,
    *,
    source_root: Path,
    workspace: Path,
    base_revision: str,
) -> str:
    return kb.create_task(
        conn,
        title="prepare self-repair workspace",
        assignee="coordinator",
        workspace_kind="worktree",
        workspace_path=str(workspace),
        branch_name="codex/bootstrap-self-repair",
        initial_status="blocked",
        execution_mode="coordinator_only",
        bootstrap_kind="self_repair",
        bootstrap_source_root=str(source_root),
        bootstrap_base_revision=base_revision,
    )


def _bootstrap_event_payload(
    *,
    task_id: str,
    source_root: Path,
    workspace: Path,
    base_revision: str,
) -> dict[str, str]:
    return {
        "task_id": task_id,
        "actor": "coordinator",
        "approval": "recorded single-use gate",
        "bootstrap_kind": "self_repair",
        "bootstrap_source_root": str(source_root),
        "bootstrap_base_revision": base_revision,
        "workspace_path": str(workspace),
        "branch_name": "codex/bootstrap-self-repair",
    }


def test_bootstrap_prepare_refuses_non_bootstrap_contract_before_git(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Git subprocess is permitted until the immutable task gate passes."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="ordinary coordinator card",
            workspace_kind="worktree",
            workspace_path="/private/tmp/not-bootstrap",
            branch_name="codex/not-bootstrap",
            initial_status="blocked",
            execution_mode="coordinator_only",
        )

        def fail_if_git_runs(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"unexpected Git subprocess: {args!r}")

        monkeypatch.setattr(kb.subprocess, "run", fail_if_git_runs)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

        assert not result.ok
        assert "self_repair" in (result.reason or "")
        assert kb.get_task(conn, task_id).status == "blocked"
        assert all(event.kind != "bootstrap_workspace_prepared" for event in kb.list_events(conn, task_id))


def test_bootstrap_prepare_rejects_base_mismatch_before_worktree_add(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale declared base must never materialize the target workspace."""
    source_root = _init_git_repo(tmp_path / "source")
    target = tmp_path / "worktrees" / "repair"
    commands: list[list[str]] = []
    real_run = kb.subprocess.run

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision="a" * 40,
        )

        def record_git(command: list[str], **kwargs: object) -> object:
            commands.append(command)
            return real_run(command, **kwargs)

        monkeypatch.setattr(kb.subprocess, "run", record_git)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not result.ok
    assert "base revision" in (result.reason or "")
    assert not target.exists()
    assert not any(command[3:5] == ["worktree", "add"] for command in commands)


def test_bootstrap_prepare_refuses_any_task_run_before_git(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical execution makes a self-repair contract ineligible forever."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision="a" * 40,
        )

        monkeypatch.setattr(kb, "list_runs", lambda *_args, **_kwargs: [object()])

        def fail_if_git_runs(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"unexpected Git subprocess: {args!r}")

        monkeypatch.setattr(kb.subprocess, "run", fail_if_git_runs)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not result.ok
    assert "run" in (result.reason or "")
    assert not target.exists()


def test_bootstrap_prepare_refuses_preexisting_target_without_audit(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing path is never adopted retroactively, even if it looks usable."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = tmp_path / "worktrees" / "repair"
    target.mkdir(parents=True)

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision="a" * 40,
        )

        def fail_if_git_runs(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"unexpected Git subprocess: {args!r}")

        monkeypatch.setattr(kb.subprocess, "run", fail_if_git_runs)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not result.ok
    assert "audit" in (result.reason or "")
    assert target.is_dir()


def test_bootstrap_prepare_refuses_audit_event_with_missing_target(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared event is authoritative and can never recreate lost state."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision="a" * 40,
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "bootstrap_workspace_prepared",
                _bootstrap_event_payload(
                    task_id=task_id,
                    source_root=source_root,
                    workspace=target,
                    base_revision="a" * 40,
                ),
            )

        def fail_if_git_runs(*args: object, **kwargs: object) -> object:
            raise AssertionError(f"unexpected Git subprocess: {args!r}")

        monkeypatch.setattr(kb.subprocess, "run", fail_if_git_runs)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not result.ok
    assert "missing" in (result.reason or "")
    assert not target.exists()


def test_bootstrap_prepare_creates_exact_workspace_once_and_audits(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sole allowed path creates an exact linked checkout and remains blocked."""
    source_root = _init_git_repo(tmp_path / "source")
    base_revision = _git_output(source_root, "rev-parse", "HEAD")
    target = tmp_path / "worktrees" / "repair"
    commands: list[list[str]] = []
    real_run = kb.subprocess.run

    def record_git(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(kb.subprocess, "run", record_git)

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=base_revision,
        )

        first = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        second = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

        task = kb.get_task(conn, task_id)
        prepared_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "bootstrap_workspace_prepared"
        ]

    assert first.ok
    assert not first.idempotent
    assert second.ok
    assert second.idempotent
    assert task is not None
    assert task.status == "blocked"
    assert _git_output(target, "rev-parse", "HEAD") == base_revision
    assert _git_output(target, "branch", "--show-current") == "codex/bootstrap-self-repair"
    assert len(prepared_events) == 1
    assert prepared_events[0].payload["approval"] == "recorded single-use gate"
    add_commands = [
        command for command in commands
        if command[3:5] == ["worktree", "add"]
    ]
    assert add_commands == [[
        "git",
        "-C",
        str(source_root),
        "worktree",
        "add",
        "-b",
        "codex/bootstrap-self-repair",
        str(target),
        base_revision,
    ]]


def test_bootstrap_prepare_leaves_workspace_when_contract_changes_after_add(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-add state race fails without audit or destructive cleanup."""
    source_root = _init_git_repo(tmp_path / "source")
    base_revision = _git_output(source_root, "rev-parse", "HEAD")
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=base_revision,
        )
        real_get_task = kb.get_task
        calls = 0

        def changed_after_add(connection: object, requested_id: str) -> object:
            nonlocal calls
            calls += 1
            task = real_get_task(connection, requested_id)
            if calls > 1 and task is not None:
                return replace(task, status="ready")
            return task

        monkeypatch.setattr(kb, "get_task", changed_after_add)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        events = kb.list_events(conn, task_id)

    assert not result.ok
    assert target.is_dir()
    assert all(event.kind != "bootstrap_workspace_prepared" for event in events)


def test_bootstrap_prepare_refuses_idempotence_after_target_head_drifts(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """An audit event never masks a changed checkout identity."""
    source_root = _init_git_repo(tmp_path / "source")
    base_revision = _git_output(source_root, "rev-parse", "HEAD")
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=base_revision,
        )
        first = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        assert first.ok

        (target / "drift.txt").write_text("drift\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(target), "add", "drift.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "commit", "-m", "drift"],
            check=True,
            capture_output=True,
            text=True,
        )

        second = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        prepared_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "bootstrap_workspace_prepared"
        ]

    assert not second.ok
    assert "HEAD" in (second.reason or "")
    assert len(prepared_events) == 1
