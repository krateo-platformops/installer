# Krateo Installer

Umbrella orchestrator blueprint for **Krateo PlatformOps**. It does not bundle the component
charts — it *sequences* the per-component blueprints into one ordered, readiness-gated install:

1. registers every component `CompositionDefinition` (`installDefinitions=true`), then
2. emits each component `Composition` only once its prerequisites' Compositions report
   `Ready=True` and the component's generated CRD exists.

The `CompositionDefinition` controller re-reconciles, so each pass sees more components become
Ready and emits the next layer until the platform is up.

## Install

A single `helm install` self-bootstraps the composition engine (`core-provider` +
`chart-inspector`, pulled as bootstrap subchart dependencies) and then applies the `Installer` CR,
which the engine reconciles into the full platform:

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version <version> \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true
```

`bootstrap.coreProvider.enabled=true` is required on a bare cluster — it renders the engine
subcharts. During the engine's own composition-mode render of the live `Installer`, bootstrap is
off, so the engine is never re-rendered by itself.

## Exposure

Browser-facing components (frontend, authn, snowplow) are exposed per `exposure.type`:

- **`LoadBalancer`** (default) — each gets its own cloud L4 LoadBalancer + external IP. Correct for
  GKE/EKS/AKS, where the NodePort range is firewall-blocked by default.
- **`NodePort`** — for bare-metal / kind or environments without a LoadBalancer controller. The
  installer resolves a node IP for the browser-facing URLs (needs node `list` RBAC).

Optionally set `exposure.port` to expose all browser-facing components on one shared port.

## Components

The tested-together component version set lives in [`chart/files/component-pins.yaml`](chart/files/component-pins.yaml)
(chart content, sourced via `.Files.Get` — immune to `helm upgrade --reuse-values`). Each entry
pins a chart, its version and its generated `kind`; feature/tier gates and dependency ordering
drive the readiness-gated install.

## CI

- `release-oci` — on a semver tag (`X.Y.Z`, no `v` prefix), packages the `chart/` and publishes it
  to `oci://ghcr.io/krateo-platformops/charts/installer`. `CHART_VERSION` is substituted from the tag.
- `lint` — helm lint + `values.schema.json` validation + a render smoke test on every PR.
- `security` — the org's shared security workflow.
