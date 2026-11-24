# Operations

## Local operation (systemd user units)

Permanent operation runs as a user service instead of a foreground `fabrikant serve`, so that scheduler/cron
triggers survive closing the terminal and reboots. Only one `serve` process at a time
([single node](runs.md)). The units below are for a source checkout; for a pip install see
[plugins.md](plugins.md#install) and the Docker image.

The units in `deploy/` contain no checkout paths. They read `FABRIKANT_HOME` (the root of the checkout, same
meaning as in `config.py`) from **one** file per machine and switch there via
`/bin/sh -c 'cd "$FABRIKANT_HOME" && exec ...'` (systemd doesn't expand variables in `WorkingDirectory=`). If the
variable is missing, the unit aborts with "FABRIKANT_HOME missing".

```bash
# ~/.config/fabrikant/deploy.env — create once per machine, from the repo root
mkdir -p ~/.config/fabrikant
echo "FABRIKANT_HOME=$PWD" > ~/.config/fabrikant/deploy.env
# example content: FABRIKANT_HOME=/home/<user>/src/fabrikant
```

```bash
cp deploy/fabrikant.service deploy/fabrikant-autoupdate.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fabrikant.service fabrikant-autoupdate.timer
journalctl --user -u fabrikant -u fabrikant-autoupdate -f      # logs
```

systemd starts without a shell profile: agent CLIs outside `/usr/bin` (`copilot` via nvm, `claude`, `opencode`)
are otherwise missing from `PATH` → agent steps fail with "CLI '...' not in PATH". Set it per machine via a
drop-in:

```ini
# ~/.config/systemd/user/fabrikant.service.d/path.conf
[Service]
Environment=PATH=%h/.nvm/versions/node/<version>/bin:%h/.local/bin:%h/.opencode/bin:/usr/local/bin:/usr/bin:/bin
```

Don't forget `enable`, or nothing starts after a reboot (`systemctl --user is-enabled fabrikant`); without a
login you also need `loginctl enable-linger`.

**Auto-update after merges** (`scripts/fabrikant-autoupdate.py`, timer every 5 min): pulls `main` from `origin`
with `--ff-only` (only on branch `main`; with local commits/conflicts it aborts with exit 1, nothing is
overwritten), runs `uv sync` if `pyproject.toml`/`uv.lock` changed and restarts `fabrikant.service` if anything
in the checkout has changed since the start commit (`state/serve-commit`, written via `ExecStartPre`). Restarts
happen only when idle — if `running`/`queued` runs exist, it is deferred to the next tick (a restart would orphan
them as `error`/`interrupted`; auto-retry kicks in if configured, ADR 0036). `needs_approval` runs don't block.

GitHub App token refresh units (`deploy/gh-token-refresh*.{service,timer}`): see
[configuration.md](configuration.md#github-identity-and-commits).

## Worker pool

Trigger runs execute on a bounded pool of daemon workers (`FABRIKANT_WORKERS`, default 4). For local development
/ initial testing: `FABRIKANT_WORKERS=1` → all runs serially on a single agent worker (no parallel LLM
sessions). Daemon threads: Ctrl+C (or SIGTERM/SIGHUP) ends the process immediately without waiting for running
runs; running shell steps and one-shot agents are terminated together with their process group.

## Parallel runs and worktrees (ADR 0028)

Each run works in its own git worktree (`<WORKTREES_DIR>/<project>/<run_id>`; `FABRIKANT_WORKTREES`, default
`~/.local/state/fabrikant/worktrees/<installation>` — deliberately outside the checkout so IDEs don't index the
run checkouts, ADR 0047 addendum), which is cleaned up at the end (or on reject, and after a crash on the next
server start); a run in `needs_approval` keeps it for the resume. `max_parallel` (project spec, default 3) limits
concurrent runs per project; specs without `repo:` run without isolation in `root` and are therefore fixed at
`max_parallel: 1`. Trigger runs over the limit are dropped, manual/chat runs and approved resumes are queued
(ADR 0015) and wait for a free slot. `worktree: false` loops (ADR 0020) don't occupy a slot. At most one active
run runs per loop + issue/PR. `state/replied-comments.json` and `state/reviewed-prs.json` are merged under lock
into `state/agent/<project>/`. Operation: exactly **one** fabrikant process per project (see ADR 0028). True
parallelism needs `FABRIKANT_WORKERS` > 1.

## One-off: label migration (`ready to develop` → `ready for development`)

Relabel existing issues once (needs `gh`, create the label first):

```bash
gh label create "ready for development" --repo OWNER/REPO || true
for n in $(gh issue list --repo OWNER/REPO --label "ready to develop" --state all --limit 500 --json number -q '.[].number'); do
  gh issue edit "$n" --repo OWNER/REPO --add-label "ready for development" --remove-label "ready to develop"
done
gh label delete "ready to develop" --repo OWNER/REPO --yes   # optional, afterwards
```
