"""Regression coverage for fail-closed declared worktree admission (#31)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import hermes_cli.kanban_db as kb


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test User")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    target = tmp_path / "task-worktree"
    _git(source, "worktree", "add", "-b", "agent/task", str(target), base)
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


def _create_attested_task(
    conn,
    source: Path,
    target: Path,
    base: str,
    *,
    initial_status: str = "blocked",
) -> str:
    return kb.create_task(
        conn,
        title="guarded worktree",
        workspace_kind="worktree",
        workspace_path=str(target),
        branch_name="agent/task",
        worktree_source_root=str(source),
        worktree_base_revision=base,
        initial_status=initial_status,
    )


def test_promote_refuses_missing_declared_worktree_without_creating_a_run(
    kanban_home: Path, tmp_path: Path,
) -> None:
    source, _target, base = _linked_worktree(tmp_path)
    missing = tmp_path / "missing"
    with kb.connect_closing() as conn:
        task_id = _create_attested_task(conn, source, missing, base)
        ok, reason = kb.promote_task(conn, task_id, actor="coordinator")
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)

    assert not ok
    assert reason == "declared worktree is missing"
    assert task is not None and task.status == "blocked"
    assert runs == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("branch", "declared worktree branch does not match"),
        ("head", "declared worktree base revision does not match"),
        ("dirty", "declared worktree is dirty"),
    ],
)
def test_initial_preflight_refuses_declared_worktree_drift(
    kanban_home: Path, tmp_path: Path, mutation: str, expected: str,
) -> None:
    source, target, base = _linked_worktree(tmp_path)
    if mutation == "branch":
        _git(target, "checkout", "-b", "agent/drift")
    elif mutation == "head":
        (target / "README.md").write_text("changed\n", encoding="utf-8")
        _git(target, "add", "README.md")
        _git(target, "commit", "-m", "drift")
    else:
        (target / "dirty.txt").write_text("x", encoding="utf-8")

    with kb.connect_closing() as conn:
        task_id = _create_attested_task(conn, source, target, base)
        ok, reason, workspace = kb.preflight_declared_worktree(conn, task_id)

    assert not ok
    assert reason == expected
    assert workspace == str(target)


def test_preflight_rejects_worktree_from_another_repository(
    kanban_home: Path, tmp_path: Path,
) -> None:
    source, _target, base = _linked_worktree(tmp_path)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign, foreign_target, _foreign_base = _linked_worktree(foreign_root)
    del foreign

    with kb.connect_closing() as conn:
        task_id = _create_attested_task(conn, source, foreign_target, base)
        ok, reason, _workspace = kb.preflight_declared_worktree(conn, task_id)

    assert not ok
    assert reason == "declared worktree is not owned by declared source"


def test_initial_preflight_allows_source_head_to_advance_after_task_creation(
    kanban_home: Path, tmp_path: Path,
) -> None:
    source, target, base = _linked_worktree(tmp_path)
    (source / "SOURCE.md").write_text("new main commit\n", encoding="utf-8")
    _git(source, "add", "SOURCE.md")
    _git(source, "commit", "-m", "advance source")

    with kb.connect_closing() as conn:
        task_id = _create_attested_task(conn, source, target, base)
        ok, reason, workspace = kb.preflight_declared_worktree(conn, task_id)

    assert ok
    assert reason is None
    assert workspace == str(target.resolve())


def test_review_claim_allows_clean_task_commits_after_the_attested_base(
    kanban_home: Path, tmp_path: Path,
) -> None:
    source, target, base = _linked_worktree(tmp_path)
    (target / "README.md").write_text("task result\n", encoding="utf-8")
    _git(target, "add", "README.md")
    _git(target, "commit", "-m", "task result")

    with kb.connect_closing() as conn:
        task_id = _create_attested_task(conn, source, target, base)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
        runs = kb.list_runs(conn, task_id)

    assert claimed is not None
    assert claimed.status == "running"
    assert len(runs) == 1


def test_dispatch_dry_run_reports_missing_attested_worktree_without_spawning(
    kanban_home: Path, tmp_path: Path, all_assignees_spawnable,
) -> None:
    source, _target, base = _linked_worktree(tmp_path)
    missing = tmp_path / "missing"
    with kb.connect_closing() as conn:
        task_id = _create_attested_task(
            conn, source, missing, base, initial_status="running"
        )
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("worker", task_id))
        result = kb.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert (task_id, "declared worktree is missing") in result.skipped_worktree_preflight
    assert task is not None and task.status == "ready"
    assert not missing.exists()


def test_dispatch_uses_existing_attested_worktree_without_reprovisioning(
    kanban_home: Path, tmp_path: Path, all_assignees_spawnable,
) -> None:
    source, target, base = _linked_worktree(tmp_path)
    calls: list[Path] = []

    def spawn(_task, workspace: str) -> int:
        calls.append(Path(workspace))
        return 0

    with kb.connect_closing() as conn:
        task_id = _create_attested_task(
            conn, source, target, base, initial_status="running"
        )
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("worker", task_id))
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)

    assert result.spawned == [(task_id, "worker", str(target.resolve()))]
    assert calls == [target.resolve()]
    assert task is not None and task.workspace_path == str(target)
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 2
