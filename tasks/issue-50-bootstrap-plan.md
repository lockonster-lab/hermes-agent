# Spec and implementation plan: #50 audited self-repair bootstrap

## Objective

Provide one offline, auditable control-plane operation for a deliberately
declared self-repair TaskContract. It prepares that task's exact linked Git
worktree only after validating the persisted source root, base revision,
workspace path, branch, manual gate, coordinator-only mode, and absence of
prior runs. It never claims, promotes, dispatches, or executes a worker.

## Assumptions and boundaries

- The caller is the local coordinator already authorized by a recorded
  TaskContract gate; the code makes that authorization narrow and auditable,
  rather than treating a CLI invocation as a generic host fallback.
- Source and worktree paths are local, canonical absolute paths. No network, Docker,
  package, provider, credential, raw-data, client-repository, cleanup, push,
  PR, merge, or rebase operation is in scope.
- Existing `tasks/plan.md` and `tasks/todo.md` document #43 and are preserved.
  This task-specific plan is the live specification for #50v2.

## Contract

`self_repair` is the only bootstrap kind. A task may declare immutable
bootstrap metadata at creation only when it is initially `blocked` and
`coordinator_only`, has an absolute `worktree` path and branch, and supplies
an absolute source root plus a full commit revision. Task JSON exposes the
declaration.

`hermes kanban bootstrap-prepare <task-id> <approval> --json` has no path,
branch, or revision arguments. It reads only the persisted declaration and
fails closed unless all of the following hold:

1. the task is still blocked, manual-gated, coordinator-only, and run-free;
2. its kind is `self_repair` and every bootstrap field is present;
3. the declared source root is a Git top-level whose current `HEAD` equals
   the declared base revision;
4. the declared workspace is either absent or exactly once registered as the
   matching linked worktree, with matching Git/admin backpointers, branch and
   `HEAD`; and
5. sanitized Git config contains no executable checkout filter and Git can
   create precisely that worktree from the declared base without replace
   objects, lazy fetch, hooks, fsmonitor or submodule recursion.

The first successful preparation revalidates TaskContract, source and target
identity at the durable boundary, then appends one
`bootstrap_workspace_prepared` event including the task-bound identity and
approval. A matching repeat is idempotent and does not widen authority. Every
denial occurs before any Git worktree mutation.

## Project structure and commands

- `hermes_cli/kanban_db.py` owns persisted metadata, validation, Git identity
  checks, worktree preparation, and audit events.
- `hermes_cli/kanban.py` owns argument parsing and JSON output only.
- `tests/hermes_cli/test_kanban_bootstrap.py` covers the stateful executor
  contract; `tests/hermes_cli/test_kanban_cli.py` covers CLI/JSON.

Run focused checks with:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_bootstrap.py tests/hermes_cli/test_kanban_cli.py tests/hermes_cli/test_kanban_db.py -q
```

The test runner pre-compiles Python files. No dependency installation or
external call is permitted.

## Ordered slices

1. Commit this spec and task list only.
2. Add RED tests for immutable declaration validation and rejection of an
   ordinary coordinator-only task before any subprocess.
3. Add the smallest schema/model/create validation needed for the immutable
   declaration; run the RED tests to GREEN and commit.
4. Add `bootstrap-prepare`, exact Git/worktree validation, event emission,
   idempotence, and CLI JSON; run focused tests and commit.
5. Run security and five-axis code review, then record a separate local-only
   integration gate. A later #49 successor may consume the supported path.

## Threat model and success criteria

The trust boundary is local coordinator input, the declared source repository,
and persisted TaskContract metadata. Abuse cases are: preparing another task's
workspace, substituting a branch/base/path after approval, inherited Git
environment redirection, checkout filters, replace refs or lazy fetch, using a
ready/worker/running task, replaying after a run, and adopting a foreign or
transplanted checkout. Mitigations are creation-time canonicalization,
immutable public setters, allowlisted Git environment, runtime exact equality
and registration/backpointer checks, final revalidation, parameterized SQL, a
durable audit event, idempotence, and subprocess sentinels in denial tests.

SQLite and the Git filesystem cannot form one atomic transaction. Final Git
revalidation under the immediate database transaction establishes the durable
point-in-time boundary; an unrelated same-user process that deliberately
ignores Hermes coordination remains a documented local residual.

Success means all acceptance checks above are covered by offline tests, the
default worker lifecycle is unchanged, and no permitted input can become a
generic local or worker execution path.
