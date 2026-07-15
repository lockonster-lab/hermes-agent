# Implementation Plan: P0 worker workspace confinement

## Objective

Prevent a Hermes Kanban worker from mutating host paths or recovering tooling
outside its declared worktree.  The worker must fail closed when it cannot use
an isolated, air-gapped container that mounts only that worktree.

## Architecture decisions

- Treat `cwd`, `TERMINAL_CWD`, and `HERMES_KANBAN_WORKSPACE` as routing hints,
  not as a security boundary.
- Make `HERMES_KANBAN_CONFINEMENT=1` a dispatcher-owned worker capability
  marker.  Only `_default_spawn` sets it.
- In a marked session, refuse the local terminal backend.  Docker is the sole
  supported execution path because it can receive a per-task host mount and
  disabled network; unavailable Docker is a blocker, never a host fallback.
- Canonicalize file targets and reject all host and container-path file-tool
  escapes outside the canonical worker workspace, including symlink escapes.
- Suppress Docker's ordinary credential, skill, and cache mounts for marked
  workers; their sole host mount is the declared worktree at `/workspace`.
- Pin an allow-list of `terminal`, `file`, and `skills` toolsets so browser,
  web, code-execution, and delegation paths cannot bypass the air-gapped
  execution backend.

## Ordered tasks

1. Add failing regression tests for local-terminal denial, Docker confinement,
   canonical workspace propagation, and file/symlink escape rejection.
2. Add the smallest dispatcher and terminal enforcement that makes the
   terminal tests pass.
3. Add file-tool canonical-path enforcement, including file-backend creation,
   and make the remaining tests pass.
4. Prevent automatic auxiliary Docker mounts for confined workers and close
   the profile toolset/network bypasses.
5. Run focused tests with `scripts/run_tests.sh`, inspect the diff for scope and
   secrets, then run the relevant broader suite and independent review.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Existing worker uses a local terminal | Fail closed with a bounded blocker; do not silently downgrade confinement. |
| Configured Docker options expose host or network | Replace host mounts, extra arguments, and network settings for marked workers. |
| Docker auto-mounts credentials, skills, or caches | Pass an explicit no-auxiliary-mount flag through the backend. |
| Symlink bypass in file tools | Compare fully resolved target to fully resolved workspace root. |
| Regression outside Kanban | Activate all new behavior only when the dispatcher-owned marker is present. |

## Success criteria

- The acceptance criteria of GitHub #43 pass in focused tests.
- No tests or commands mutate package managers, interpreters, network state, or
  user worktrees.
- #25 remains blocked until review accepts this prerequisite.
