"""Tests: kanban worker spawn pins TERMINAL_CWD to the task workspace.

Regression coverage for #34619 and #41312 (same root cause): ``_default_spawn``
launched the worker subprocess with ``cwd=workspace`` and set
``HERMES_KANBAN_WORKSPACE``, but did NOT set ``TERMINAL_CWD``. Because
``TERMINAL_CWD`` takes precedence over the process cwd in both
``tools/file_tools.py::_resolve_base_dir`` (relative ``write_file`` paths) and
``agent_init``'s context-file loader (``AGENTS.md`` discovery), workers inherited
the dispatching gateway's cwd — relative writes landed in the gateway user's
home (#41312) and the wrong profile's ``AGENTS.md`` was loaded (#34619).
Pinning ``TERMINAL_CWD`` to the workspace fixes both.
"""

from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_cwd",
        title="cwd pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _capture_spawn_env(kb, monkeypatch, workspace: str) -> dict:
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_preflight_confined_worker_docker_image", lambda _image: None)
    kb._default_spawn(_make_task(kb), workspace)
    return captured


def test_terminal_cwd_pinned_to_workspace(monkeypatch, tmp_path):
    """A real, absolute workspace dir is pinned as TERMINAL_CWD."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))

    canonical_workspace = str(workspace.resolve())
    assert captured["env"]["TERMINAL_CWD"] == canonical_workspace
    # The subprocess cwd and TERMINAL_CWD must agree — both anchor the workspace.
    assert captured["cwd"] == canonical_workspace
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == canonical_workspace
    assert captured["env"]["HERMES_KANBAN_CONFINEMENT"] == "1"
    assert captured["env"]["TERMINAL_ENV"] == "docker"


def test_worker_spawn_rejects_nonexistent_workspace_before_popen(monkeypatch, tmp_path):
    """A worker must fail closed instead of inheriting a host working directory."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli import kanban_db as kb

    missing = tmp_path / "does-not-exist"

    import pytest

    with pytest.raises(ValueError, match="workspace"):
        _capture_spawn_env(kb, monkeypatch, str(missing))


def test_worker_spawn_canonicalizes_a_workspace_symlink(monkeypatch, tmp_path):
    """The child sees the real worktree root, never an attacker-controlled alias."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)

    captured = _capture_spawn_env(kb, monkeypatch, str(alias))

    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace.resolve())
