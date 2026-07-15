# #50v2 task list

## Slice 1 — specification

- [x] Preserve #43 planning files and add this task-specific specification.
  - Acceptance: the policy, boundaries, and verification commands are explicit.
  - Verify: inspect the diff; `git diff --check`.
  - Files: `tasks/issue-50-bootstrap-plan.md`, this file.

## Slice 2 — immutable declaration

- [x] Add failing tests for declaration validation and immutable JSON fields.
  - Acceptance: ordinary worker/coordinator tasks cannot claim bootstrap
    authority, and incomplete/path-relative declarations are rejected.
  - Verify: focused test is RED before implementation, then GREEN.
  - Files: `tests/hermes_cli/test_kanban_bootstrap.py`,
    `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`.

## Slice 3 — exact offline preparation

- [x] Implement task-bound `bootstrap-prepare` and a durable audit event.
  - Acceptance: it validates blocked/manual/coordinator/no-run state, exact
    source `HEAD`, base, worktree and branch; it is idempotent; every denial
    precedes Git worktree mutation.
  - Verify: Popen/subprocess sentinel tests plus focused test command.
  - Files: `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`,
    `tests/hermes_cli/test_kanban_bootstrap.py`,
    `tests/hermes_cli/test_kanban_cli.py`.

## Slice 4 — review and local integration

- [ ] Review correctness, readability, architecture, security, and
  performance; then record an explicit local-only integration gate.
  - Acceptance: no generic fallback, no added dependency, no secret/external
    I/O, and legacy worker behavior preserved.
  - Verify: diff/secrets checks and the focused suite.
  - Files: review only; no unrelated cleanup.
