"""Fail-closed lifecycle admission for declared Git worktrees (#31)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import hermes_cli.kanban_db as kb


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
