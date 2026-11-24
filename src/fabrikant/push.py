"""CLI for agents: push the current branch — via `git push` or as signed API commits (`commit_via`).

Own module, because `fabrikant.apicommit` is already loaded at package import (`python -m` would warn)::

    python -m fabrikant.push [--force] [--repo owner/name]
"""

import sys

from .apicommit import main

if __name__ == "__main__":
    sys.exit(main())
