# Releasing fabrikant

Releases are tag-driven (`.github/workflows/release.yml`); see ADR 0069 and ADR 0070.

## One-time setup

1. **PyPI project + Trusted Publisher.** On pypi.org create the project `fabrikant` (or add a *pending*
   publisher): owner = the GitHub owner, repository = `fabrikant`, workflow = `release.yml`,
   environment = `pypi`. No API token is stored anywhere.
2. **GitHub environment** `pypi` (Settings → Environments); optionally require a reviewer.
3. **Rename the GitHub repository** `fabrik` → `fabrikant` (GitHub redirects the old URL) so the
   `project.urls` in `pyproject.toml` resolve.
4. Optionally publish a `0.0.1` placeholder first to reserve the name.

## Per release

1. Bump `version` in `pyproject.toml`, merge to `main`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow checks tag == version, builds, runs
   `twine check` and the wheel smoke test (`scripts/smoke-wheel.sh`), publishes to PyPI and pushes
   `ghcr.io/<owner>/fabrikant:{X.Y.Z,latest}`.
3. Verify in a clean venv: `pip install fabrikant==X.Y.Z && fabrikant init demo`.

Do not tag before the rename stack (ADR 0070) is merged: the first PyPI upload must already carry
the final import and CLI names.
