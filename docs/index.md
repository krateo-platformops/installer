---
type: ChartRepo
title: installer — index
description: The map of the installer doc bundle — the umbrella orchestrator chart that sequences every Krateo PlatformOps component blueprint into one ordered, readiness-gated install.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [installer, umbrella, orchestrator, chart-repo]
timestamp: 2026-08-07T00:00:00Z
---

# installer

The **umbrella orchestrator blueprint** of Krateo PlatformOps. It bundles no component
charts — it *sequences* the per-component blueprints: it registers a
`CompositionDefinition` per component and emits each component `Composition` only once
its dependencies are `Ready=True` and its generated CRD is served. One bare-cluster
`helm install` self-bootstraps the composition engine and registers the umbrella as a
composition of itself, so the whole platform converges on the engine's reconcile loop —
no scripts, no second command.

This repo is chart-only: `chart/` (templates, `values.yaml`, `values.schema.json`,
`files/component-pins.yaml`) plus the canonical CI workflows. There is no application
code — the runtime is [core-provider](https://github.com/krateo-platformops/core-provider),
pulled as a bootstrap subchart dependency.

## The bundle (start here)

- [overview](./overview.md) — how the umbrella works: bootstrap vs composition mode,
  Pass A / Pass B, the self-reconcile loop, the three teardown hooks.
- [usage](./usage.md) — the ONE canonical install, upgrade semantics (the surgical
  post-upgrade merge patch), uninstall, the agent-only profile.
- [configuration](./configuration.md) — the whole values surface: `features`,
  `exposure`, `componentValues`, `registryAuth`, `vertexAI`, `localModel`,
  `hitlApproval`, `bootstrap`.
- [api](./api.md) — the generated `Installer` CRD (crdgen from `values.schema.json`),
  the emitted CompositionDefinitions/Compositions, and `component-pins.yaml` as the
  version source of truth.
- [examples](./examples.md) — the runnable examples under `examples/`.
- [runbooks/kind-full-profile](./runbooks/kind-full-profile.md) — install the full platform (portal + observability + agent fleet) on a local kind cluster.
- [release](./release.md) — how a release ships (plain-semver tag → OCI chart) and what
  a version bump means downstream (the `v0-3-x` GVK migration).
- [log](./log.md) — curated history.
- [llms.txt](./llms.txt) — the version-pinned agent index of this bundle.

## Ground truth in the chart

| Concern | File |
|---|---|
| Values + inline design notes | [`chart/values.yaml`](../chart/values.yaml) |
| The typed values contract (and CRD source) | [`chart/values.schema.json`](../chart/values.schema.json) |
| Component pins (versions, deps, gates) | [`chart/files/component-pins.yaml`](../chart/files/component-pins.yaml) |
| Pass A — CompositionDefinition registration | [`chart/templates/definitions.yaml`](../chart/templates/definitions.yaml) |
| Pass B — gated Composition emission | [`chart/templates/compositions.yaml`](../chart/templates/compositions.yaml) |
| Self-bootstrap (CR, RBAC, post-install/upgrade hook) | [`chart/templates/self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml) |
| Gating/exposure helpers | [`chart/templates/_helpers.tpl`](../chart/templates/_helpers.tpl) |
| Ordered teardown hooks | [`chart/templates/ordered-teardown.yaml`](../chart/templates/ordered-teardown.yaml), [`bootstrap-teardown.yaml`](../chart/templates/bootstrap-teardown.yaml), [`post-delete-cleanup.yaml`](../chart/templates/post-delete-cleanup.yaml) |
