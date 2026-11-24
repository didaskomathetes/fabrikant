# Licensing

**Open core** (Dagster-style):

- **Core — Apache 2.0** (this repo, see [`LICENSE`](../LICENSE)): engine, CLI, web UI, spec format and the plugin
  contract. Free to use, modify and redistribute, including commercially.
- **Closed / enterprise features** — separate packages built on the `fabrikant.steps` plugin contract
  ([ADR 0069](adr/0069-distribution-and-plugin-contract.md)), under a commercial license and distributed outside
  this repo. The core never requires them.
- **SaaS** — fabrikant as a hosted service is a service offering and is not covered by this repository's
  license.
