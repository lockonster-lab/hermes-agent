# P0 #43 checklist

- [x] Write red regression tests for terminal, file backend, toolsets, and Docker mounts.
- [x] Verify the new regressions fail before each corrective slice.
- [x] Implement dispatcher-owned confinement marker and canonical workspace.
- [x] Implement fail-closed isolated terminal and file-backend configuration.
- [x] Implement canonical host/container file-path containment and symlink checks.
- [x] Disable automatic auxiliary Docker mounts and network-capable toolsets.
- [x] Run focused `scripts/run_tests.sh` checks (350 passed).
- [x] Attempt broader suite; record independent async-plugin environment blocker as #44.
- [ ] Complete final diff review, local integration, and evidence updates in Hermes task `t_73ec4bfa` and GitHub #43.
