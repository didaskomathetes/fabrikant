You are performing a targeted refactoring in the `fabrikant` project (repo `didaskomathetes/fabrikant`).

## Preparation (mandatory)
1. Follow ALL rules from `AGENTS.md` (already in your context), especially the
   boy-scout rule: leave the codebase cleaner than you found it, but keep the scope limited.
2. Read the affected file(s) and their tests in `tests/`.

## Target
{target}

## Rules for this refactoring
- **Behavior stays 100% unchanged.** Refactoring ≠ feature work. If you notice
  you want to change features: STOP, only clarify structure/logic, no new capabilities.
- Only change files in this directory (the repo's `fabrikant/` package).
- These commands must be green before and after:
  - `uv run pytest -m "not browser"`
  - `uv run ruff format src/ tests/ scripts/`
  - `bash scripts/lint.sh` (ruff, pyright, bandit)
- Goal: reduce complexity, remove duplication, clearer names, add docs — as far as it
  concerns the target.
- Fabrikant's `git` step handles commit and push — you must NOT commit or push.
- At the end, answer in 2-3 sentences describing what was refactored and why.
