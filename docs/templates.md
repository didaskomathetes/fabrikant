# Template Library

Curated, documented, versioned loop templates for fabrikant (ADR 0067). A template
is an [ADR 0047](../adr/0047-spec-sources-vars-templates.md) file in
`src/fabrikant/templates/` (`params:` + `loop:`) that a spec can instantiate with
`use:`/`with:`.

**Resolution order** (first hit wins): the user dir (`FABRIKANT_DATA/templates`)
overrides the bundled dir (the package's `templates/` folder) — same order as the
engine.

**Discover & scaffold:**

```bash
fabrikant template list                # all templates + required parameters
fabrikant template create <name>       # scaffold <name>.fabrikant.yaml into the projects dir
fabrikant template create hotfix --project myapi --out ~/specs --force
```

`scaffold` writes a spec with one loop instance of the template; required
parameters (`null` defaults) come out as `param: null  # TODO`. Fill them in,
then `fabrikant validate`.

## The templates

| Template | Use case | Required params | Merges automatically? |
|---|---|---|---|
| `develop` | issue → PR → cross-model review → merge (dual identity, ADR 0010) | `repo`, `prompt_file`, `lint_cmd`, `test_cmd`, `builder_model`, `reviewer_model` | yes (after `human_gate` for critical changes) |
| `review-pr` | cross-model review of labeled PRs (ADR 0003) | `repo`, `prompt_file`, `reviewer_model` | n/a (no merge step) |
| `docs` | documentation issues → PR | `repo`, `prompt_file`, `builder_model` | **no** (no merge step; PR creation needs approval) |
| `hotfix` | fast fix onto a release branch | `repo`, `prompt_file`, `lint_cmd`, `test_cmd`, `builder_model`, `reviewer_model`, `base` | yes (after `human_gate`, always) |
| `dogfood` | minimal self-hosting loop (ADR 0001 on-ramp) | `repo`, `prompt_file`, `test_cmd`, `builder_model` | **no** (no merge step; PR creation needs approval) |
| `escalation` | `needs human` items → waiting gate run (ADR 0059) | `repo` | n/a |

Every template is covered by `tests/test_templates.py`, which expands it with
sample parameters and validates the result against the spec schema.

## Examples

### docs — documentation PRs

```yaml
name: mydocs
description: "Docs PRs from labeled issues"
root: ~/repos/mydocs
loops:
  docs:
    use: docs
    with:
      repo: owner/mydocs
      prompt_file: prompts/docs.prompt.md
      builder_model: llama/my-model
      verify_cmd: bash scripts/check-links.sh   # optional; default "true" = no-op
      issue_label: docs                          # issues with this label
```

### hotfix — fast path onto a release branch

```yaml
name: myapi
description: "Hotfix pipeline for the 1.x line"
root: ~/repos/myapi
loops:
  hotfix:
    use: hotfix
    with:
      repo: owner/myapi
      prompt_file: prompts/hotfix.prompt.md
      lint_cmd: ruff check .
      test_cmd: pytest -q
      builder_model: llama/my-model
      reviewer_model: llama/other-model
      base: release/1.0                          # required — release branch
```

Hotfixes cut ahead of dependency gates (`skip_blocked: false`) and always pass
a `human_gate` before the squash merge.

### dogfood — the on-ramp for new projects

```yaml
name: myproj
description: "fabrikant on myproj"
root: ~/repos/myproj
loops:
  dogfood:
    use: dogfood
    with:
      repo: owner/myproj
      prompt_file: prompts/build.prompt.md
      test_cmd: pytest -q
      builder_model: llama/my-model
      issue_label: fabrikant
```

Deliberately minimal: one loop, no refine, no review loop, no auto-merge. Grow
from here (`develop` + `review-pr` + `escalation` is the full dogfooding shape,
see `projects/fabrikant.fabrikant.yaml`).

## Adding a template

1. `src/fabrikant/templates/<name>.loop.yaml` — ADR 0047 format (`params:` + `loop:`);
   `null` = required parameter, anything else is a default.
2. Document it in the table above (required params, auto-merge behavior).
3. Add a sample `with:` mapping to `SAMPLE_PARAMS` in `tests/test_templates.py`
   so the expansion + schema validation is covered.
