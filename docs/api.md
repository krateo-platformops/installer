---
type: API
title: installer — API
description: The contract the chart creates — the generated Installer CRD (crdgen from values.schema.json), the CompositionDefinitions and Compositions it emits, and component-pins.yaml as the version source of truth.
resource: installers.composition.krateo.io
tags: [crd, compositiondefinition, composition, pins]
timestamp: 2026-08-07T00:00:00Z
---

# API

This repo ships no code and serves no HTTP endpoint. Its API is the set of Kubernetes
objects the chart creates and the CRD the engine generates *from* it.

## The `Installer` CRD — generated, not authored

There is no CRD manifest in this repo. When the self-bootstrap hook registers the
`installer` CompositionDefinition (pointing at this same OCI chart —
[`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml)), **core-provider
generates the CRD from the chart itself**:

- **Schema** — crdgen converts [`chart/values.schema.json`](../chart/values.schema.json)
  into the CRD's OpenAPI schema. The values contract *is* the CR spec contract: every
  key in [configuration](./configuration.md), strictly typed
  (`additionalProperties: false`), including the per-component `componentValues`
  sub-schemas imported from each pinned component chart's own schema. Schema defaults
  are applied by the apiserver to the CR, so an omitted key gets the schema default.
- **Kind** — PascalCase of the chart name: `installer` → `Installer`.
- **Group / version** — `composition.krateo.io` / `v<chart-version, dots→dashes>`
  (`inst.apiVersion` in [`_helpers.tpl`](../chart/templates/_helpers.tpl)): chart
  `0.3.11` serves `composition.krateo.io/v0-3-11`. **Every chart release is a new
  served version** — the GVK migration mechanics are in
  [release](./release.md#what-a-version-bump-means-downstream).

The one `Installer` CR (`installer`, in `namespaces.krateo`) is applied by the
bootstrap hook with this release's values as its spec (bootstrap flags forced off,
`components` injected from the pins file) and the Helm release name pinned via the
`krateo.io/release-name: krateo` label. It is the **runtime source of truth**: the
engine re-renders the umbrella from its spec on every reconcile, and upgrades
merge-patch only the chart-managed keys onto it ([usage](./usage.md#upgrade)).

## Emitted: CompositionDefinitions (Pass A)

One per gated component ([`definitions.yaml`](../chart/templates/definitions.yaml)),
named after the component, labeled `krateo.io/tier: <tier>`:

```yaml
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: snowplow
  namespace: krateo-system
  labels:
    krateo.io/tier: platform
spec:
  chart:
    url: oci://ghcr.io/krateo-platformops/charts/snowplow
    version: "1.9.0"
```

`url` is `<ociRepo|component repo override>/<chart|name>`; `registryAuth` adds
`spec.chart.credentials` / `insecureSkipVerifyTLS` (`inst.chartExtras`). For each CD,
core-provider generates that component's own CRD (again from *that* chart's
`values.schema.json` — same pipeline as the Installer's) and deploys its cdc.
`app.kubernetes.io/managed-by` is deliberately not set: helm forces it to `Helm` at
apply time, so a chart-set value would never converge and would churn a revision every
reconcile.

## Emitted: Compositions (Pass B)

One CR per component whose gates pass, at that component's own served version:

```yaml
apiVersion: composition.krateo.io/v1-9-0
kind: Snowplow
metadata:
  name: snowplow
  namespace: krateo-system
  labels:
    krateo.io/tier: platform
    krateo.io/release-name: snowplow
spec:
  # installer-computed wiring + the componentValues.snowplow deep-merge
  service:
    type: LoadBalancer
```

The `krateo.io/release-name: <name>` label pins the cdc's Helm release name to the
stable component name (instead of `<name>-<hash(UID)>`), so a delete+recreate of the
CR adopts the existing release instead of colliding on pinned resources. The spec is
the render-time wiring described in [configuration](./configuration.md) (exposure,
frontend config, model injection, HITL, `extraAgents`) merged with
`componentValues.<name>`.

## `component-pins.yaml` — the version source of truth

[`chart/files/component-pins.yaml`](../chart/files/component-pins.yaml) is the
tested-together component set: for each component its `name`, `kind`, `chart`,
`version`, `deps`, `tier` (`platform` | `observability` | `catalog`), `feature` gate,
optional `repo` override and the wiring flags (`expose`/`exposePort`/`svcMatch`,
`configKeys`, `consumer`, `frontendConfigEnv`, `vertexAI`, `modelOwner`,
`requireApproval`). It is **chart content**, sourced via `.Files.Get`
(`inst.componentsYaml`) — not values — so it is immune to
`helm upgrade --reuse-values` and a chart bump always propagates its pins.

The DAG at a glance (36 components): the `platform` tier
(`authn`/`snowplow`/`frontend` chain + `portal`, `krateo-helm-render-service`,
`oasgen-provider`, the four `*-crd` charts), the `observability` tier gated on
`portal` (clickhouse/mongodb operators → `krateo-observability` → OTel collectors,
`krateo-sse-proxy`) and the agent tiers gated on `coreAgents`/`specialistAgents`
(`kagent-crds` → `kagent` → `installer-agent` + `krateo-autopilot`, the five
specialists + `clickhouse-mcp-server`, all pinned to the private
`oci://ghcr.io/krateo-agentiko/charts`). The autopilot chart also deploys
`repo-mcp-server`, the fleet's single grounding server — it is not a pinned component.

Two `.Values.components` interactions (validated by name against the pins,
[`_helpers.tpl`](../chart/templates/_helpers.tpl)):

- **Version override** — an entry whose `name` matches a pinned component may override
  only `version`; it flows through Pass A (CD `spec.chart.version`) *and* Pass B (the
  instance apiVersion) in lockstep, so a live single-component bump via the Installer
  CR never skews served versions.
- **Catalog append** — an unknown `name` is accepted only with `registerOnly: true`
  (+ `tier: catalog`): a blueprint registered for the portal catalog but never
  auto-emitted. Anything else fails the render loudly — a typo'd component name would
  otherwise silently register a dead CompositionDefinition.
