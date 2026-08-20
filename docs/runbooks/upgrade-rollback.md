---
type: Runbook
title: Upgrade and rollback
description: How to upgrade a running Krateo platform to a new installer version, and how to roll back safely — with the one gotcha that makes `helm rollback` the wrong tool.
tags: [runbook, upgrade, rollback, installer]
timestamp: 2026-08-20T00:00:00Z
---

# Upgrade and rollback

The `Installer` **CR is the source of truth** for a running platform — component versions,
`componentValues`, `exposure`, and the runtime `features` the installer-agent toggles. The
chart is a delivery mechanism that stamps that CR. This asymmetry is the thing to keep in
mind: every procedure below is really about getting the live CR to the state you want, not
about the Helm release.

## Upgrade

```sh
helm upgrade installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version <new> \
  --namespace krateo-system \
  --set bootstrap.coreProvider.enabled=true \
  --set bootstrap.waitForSynced=true \
  -f <your-live-values.yaml>
```

What happens (validated live, 0.3.39 → 0.3.40):

1. A chart version **is** the served version of the `Installer` CRD (`0.3.40` →
   `composition.krateo.io/v0-3-40`), so an upgrade is a **GVK migration**. The chart
   re-registers the `installer` CompositionDefinition at the new version; core-provider
   regenerates the CRD to serve `v<new>` and prunes the old served version.
2. The `post-upgrade` self-bootstrap hooks (weights 0/5/10/20) wait for `v<new>` to actually
   be served (a raw discovery GET, cache-proof), then **surgically merge-patch** only the
   chart-managed keys — `components`, `componentValues`, `exposure` — onto the live CR. Your
   runtime edits (`spec.features`, operator overrides) are preserved.
3. core-provider reconciles the patched CR; each changed component's Composition re-renders
   and its Helm release moves to the new pin.

**Verified end-to-end:** upgrading 0.3.39 → 0.3.40 (whose only pin change is portal
`1.6.0 → 1.6.3`) moved the live CR's portal pin to `1.6.3`, regenerated the `portals` CRD to
serve `v1-6-3`, and the running `portal` Helm release became `portal-1.6.3` — while a runtime
annotation stamped on the CR survived. The dependency DAG holds a component back until its
deps are Ready, so an under-capacity upgrade degrades to *slow*, not *broken*.

### Preconditions

- **Kubernetes ≥ 1.36.** The chart uses `MutatingAdmissionPolicy`
  (`admissionregistration.k8s.io/v1`), which is not served on ≤1.35 — the install fails at
  manifest-build with `no matches for kind MutatingAdmissionPolicy`.
- **Size `--timeout` to a full bring-up.** With `bootstrap.waitForSynced=true` the release
  stays `pending` until the platform converges; 40m is a safe ceiling.
- **`waitForSynced` reports Synced, not Ready.** It returns `deployed` once the CR is
  `Synced`, which can be before every workload is `Ready` (a component may still be
  scheduling). That is the composition-status aggregation gap tracked in `core-provider#72`,
  not an installer bug — but do not read `deployed` as "every pod is up".

## Rollback

> **Do not use `helm rollback` to roll back component versions — validated: it does not move the platform.**

`helm rollback` reverts the **rendered chart** (the ConfigMaps that *carry* the CR spec) but
runs only `post-rollback` hooks — and the self-bootstrap hooks that merge-patch pins onto the
live CR are annotated `post-install,post-upgrade`, **not** `post-rollback` (verified in
`self-bootstrap.yaml`: 9 `post-upgrade` hook annotations, 0 `post-rollback`). It also rolls
back the **bootstrap** release, not the CR-driven `installer-platform` release that
core-provider actually manages. So the live `Installer` CR — the source of truth — is never
re-patched, and the platform stays on the new versions while the Helm revision *looks* rolled
back.

**Validated live** (fresh GKE 1.36.2, default features):

