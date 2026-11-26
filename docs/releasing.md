# Releasing fabrikant

Releases are tag-driven (`.github/workflows/release.yml`); see ADR 0069 and ADR 0070.

## Prerequisites

Publishing uses PyPI Trusted Publishing (OIDC, no stored token) from the `pypi` GitHub environment, and the
container image goes to GHCR. The maintainers configure both once; contributors don't need to.

## Per release

1. Bump `version` in `pyproject.toml`, merge to `main`.
2. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow checks tag == version, builds, runs
   `twine check` and the wheel smoke test (`scripts/smoke-wheel.sh`), publishes to PyPI and pushes
   `ghcr.io/didaskomathetes/fabrikant:{X.Y.Z,latest}`.
3. Verify in a clean venv: `pip install fabrikant==X.Y.Z && fabrikant init demo`.

## PyPI project page

The PyPI description is the README, rewritten at build time by `hatch-fancy-pypi-readme` (see `pyproject.toml`):
the Mermaid diagram becomes `docs/images/architecture.png` and repo-relative links become absolute GitHub links.
PyPI only picks up README changes with a new release. After editing the diagram, run
`bash scripts/render-architecture.sh` and commit the PNG.
