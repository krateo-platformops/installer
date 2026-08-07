---
type: Architecture
title: installer — overview
description: How the umbrella works — bootstrap mode (engine subcharts + self-registration as an Installer composition) vs composition mode (Pass A registration, gated Pass B emission), the self-reconcile loop, and the three ordered-teardown hooks.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [architecture, umbrella, composition, bootstrap]
timestamp: 2026-08-07T00:00:00Z
---

# Overview

The installer is one Helm chart with **two render modes**, switched by a single flag:
`bootstrap.coreProvider.enabled` ([`values.yaml`](../chart/values.yaml), documented
there as "the SOLE bootstrap-vs-composition mode switch"). Which templates render is
strictly partitioned:

| Mode | Renders | Suppressed |
|---|---|---|
| **Bootstrap** (`enabled=true`) | `core-provider` + `core-provider-crds` subchart deps ([`Chart.yaml`](../chart/Chart.yaml) conditions), [`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml), [`bootstrap-teardown.yaml`](../chart/templates/bootstrap-teardown.yaml), [`post-delete-cleanup.yaml`](../chart/templates/post-delete-cleanup.yaml) | Pass A, Pass B, `secret.yaml`, `ordered-teardown.yaml` |
| **Composition** (`enabled=false`, the default) | [`definitions.yaml`](../chart/templates/definitions.yaml) (Pass A), [`compositions.yaml`](../chart/templates/compositions.yaml) (Pass B), [`secret.yaml`](../chart/templates/secret.yaml), [`ordered-teardown.yaml`](../chart/templates/ordered-teardown.yaml) | all bootstrap templates |

The default is **false** deliberately (composition-safe): core-provider re-renders this
chart in composition mode using the live `Installer` CR spec as values; if that spec
ever lost its `bootstrap` key, a `true` default would silently flip the render back to
bootstrap mode and wedge the install on ownership conflicts. A missing key must be safe.

## Bootstrap mode — one install, then self-registration

A bare-cluster `helm install … --set bootstrap.coreProvider.enabled=true`:

1. **Engine first.** The `core-provider` and `core-provider-crds` subcharts install the
   composition engine (CRDs and controller are hook-weighted inside those charts so the
   engine is up before anything depends on it).
2. **Self-registration.** [`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml)
   ships an `installer-self-cr` ConfigMap carrying three documents and a
   `post-install,post-upgrade` hook Job (`installer-self-bootstrap`) that applies them:
   - `compositiondefinition.yaml` — an `installer` **CompositionDefinition pointing at
     this same OCI chart and version**. It cannot be a build-time template (it is a CR
     of a CRD this very release installs, unmappable at render), so the hook waits for
     `compositiondefinitions.core.krateo.io` to exist, then applies it.
   - `installer.yaml` — the **Installer CR**: this release's values with every
     `bootstrap` flag forced off, `components` injected from
     [`files/component-pins.yaml`](../chart/files/component-pins.yaml), and the Helm
     release name pinned via the `krateo.io/release-name: krateo` label (stable across
     CR recreates; distinct from the bootstrap release `installer`).
   - `components-patch.json` — the surgical upgrade patch (see
     [usage](./usage.md#upgrade)).
   The hook waits for the apiserver to **serve this chart's version** of the Installer
   CRD via a raw discovery GET (`kubectl get --raw /apis/composition.krateo.io/v<ver>`)
   — deliberately not a plain `kubectl get`, which passes on any stale served version
   and resolves through kubectl's on-disk discovery cache.
3. **Hand-off.** core-provider generates the `Installer` CRD from the chart's
   `values.schema.json`, deploys a per-version composition controller (cdc, SA
   `installers-v<ver>`), and reconciles the Installer CR — re-rendering this same chart
   in **composition mode** with the CR spec as values. The bootstrap release never
   renders Pass A/B itself, so it does not double-own them.

Bootstrap mode also pre-grants RBAC that the cdc SA needs but cannot grant itself
(chart-inspector does not scan hooks or derive `lookup` RBAC): `services` read for the
LoadBalancer-IP lookup (`installers-lbip-services`), CRD read for the teardown's plural
resolution (`installers-teardown-crd-read`), and `nodes` read for the NodePort node-IP
lookup (`installers-nodeip-nodes` — granted unconditionally because `exposure` is
runtime-editable, so a later switch to NodePort must not depend on a bootstrap-time
values gate).

## Composition mode — Pass A + Pass B, every reconcile

Each engine reconcile is a fresh render of the whole chart against live cluster state
(Helm `lookup`), so the manifest *converges* layer by layer:

- **Pass A** ([`definitions.yaml`](../chart/templates/definitions.yaml)) registers a
  `CompositionDefinition` per component whose feature flag is on **or** whose
  Composition still exists — the second clause keeps a CD (and therefore its generated
  CRD) registered while a disabled component is still draining, so a CRD is never
  GC'd out from under a live CR. `registerOnly` catalog blueprints are always
  registered (they appear in the portal) but never auto-emitted.
- **Pass B** ([`compositions.yaml`](../chart/templates/compositions.yaml)) emits each
  component's `Composition`, gated on three conditions: feature enabled, the
  component's generated CRD **serving this component's pinned version**
  (`inst.crdExists` — version-aware, so a mid-bump served-version lag reads as "not
  ready" instead of a hard render error), and all `deps` Compositions `Ready=True`
  (`inst.depsReady`). Once an instance exists it keeps rendering even through a
  transient dep blip (`inst.compositionExists`) — a momentary not-Ready must not prune
  live workloads. When a feature is turned off, the component keeps rendering until
  every dependent is gone (`inst.dependentsGone`) — teardown is reverse-topological,
  leaves first.

Pass B also computes the platform wiring at render time (no post-install patching):
exposure `service.type`/`port` flips, browser-reachable peer URLs for the frontend
config (`inst.peerurl` / `inst.lbip` / `inst.nodeip`), Vertex/local-model injection,
the HITL gate and the autopilot's auto-derived `extraAgents` fleet — the whole surface
is described in [configuration](./configuration.md). `secret.yaml` generates the
`jwt-sign-key` Secret once and reuses it via `lookup` on every later render (reconciles
never rotate it).

The **self-reconcile loop**: the `installer` CompositionDefinition points at this
chart, so the Installer CR *is* a composition like any other. The cdc re-renders it on
its resync loop; every pass re-runs the lookups, sees newly-Ready deps, newly-served
CRDs and newly-assigned LoadBalancer IPs, and emits the next layer — until the full
DAG in [`component-pins.yaml`](../chart/files/component-pins.yaml) is up. Editing the
live Installer CR (features, componentValues, a component version) is therefore the
day-2 interface: the next reconcile converges the platform to the new spec.

## The ordered-teardown hooks

Symmetric teardown is three hooks, each solving one orphan class:

1. **HOOK 1 — [`ordered-teardown.yaml`](../chart/templates/ordered-teardown.yaml)**
   (composition release, `pre-delete`). Runs as the cdc SA while every component
   controller is still alive: deletes the component Composition CRs in the **exact
   reverse of the dependency-ordered pins list**, waiting for each kind to drain before
   the next, so every finalizer clears in order (the portal's widget/User/RESTAction
   CRs drain before the `*-crd` charts that serve them). Kind → CRD plural is resolved
   at runtime from the live CRD (irregular plurals like `krateosseproxies` can't
   drift); `oasgen-provider`'s deletion is additionally gated on ogen `RestDefinitions`
   reaching zero; orphaned `clickhouse-storage-volume-*` PVCs (the ClickHouse operator
   sets no ownerRef) are reaped at the end. Best-effort — it never blocks uninstall.
2. **HOOK 2 — [`bootstrap-teardown.yaml`](../chart/templates/bootstrap-teardown.yaml)**
   (bootstrap release, `pre-delete`). `helm uninstall installer` would otherwise delete
   core-provider in the same pass as everything it finalizes. The hook deletes the
   top-level `installer` CompositionDefinition and **blocks until the entire krateo
   footprint drains** (no CompositionDefinitions, no `composition.krateo.io` CRDs, no
   RestDefinitions — a zero count is only trusted on a successful API read, and must
   hold for 3 consecutive polls) while core-provider is still alive to cascade HOOK 1
   and clear every finalizer. Only then does helm remove the engine.
3. **HOOK 3 — [`post-delete-cleanup.yaml`](../chart/templates/post-delete-cleanup.yaml)**
   (bootstrap release, `post-delete`). Sweeps what helm never owned: core-provider's
   runtime-registered webhook configurations and the oasgen-generated
   `*.hyperdx.krateo.io` CRDs. It ships its own hook-scoped RBAC because the bootstrap
   SAs are already gone at post-delete time.

## Render-performance note

`inst.crdExists` gating used to LIST all CRDs once per component/dep check; the CRD
list is now looked up **once** per render and threaded through the helpers
([`_helpers.tpl`](../chart/templates/_helpers.tpl), note "D9") — on a full cluster this
cut the umbrella render from ~55s (blowing the cdc↔chart-inspector timeout) to seconds.
