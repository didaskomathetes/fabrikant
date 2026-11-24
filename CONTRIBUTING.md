# Contributing

Thanks for your interest in fabrikant! Setup, tests and the lint gate are described in
[docs/development.md](docs/development.md); the conventions we follow are in [AGENTS.md](AGENTS.md).

In short:

- Open an issue or discussion before large changes; significant design decisions are recorded as ADRs in
  [docs/adr/](docs/adr/README.md).
- Keep changes small and tested (`scripts/lint.sh` and `pytest` must pass; the coverage gate is 90%).
- Write docs, comments, commit messages and PR descriptions in English.
- Step plugins live in their own packages: see [docs/plugins.md](docs/plugins.md).

By contributing you agree that your contribution is licensed under the Apache License 2.0 ([LICENSE](LICENSE)).
