---
type: Usage
title: installer — usage
description: The one canonical install command, what happens next, upgrade semantics (the surgical post-upgrade merge patch), uninstall, and the agent-only profile.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [install, helm, upgrade, uninstall]
timestamp: 2026-08-07T00:00:00Z
---

# Usage

## Install — the ONE command

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.20 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true
```

For a step-by-step local walkthrough per profile, see the kind runbooks:
[default (portal)](./runbooks/kind-default-profile.md),
[agent-only](./runbooks/kind-agent-only-profile.md),
[full](./runbooks/kind-full-profile.md).

`bootstrap.coreProvider.enabled=true` is **required on a bare cluster** and is not the
default — the composition-safe default is `false` (see
[overview](./overview.md)). The flag renders the engine subcharts
(`core-provider` 2.12.5 + `core-provider-crds` 0.36.16, pinned in
[`Chart.yaml`](../chart/Chart.yaml)) and the self-bootstrap hook that registers the
umbrella as an `Installer` composition. Everything else — authn, snowplow, frontend,
portal, the observability stack — rolls out on the engine's reconcile loop in
dependency order; there is no step 2.

Preconditions:

- **Kubernetes ≥ 1.36** — the bundled core-provider 2.x is de-webhooked and relies on
  `MutatingAdmissionPolicy` ([`values.yaml`](../chart/values.yaml), `bootstrap` note).
- The default profile (`features.portal=true`, agents off) pulls only public
  `ghcr.io/krateo-platformops/charts/*` charts. The agent tiers pull from the private
  `krateo-agentiko` registry and need `registryAuth`
  ([configuration](./configuration.md#registryauth)).

Watch it converge:

```sh
kubectl get compositiondefinitions -n krateo-system
kubectl get installers -n krateo-system
kubectl get pods -n krateo-system
```

## Upgrade — surgical, runtime-state-preserving

```sh
helm upgrade installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version <new-version> \
  --namespace krateo-system \
  --set bootstrap.coreProvider.enabled=true \
  -f <your-live-values.yaml>
```

The live `Installer` CR is the source of truth for **runtime state** — `spec.features`
toggled by the installer-agent, operator edits. Re-applying the whole CR on upgrade
would clobber that, so the `post-upgrade` self-bootstrap hook
([`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml)) does a **surgical
RFC-7386 merge patch** of only the chart/values-managed keys onto the live CR:

- `spec.components` — the chart's component pins, **replaced wholesale** (the CR always
  carries the full list, so single-entry edits stay safe);
- `spec.componentValues` and `spec.exposure` — **deep-merged** (chart-set keys win,
  user-only keys survive).

`spec.features` and every other runtime field are untouched. Component pins come from
chart *content* (`.Files.Get` on
[`files/component-pins.yaml`](../chart/files/component-pins.yaml)), not values, so a
chart bump propagates its pins even under `helm upgrade --reuse-values` — but the
runbook is to pass your live values with `-f` on every upgrade: `componentValues` and
`exposure` in the patch are values-managed, and `--reuse-values` would replay stale
ones.

Mechanically, the hook is migration-hardened (0.3.11): it waits for the apiserver to
serve the **new** `v<version>` of the Installer CRD via a cache-proof raw discovery
GET, addresses the CR through the fully-qualified
`installers.v<ver>.composition.krateo.io` resource, and does **not** wait for
`Synced` — once the patch lands, Pass A/B advance on the cdc resync loop (a full
umbrella re-render legitimately takes minutes; the hook has nothing left to
contribute). A version bump is also a GVK migration for the Installer CRD — see
[release](./release.md#what-a-version-bump-means-downstream).

## Day-2: edit the Installer CR, not the components

The component Compositions are re-rendered every reconcile, so a direct `kubectl edit`
of a component CR reverts. Durable changes go through the live Installer CR:

```sh
kubectl patch installers.<v-ver>.composition.krateo.io installer -n krateo-system \
  --type merge --patch-file patch.yaml
```

with a block-YAML merge patch (e.g. flip `spec.features.coreAgents`, add a
`spec.componentValues.<name>` override, bump one `spec.components[]` version). See
[examples](./examples.md) and [configuration](./configuration.md).

## Uninstall

```sh
helm uninstall installer -n krateo-system
```

One command, symmetric with the install: the pre-delete hook chain drains the whole
platform in reverse dependency order **while every controller is still alive**, then
the post-delete hook sweeps runtime-created leftovers (webhook configurations,
oasgen-generated CRDs). The mechanics — HOOK 1/2/3 — are in
[overview](./overview.md#the-ordered-teardown-hooks).

## Agent-only profile

[`chart/values-agent-only.yaml`](../chart/values-agent-only.yaml) brings up only the
agent layer (kagent + autopilot; `features.portal=false`, `oasgenProvider=false`) and
lets the autopilot install the rest of Krateo later by editing the Installer CR:

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.20 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f chart/values-agent-only.yaml \
  --set vertexAI.projectID=<your-gcp-project>
```

The agent charts are private (`registryAuth` required — see
[configuration](./configuration.md#registryauth)); Vertex AI needs a GCP project and
either GKE node-SA ADC or an explicit key Secret (`vertexAI.secretName`). For a
step-by-step kind walkthrough, see the
[kind-agent-only runbook](./runbooks/kind-agent-only-profile.md).

## Local render (no cluster)

```sh
helm dependency build chart/          # pulls the core-provider subcharts from OCI
helm template installer chart/ --namespace krateo-system \
  --set bootstrap.coreProvider.enabled=true
```

Client-side `helm template` has no live cluster to `lookup`, so composition-mode
renders emit Pass A (and the `authn-jwt-signing-key` Secret)
but no Pass B Compositions — every Pass B gate reads "CRD not served yet". That is the
gating working as designed, not a
failure. In-repo `Chart.yaml` carries the
`CHART_VERSION` placeholder ([release](./release.md)); substitute any semver before
templating a working copy.
