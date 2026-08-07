# installer

The umbrella orchestrator blueprint of Krateo PlatformOps: one `helm install` that
self-bootstraps the composition engine and sequences every component blueprint into an
ordered, readiness-gated platform rollout.

[![release-oci](https://github.com/krateo-platformops/installer/actions/workflows/release-oci.yaml/badge.svg)](https://github.com/krateo-platformops/installer/actions/workflows/release-oci.yaml)
[![lint](https://github.com/krateo-platformops/installer/actions/workflows/lint.yaml/badge.svg)](https://github.com/krateo-platformops/installer/actions/workflows/lint.yaml)

## What is this

A chart that bundles no components — it *sequences* them: Pass A registers a
`CompositionDefinition` per component, Pass B emits each component `Composition` only
once its dependencies are `Ready=True` and its generated CRD is served. The chart also
registers **itself** as an `Installer` composition, so the whole platform converges —
and heals, and upgrades, and tears down in order — on the engine's reconcile loop.
Full picture: [docs/index.md](docs/index.md).

## Install

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.11 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true
```

`bootstrap.coreProvider.enabled=true` is required on a bare cluster (Kubernetes
≥ 1.36); there is no step 2. Upgrades, uninstall and the agent-only profile:
[docs/usage.md](docs/usage.md).

## Configure

See [docs/configuration.md](docs/configuration.md). Most used:

| Setting | Default | Effect |
|---|---|---|
| `features.*` | portal on, agents off | Feature gates over the component DAG (`portal`, `oasgenProvider`, `coreAgents`, `specialistAgents`). |
| `exposure.type` / `exposure.port` | `LoadBalancer` / unset | How browser-facing components are exposed; `NodePort` for kind/bare-metal; `port` = one shared port. |
| `componentValues.<name>` | curated defaults | Per-component Composition-spec overrides, deep-merged — the only durable customization channel. |

## Examples

In [`examples/`](examples/), each with preconditions and the one command
([docs/examples.md](docs/examples.md)):

- [bootstrap-install](examples/bootstrap-install/README.md) — the canonical
  bare-cluster install, NodePort exposure.
- [component-overrides](examples/component-overrides/README.md) — `componentValues`
  overrides at install time or on the live Installer CR.

## Docs

- [docs/index.md](docs/index.md) — the map of the bundle.
- [docs/overview.md](docs/overview.md) — bootstrap vs composition mode, Pass A/B, the
  self-reconcile loop, the teardown hooks.
- [docs/usage.md](docs/usage.md) — install / upgrade / day-2 / uninstall.
- [docs/configuration.md](docs/configuration.md) — the whole values surface.
- [docs/api.md](docs/api.md) — the generated Installer CRD + emitted objects;
  `component-pins.yaml` as version source of truth.
- [docs/examples.md](docs/examples.md) — the runnable examples.
- [docs/release.md](docs/release.md) — how a release ships; the GVK migration.
- [docs/log.md](docs/log.md) — curated history.
- [docs/llms.txt](docs/llms.txt) — the version-pinned agent index.

## Develop & release

Validate locally with `helm dependency build chart/ && helm lint chart/` (CI runs the
same plus a render smoke test and this doc bundle's conformance check). Releases ship
from a plain-semver tag (no `v` prefix) via the canonical `release-oci` workflow:
[docs/release.md](docs/release.md).
