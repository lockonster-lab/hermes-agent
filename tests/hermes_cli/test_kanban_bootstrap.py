"""Tests for the narrow, offline self-repair bootstrap declaration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shlex
import sqlite3
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


def test_self_repair_declaration_persists_canonical_path_identity(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """Symlink aliases are normalized before immutable identity is stored."""
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    source = real_parent / "source"
    source.mkdir()

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="canonical bootstrap paths",
            workspace_kind="worktree",
            workspace_path=str(alias_parent / "target"),
            branch_name="codex/bootstrap-target",
            initial_status="blocked",
            execution_mode="coordinator_only",
            bootstrap_kind="self_repair",
            bootstrap_source_root=str(alias_parent / "source"),
            bootstrap_base_revision="a" * 40,
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workspace_path == str(real_parent / "target")
    assert task.bootstrap_source_root == str(source)


def test_self_repair_workspace_identity_is_immutable(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """Generic workspace setters cannot rewrite a bootstrap declaration."""
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source,
            workspace=target,
            base_revision="a" * 40,
        )

        with pytest.raises(ValueError, match="immutable"):
            kb.set_workspace_path(conn, task_id, tmp_path / "other-target")
        with pytest.raises(ValueError, match="immutable"):
            kb.set_branch_name(conn, task_id, "codex/other-branch")

        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workspace_path == str(target)
    assert task.branch_name == "codex/bootstrap-self-repair"


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


def test_bootstrap_prepare_ignores_inherited_git_identity_and_path(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-controlled Git environment cannot redirect the approved source."""
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
        monkeypatch.setenv("PATH", "/definitely/not/a/trusted/git/path")
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "redirected.git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "redirected-worktree"))
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath'='malicious'")

        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert result.ok
    assert target.is_dir()
    git_env = kb._bootstrap_git_env()
    assert git_env["PATH"] == "/usr/bin:/bin"
    assert git_env["GIT_NO_LAZY_FETCH"] == "1"
    assert git_env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert "GIT_DIR" not in git_env
    assert "GIT_WORK_TREE" not in git_env
    assert "GIT_CONFIG_PARAMETERS" not in git_env


def test_bootstrap_prepare_rejects_executable_checkout_filter(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """A repository-configured smudge command is refused before checkout."""
    source_root = _init_git_repo(tmp_path / "source")
    (source_root / ".gitattributes").write_text(
        "README.md filter=bootstrap-evil\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(source_root), "add", ".gitattributes"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "commit", "-m", "add attributes"],
        check=True,
        capture_output=True,
        text=True,
    )
    marker = tmp_path / "smudge-ran"
    included_config = tmp_path / "checkout-filter.gitconfig"
    included_config.write_text(
        "[filter \"bootstrap-evil\"]\n"
        f"\tsmudge = /usr/bin/touch {shlex.quote(str(marker))}; /bin/cat\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(source_root), "config", "include.path", str(included_config)],
        check=True,
        capture_output=True,
        text=True,
    )
    base_revision = _git_output(source_root, "rev-parse", "HEAD")
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=base_revision,
        )
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        prepared_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "bootstrap_workspace_prepared"
        ]

    assert not result.ok
    assert "filter" in (result.reason or "")
    assert not marker.exists()
    assert not target.exists()
    assert prepared_events == []


