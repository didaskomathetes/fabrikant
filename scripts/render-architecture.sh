#!/usr/bin/env bash
# Render the README's Mermaid architecture diagram to docs/images/architecture.png, the image the PyPI long
# description shows instead (PyPI does not render Mermaid; see [tool.hatch.metadata.hooks.fancy-pypi-readme]
# in pyproject.toml). Re-run after editing the diagram. Needs Node (npx); usage: bash scripts/render-architecture.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MERMAID_CLI="@mermaid-js/mermaid-cli@11.4.2"
OUT="docs/images/architecture.png"

src="$(mktemp --suffix=.mmd)"
trap 'rm -f "$src"' EXIT
awk '/^```mermaid/{f=1;next} f&&/^```/{exit} f' README.md > "$src"
[[ -s "$src" ]] || { echo "no mermaid block found in README.md" >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
npx --yes "$MERMAID_CLI" -i "$src" -o "$OUT" --scale 2 --backgroundColor white
echo "wrote $OUT"
