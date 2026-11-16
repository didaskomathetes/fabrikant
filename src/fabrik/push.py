"""CLI für Agents: aktuellen Branch pushen — per `git push` oder als signierte API-Commits (`commit_via`).

Eigenes Modul, weil `fabrik.apicommit` beim Paket-Import schon geladen ist (`python -m` würde warnen)::

    python -m fabrik.push [--force] [--repo owner/name]
"""

import sys

from .apicommit import main

if __name__ == "__main__":
    sys.exit(main())