def test_bootstrap_prepare_rechecks_source_before_audit(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source drift after checkout prevents the durable prepared event."""
    source_root = _init_git_repo(tmp_path / "source")
    base_revision = _git_output(source_root, "rev-parse", "HEAD")
    target = tmp_path / "worktrees" / "repair"
    real_validate_target = kb._validate_bootstrap_target
    validation_calls = 0

    def drift_source_after_first_validation(*args: object, **kwargs: object) -> object:
        nonlocal validation_calls
        error = real_validate_target(*args, **kwargs)
        validation_calls += 1
        if validation_calls == 1 and error is None:
            (source_root / "source-drift.txt").write_text("drift\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(source_root), "add", "source-drift.txt"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(source_root), "commit", "-m", "source drift"],
                check=True,
                capture_output=True,
                text=True,
            )
        return error

    monkeypatch.setattr(kb, "_validate_bootstrap_target", drift_source_after_first_validation)

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=base_revision,
        )
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        prepared_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "bootstrap_workspace_prepared"
        ]

    assert not result.ok
    assert "source HEAD" in (result.reason or "")
    assert validation_calls >= 1
    assert target.is_dir()
    assert prepared_events == []


def test_bootstrap_prepare_rechecks_target_before_idempotent_success(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target drift during an idempotence check cannot return success."""
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

        real_validate_target = kb._validate_bootstrap_target
        validation_calls = 0

        def drift_target_after_first_validation(*args: object, **kwargs: object) -> object:
            nonlocal validation_calls
            error = real_validate_target(*args, **kwargs)
            validation_calls += 1
            if validation_calls == 1 and error is None:
                (target / "target-drift.txt").write_text("drift\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(target), "add", "target-drift.txt"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(target), "commit", "-m", "target drift"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            return error

        monkeypatch.setattr(kb, "_validate_bootstrap_target", drift_target_after_first_validation)
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
    assert validation_calls >= 2
    assert len(prepared_events) == 1


def test_bootstrap_prepare_requires_registered_target_record(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotence fails if source registration no longer names the target."""
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

        real_run = kb._run_bootstrap_git

        def hide_target_registration(
            root: Path,
            args: list[str],
            *,
            timeout: int = 30,
        ) -> tuple[object, object]:
            if root == source_root and args == ["worktree", "list", "--porcelain", "-z"]:
                stdout = (
                    f"worktree {source_root}\0"
                    f"HEAD {base_revision}\0"
                    "branch refs/heads/main\0\0"
                )
                return subprocess.CompletedProcess(args, 0, stdout, ""), None
            return real_run(root, args, timeout=timeout)

        monkeypatch.setattr(kb, "_run_bootstrap_git", hide_target_registration)
        second = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not second.ok
    assert "registered" in (second.reason or "")


def test_bootstrap_prepare_rejects_wrong_common_dir_after_audit(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """A replacement repository at the audited path is never adopted."""
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

        target.rename(tmp_path / "registered-worktree-moved-aside")
        _init_git_repo(target)
        second = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not second.ok
    assert "common directory" in (second.reason or "")


@pytest.mark.parametrize("audit_case", ["mismatched", "duplicate"])
def test_bootstrap_prepare_rejects_invalid_audit_identity_before_git(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_case: str,
) -> None:
    """Malformed durable authority fails before source or target probing."""
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
        payload = _bootstrap_event_payload(
            task_id=task_id,
            source_root=source_root,
            workspace=target,
            base_revision="a" * 40,
        )
        with kb.write_txn(conn):
            if audit_case == "mismatched":
                payload["approval"] = "different approval"
                kb._append_event(conn, task_id, "bootstrap_workspace_prepared", payload)
            else:
                kb._append_event(conn, task_id, "bootstrap_workspace_prepared", payload)
                kb._append_event(conn, task_id, "bootstrap_workspace_prepared", payload)

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


def test_bootstrap_prepare_ignores_replace_refs_for_checkout(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """A local replace ref cannot substitute the declared commit's tree."""
    source_root = _init_git_repo(tmp_path / "source")
    declared_revision = _git_output(source_root, "rev-parse", "HEAD")
    (source_root / "README.md").write_text("replacement content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source_root), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "commit", "-m", "replacement commit"],
        check=True,
        capture_output=True,
        text=True,
    )
    replacement_revision = _git_output(source_root, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(source_root), "update-ref", "refs/heads/main", declared_revision],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source_root), "replace", declared_revision, replacement_revision],
        check=True,
        capture_output=True,
        text=True,
    )
    target = tmp_path / "worktrees" / "repair"

    with kb.connect_closing() as conn:
        task_id = _create_self_repair_task(
            conn,
            source_root=source_root,
            workspace=target,
            base_revision=declared_revision,
        )
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert result.ok
    assert (target / "README.md").read_text(encoding="utf-8") == "bootstrap test\n"


def test_bootstrap_prepare_rejects_transplanted_worktree_admin_pointer(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """The target .git file must round-trip through its own registered admin."""
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

        other = tmp_path / "worktrees" / "other"
        subprocess.run(
            [
                "git",
                "-C",
                str(source_root),
                "worktree",
                "add",
                "-b",
                "codex/other-bootstrap",
                str(other),
                base_revision,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        other_git_dir = Path(_git_output(other, "rev-parse", "--absolute-git-dir"))
        (other_git_dir / "HEAD").write_text(
            "ref: refs/heads/codex/bootstrap-self-repair\n",
            encoding="utf-8",
        )
        (target / ".git").write_text(
            (other / ".git").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        second = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )

    assert not second.ok
    assert "pointer" in (second.reason or "")


def test_bootstrap_prepare_rejects_branch_drift_after_audit(
    kanban_home: Path,
    tmp_path: Path,
) -> None:
    """The registered branch remains part of exact idempotent identity."""
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
        subprocess.run(
            ["git", "-C", str(target), "branch", "-m", "codex/drifted-bootstrap"],
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

    assert not second.ok
    assert "branch" in (second.reason or "")


def test_bootstrap_prepare_leaves_workspace_on_audit_write_failure(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit failure is fail-closed and never triggers worktree cleanup."""
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
        real_append_event = kb._append_event

        def fail_prepared_event(
            connection: object,
            requested_id: str,
            kind: str,
            payload: object,
            **kwargs: object,
        ) -> object:
            if kind == "bootstrap_workspace_prepared":
                raise sqlite3.OperationalError("simulated audit failure")
            return real_append_event(connection, requested_id, kind, payload, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_prepared_event)
        result = kb.prepare_bootstrap_workspace(
            conn,
            task_id,
            actor="coordinator",
            approval="recorded single-use gate",
        )
        prepared_events = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "bootstrap_workspace_prepared"
        ]

    assert not result.ok
    assert "durable bootstrap audit" in (result.reason or "")
    assert target.is_dir()
    assert prepared_events == []
