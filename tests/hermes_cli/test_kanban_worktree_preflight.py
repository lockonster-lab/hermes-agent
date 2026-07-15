"""Fail-closed lifecycle admission for declared Git worktrees (#31)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli import kanban as kc


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    (source / "README").write_text("source\n")
    _git(source, "add", "README")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    target = tmp_path / "target"
    _git(source, "worktree", "add", "-b", "wt/preflight", str(target), base)
    return source, target, base


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def test_promote_refuses_missing_declared_worktree_before_ready(
    kanban_home, tmp_path,
) -> None:
    """A declared target must exist before a blocked task becomes ready."""
    missing = tmp_path / "missing-linked-worktree"
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="guarded worktree release",
            workspace_kind="worktree",
            workspace_path=str(missing),
            branch_name="wt/guarded-release",
            initial_status="blocked",
        )

        ok, reason = kb.promote_task(conn, task_id, actor="coordinator")
        task = kb.get_task(conn, task_id)

    assert not ok
    assert reason == "declared worktree is missing"
    assert task is not None
    assert task.status == "blocked"


def test_claim_refuses_ready_task_with_missing_declared_worktree(
    kanban_home, tmp_path,
) -> None:
    """No claim/run can precede worktree preflight for a ready task."""
    missing = tmp_path / "missing-linked-worktree"
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="guarded worktree claim",
            workspace_kind="worktree",
            workspace_path=str(missing),
            branch_name="wt/guarded-claim",
        )

        claimed = kb.claim_task(conn, task_id, claimer="preflight-test")
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert claimed is None
    assert task is not None
    assert task.status == "ready"
    assert runs == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("branch", "declared worktree branch does not match"),
        ("head", "declared worktree base revision does not match"),
        ("dirty", "declared worktree is dirty"),
    ],
)
def test_preflight_refuses_attestation_drift(
    kanban_home, tmp_path: Path, mutation: str, expected: str,
) -> None:
    source, target, base = _linked_worktree(tmp_path)
    if mutation == "branch":
        _git(target, "checkout", "-b", "wt/drift")
    elif mutation == "head":
        (target / "README").write_text("changed\n")
        _git(target, "add", "README")
        _git(target, "commit", "-m", "drift")
    else:
        (target / "dirty").write_text("x")
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="attested", workspace_kind="worktree", workspace_path=str(target), branch_name="wt/preflight", worktree_source_root=str(source), worktree_base_revision=base)
        ok, reason, workspace = kb.preflight_declared_worktree(conn, task_id)
    assert not ok
    assert reason == expected
    assert workspace == str(target)


def test_preflight_returns_canonical_verified_worktree_path(
    kanban_home, tmp_path: Path,
) -> None:
    """A valid symlink declaration reports the path actually validated."""
    source, target, base = _linked_worktree(tmp_path)
    alias = tmp_path / "declared-alias"
    alias.symlink_to(target, target_is_directory=True)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="canonical preflight",
            workspace_kind="worktree",
            workspace_path=str(alias),
            branch_name="wt/preflight",
            worktree_source_root=str(source),
            worktree_base_revision=base,
        )
        ok, reason, workspace = kb.preflight_declared_worktree(conn, task_id)

    assert ok
    assert reason is None
    assert workspace == str(target.resolve())


@pytest.mark.parametrize("status", ["ready", "review"])
def test_dry_run_skips_invalid_worktree_before_reporting_spawnable(
    kanban_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    """Dry-run is non-mutating but must not advertise an invalid target."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    missing = tmp_path / "missing"
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="invalid dry-run target",
            assignee="test-profile",
            workspace_kind="worktree",
            workspace_path=str(missing),
            branch_name="wt/missing",
        )
        if status == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        result = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task_id in result.skipped_worktree_preflight
    assert result.spawned == []
    assert task is not None and task.status == status
    assert runs == []


@pytest.mark.parametrize("status", ["ready", "review"])
def test_dry_run_reports_canonical_valid_worktree_without_mutation(
    kanban_home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    """Both dispatch queues expose only the exact worktree they verified."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _: True)
    source, target, base = _linked_worktree(tmp_path)
    alias = tmp_path / "declared-alias"
    alias.symlink_to(target, target_is_directory=True)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="valid dry-run target",
            assignee="test-profile",
            workspace_kind="worktree",
            workspace_path=str(alias),
            branch_name="wt/preflight",
            worktree_source_root=str(source),
            worktree_base_revision=base,
        )
        if status == "review":
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        result = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert result.skipped_worktree_preflight == []
    assert result.spawned == [(task_id, "test-profile", str(target.resolve()))]
    assert task is not None and task.status == status
    assert task.worker_pid is None
    assert runs == []


def test_lifecycle_bypasses_and_identity_setters_reject_invalid_worktree(
    kanban_home, tmp_path: Path,
) -> None:
    """Every release path keeps a drifted declared worktree non-runnable."""
    source, target, base = _linked_worktree(tmp_path)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="all admission paths",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name="wt/preflight",
            worktree_source_root=str(source),
            worktree_base_revision=base,
        )
        _git(target, "checkout", "-b", "wt/drift")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        assert kb.claim_review_task(conn, task_id, claimer="preflight-test") is None
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        assert not kb.unblock_task(conn, task_id)
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        assert kb.recompute_ready(conn) == 0
        with pytest.raises(ValueError, match="identity is immutable"):
            kb.set_workspace_path(conn, task_id, tmp_path / "other")
        with pytest.raises(ValueError, match="identity is immutable"):
            kb.set_branch_name(conn, task_id, "wt/other")
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert task is not None and task.status == "todo"
    assert runs == []


def test_worktree_preflight_command_reports_verified_path_without_mutation(
    kanban_home, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI exit status and JSON use the canonical admitted identity."""
    source, target, base = _linked_worktree(tmp_path)
    alias = tmp_path / "declared-alias"
    alias.symlink_to(target, target_is_directory=True)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="CLI canonical preflight",
            workspace_kind="worktree",
            workspace_path=str(alias),
            branch_name="wt/preflight",
            worktree_source_root=str(source),
            worktree_base_revision=base,
        )
        before = kb.get_task(conn, task_id)

    exit_code = kc._cmd_worktree_preflight(
        argparse.Namespace(task_id=task_id, json=True)
    )
    payload = json.loads(capsys.readouterr().out)

    with kb.connect_closing() as conn:
        after = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert exit_code == 0
    assert payload == {
        "task_id": task_id,
        "ok": True,
        "reason": None,
        "workspace_path": str(target.resolve()),
    }
    assert after is not None and before is not None
    assert after.status == before.status == "ready"
    assert runs == []
