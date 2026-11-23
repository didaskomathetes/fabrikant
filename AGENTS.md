# AGENTS
## Follow best practices:
- linting
- type checking
- testing: aim for 80%+ coverage. testing getters and setters is unnecessary, but test all logic and edge cases.
- documentation; document all public functions, classes, and modules; document any non-obvious behavior. try to keep it up to date with code changes and try to keep it short.
- don't change/remove tests just because they fail; fix the code or the test, but don't remove tests (unless they are truly obsolete)
- never change specs/ADRs without explicit confirmation from the user
- codequality; try to use tools like sonarqube for code quality analysis and keep the code quality high. avoid code smells, duplication, and complexity.
- work like a distinguished engineer; don't just hack together a solution, think about the long term maintainability, architecture and scalability of the code.
- employ the boy scout rule: leave the codebase cleaner than you found it. if you see a small improvement that can be made, make it. if you see a larger improvement that can be made, create an issue
- keep the code DRY (don't repeat yourself); if you see code duplication, refactor it into a common function or class.
- don't keep hardcoding constants in literal values; introduce constants or configuration values.
- model structured data with types: when a dict with a known set of keys gets passed around (e.g. a request, a context, a record section), introduce a dataclass or small class with typed fields, defaults and the behavior that belongs to it (parsing, serialization, formatting). avoid opaque procedural code that pokes at keys that may or may not be there. plain dicts are fine at the boundaries (JSON I/O, ad-hoc lookups); convert at the edge.
- before you start implementing a feature from scratch, check if there is already a well-maintained library or module that can be used. don't reinvent the wheel.

## Language
- write docs (README, ADRs), issues, PR descriptions, commit messages, comments and docstrings in English.

## ADRs, Critical descisions, and design docs
- all critical decisions should be documented in ADRs (architecture decision records) or design docs.
- docs are in docs/adr or docs/
- if you change an ADR numbering, search the codebase for references to it and update them. don't just change the number in the ADR file.
- when mergeing a PR that containts ADRs, make sure to update the ADR index (docs/adr/README.md) with the new ADRs and their status. Merge to main is status=accepted.
- when updating an ADR, make sure to update the status in the index and in the ADR file with the date and a short summary of the change.