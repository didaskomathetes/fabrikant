"""Relay tasks (ADR 0037): long-running tasks as a chain of short agent sessions.

A GitHub issue (label `fabrikant-relay`) is the assignment, a handoff file on the branch
`fabrikant/relay/<N>` the memory between the sessions. Projects enable the feature with a
`relay:` block in the spec (`config.expand_loops`); logic in `model`, I/O in `github`,
`workspace` and `commands`.
"""