| step | Helm revision | portal release | Installer CRD served |
|---|---|---|---|
| A — install `0.3.39` | 1 | `portal-1.6.0` | `v0-3-39` |
| B — `helm upgrade` → `0.3.40` | 2 | `portal-1.6.3` ✅ propagated | `v0-3-40` |
| C — **`helm rollback` to 1** | 3 (chart `0.3.39`) | **not restored to `1.6.0`** ❌ | — |
| D — `helm upgrade` down → `0.3.39` | 4 | `portal-1.6.0` ✅ restored | `v0-3-39` |

Step C is the proof: Helm reported a successful rollback to chart `0.3.39`, but portal did
**not** return to `1.6.0`. Step D — the correct procedure below — restored it, with the live
CR's portal pin back to `1.6.0` and the CRD re-serving `v0-3-39`.

### The correct rollback: down-`helm upgrade`

Roll back by upgrading to the previous chart version. It re-drives the same `post-upgrade`
hooks, which re-serve the older GVK and merge-patch the older pins onto the live CR:

```sh
helm upgrade installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version <previous> \
  --namespace krateo-system \
  --set bootstrap.coreProvider.enabled=true \
  --set bootstrap.waitForSynced=true \
  -f <your-live-values.yaml>
```

Validated as step D above: down-upgrading `0.3.40 → 0.3.39` moved the live CR's portal pin
back to `1.6.0`, the running release to `portal-1.6.0`, and the Installer CRD's served
version back to `v0-3-39`.

**Why this is safe for stored data.** The Installer CRD keeps a fixed **storage** version
(observed: a non-served `vacuum` version is `storage=true`) while the *served* version tracks
the chart (`v0-3-39` ⇄ `v0-3-40`). Stored `Installer` objects never migrate storage — only
the API-served version flips — so moving the served version up and back down is a pure API
re-serve, not a data migration. That is what makes a down-upgrade a clean, reversible
operation rather than a lossy one.

### Rolling back a single component (surgical)

If only one component regressed, you do not need a full down-version. Edit that entry in the
live CR directly — it is the source of truth, and core-provider reconciles it in place:

```sh
kubectl edit installers.<crVer>.composition.krateo.io installer -n krateo-system
# set spec.components[<name>].version back; save. The cdc re-renders that component's release.
```

`<crVer>` is the served version of the *running* chart (e.g. `v0-3-40`), not the version you
are pinning the component to. This is also the escape hatch when a down-upgrade is itself
blocked (e.g. the previous chart is unpublished).

## Rollback caveats

- **A pruned served version must be re-servable.** Down-upgrading re-registers the older
  CompositionDefinition, so core-provider regenerates and re-serves `v<old>` — but if the
  older *chart* was deleted from the registry, that path is gone; use the surgical CR edit.
- **CRD schema shrink.** If a component's newer chart added required fields to its own CRD
  and stored CRs use them, rolling that component back to a chart whose CRD lacks those
  fields can strand the stored objects. Roll components back one minor at a time and watch
  the component's own `Synced` condition.
- **The child/trial substrate is separate.** This runbook is the **parent** platform. A
  child (vcluster) is not rolled back by any of the above; its lifecycle is the trial
  layer's concern, and its control-plane PVC is deleted on child-delete (fixed in
  `selfservice-krateo` 0.2.27), so a deleted child does not resurrect stale state.

## Verify (either direction)

```sh
# the live CR's pin for a component
kubectl get installers.<crVer>.composition.krateo.io installer -n krateo-system \
  -o jsonpath='{.spec.components[?(@.name=="portal")].version}'
# the running Helm release that pin produced
helm -n krateo-system list -A | grep '^portal '
# the served CRD version tracks the chart version
kubectl get crd installers.composition.krateo.io -o jsonpath='{.spec.versions[?(@.served)].name}'
```

The pin, the release, and (for the Installer CRD) the served version must all agree with the
chart version you intend to be on. If the Helm revision moved but these did not, you used
`helm rollback` — down-upgrade instead.
