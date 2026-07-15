"""Regression tests for worker-only host confinement (#43)."""

from __future__ import annotations

import json

import pytest

import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool


def _terminal_config(env_type: str) -> dict:
    return {
        "env_type": env_type,
        "cwd": "/untrusted-host-cwd",
        "timeout": 60,
        "lifetime_seconds": 3600,
        "docker_image": "test-image",
        "docker_volumes": ["/host/data:/data"],
        "docker_mount_cwd_to_workspace": False,
        "docker_network": True,
        "docker_extra_args": ["--privileged"],
        "docker_forward_env": ["UNTRUSTED_HOST_TOKEN"],
        "docker_env": {"UNTRUSTED_HOST_TOKEN": "should-not-enter-worker"},
        "docker_run_as_host_user": True,
        "docker_persistent": True,
        "docker_orphan_reaper": False,
    }


def _mark_kanban_worker(monkeypatch, workspace):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_confined")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_KANBAN_CONFINEMENT", "1")


def _allow_command(monkeypatch):
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})


def test_confined_worker_denies_local_terminal_before_environment_creation(monkeypatch, tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _mark_kanban_worker(monkeypatch, workspace)
    _allow_command(monkeypatch)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("local"))

    calls = []

    class FakeEnvironment:
        env = {}

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "unexpected", "returncode": 0}

    monkeypatch.setattr(terminal_tool, "_create_environment", lambda **kwargs: FakeEnvironment())

    result = json.loads(
        terminal_tool.terminal_tool(command="brew reinstall python@3.14", task_id="session")
    )

    assert result["status"] == "blocked"
    assert "isolated Docker" in result["error"]
    assert calls == []


@pytest.mark.parametrize("command", ["python -m venv .venv", "pip install pytest"])
def test_confined_worker_blocks_toolchain_recovery_inside_container(monkeypatch, tmp_path, command):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _mark_kanban_worker(monkeypatch, workspace)
    _allow_command(monkeypatch)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("docker"))

    calls = []

    class FakeEnvironment:
        env = {}

        def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return {"output": "unexpected", "returncode": 0}

    monkeypatch.setattr(terminal_tool, "_create_environment", lambda **kwargs: FakeEnvironment())

    result = json.loads(terminal_tool.terminal_tool(command=command, task_id="session"))

    assert result["status"] == "blocked"
    assert "toolchain recovery" in result["error"]
    assert calls == []


def test_confined_worker_replaces_host_exposing_docker_options(monkeypatch, tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _mark_kanban_worker(monkeypatch, workspace)
    _allow_command(monkeypatch)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("docker"))

    captured = {}

    class FakeEnvironment:
        env = {}
        cwd = "/workspace"

        def execute(self, command, **kwargs):
            captured["execute"] = kwargs
            return {"output": "ok", "returncode": 0}

    def fake_create_environment(**kwargs):
        captured["create"] = kwargs
        return FakeEnvironment()

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    result = json.loads(terminal_tool.terminal_tool(command="pwd", task_id="session"))

    assert result["exit_code"] == 0
    assert captured["create"]["cwd"] == "/workspace"
    assert captured["create"]["host_cwd"] == str(workspace.resolve())
    assert captured["create"]["task_id"] == "kanban-t_confined"
    assert captured["create"]["container_config"]["docker_mount_cwd_to_workspace"] is True
    assert captured["create"]["container_config"]["docker_mount_host_auxiliary"] is False
    assert captured["create"]["container_config"]["docker_network"] is False
    assert captured["create"]["container_config"]["docker_volumes"] == []
    assert captured["create"]["container_config"]["docker_extra_args"] == []
    assert captured["create"]["container_config"]["docker_forward_env"] == []
    assert captured["create"]["container_config"]["docker_env"] == {}
    assert captured["create"]["container_config"]["docker_run_as_host_user"] is False
    assert captured["create"]["container_config"]["docker_persistent"] is False


def test_confined_worker_file_backend_uses_the_same_docker_boundary(monkeypatch, tmp_path):
    """File tools must not create an unconfined backend before terminal runs."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _mark_kanban_worker(monkeypatch, workspace)
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("docker"))
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})

    captured = {}

    class FakeEnvironment:
        cwd = "/workspace"

    def fake_create_environment(**kwargs):
        captured["create"] = kwargs
        return FakeEnvironment()

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)

    file_tools._get_file_ops("session")

    assert captured["create"]["task_id"] == "kanban-t_confined"
    assert captured["create"]["cwd"] == "/workspace"
    assert captured["create"]["host_cwd"] == str(workspace.resolve())
    assert captured["create"]["container_config"]["docker_mount_cwd_to_workspace"] is True
    assert captured["create"]["container_config"]["docker_mount_host_auxiliary"] is False
    assert captured["create"]["container_config"]["docker_network"] is False
    assert captured["create"]["container_config"]["docker_volumes"] == []
    assert captured["create"]["container_config"]["docker_forward_env"] == []
    assert captured["create"]["container_config"]["docker_env"] == {}


def test_confined_worker_file_paths_use_the_container_workspace(monkeypatch, tmp_path):
    """A Docker file tool must resolve relative paths inside the sole mount."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _mark_kanban_worker(monkeypatch, workspace)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("docker"))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})

    resolved = file_tools._resolve_path_for_task("src/module.py", task_id="session")

    assert str(resolved) == "/workspace/src/module.py"


def test_confined_worker_container_file_path_rejects_host_symlink_escape(monkeypatch, tmp_path):
    """The lexical /workspace prefix cannot hide a host symlink escape."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    _mark_kanban_worker(monkeypatch, workspace)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: _terminal_config("docker"))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})

    with pytest.raises(ValueError, match="outside the declared Kanban workspace"):
        file_tools._resolve_path_for_task("escape/secret.txt", task_id="session")
