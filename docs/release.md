---
type: Runbook
title: installer — release
description: How a release ships — a plain-semver tag (no v prefix) drives the canonical release-oci publish to GHCR — and what a chart version bump means downstream (the Installer CRD GVK migration).
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [release, ci, oci, versioning]
timestamp: 2026-08-07T00:00:00Z
---

# Release

## The runbook

1. Land the change on `main` (PR; the `lint` workflow runs `helm lint` +
   `values.schema.json` validation + a render smoke test, `lint-docs` enforces this
   bundle, `security` is the org's shared scan).
2. Tag **plain semver, no `v` prefix** — the release workflow triggers on
   `[0-9]+.[0-9]+.[0-9]+` only; a `v0.3.12` tag ships nothing:

   ```sh
   git tag 0.3.12 && git push origin 0.3.12
   ```

3. [`release-oci.yaml`](../.github/workflows/release-oci.yaml) — the canonical,
   byte-identical Krateo package-build workflow — does the rest:
   - discovers `chart/` as the repo's first-class chart (vendored `charts/` deps are
     skipped);
   - substitutes the `CHART_VERSION` placeholder in
     [`Chart.yaml`](../chart/Chart.yaml) (`version` **and** `appVersion`) with the tag
     — the in-repo Chart.yaml never carries a literal version;
   - runs `helm dependency build` (pulls the pinned `core-provider` /
     `core-provider-crds` subcharts from OCI), packages, and pushes to
     `oci://ghcr.io/krateo-platformops/charts/installer` (a guard refuses a
     registry-root `OCI_REPO`, so a chart can never collide with a same-named image).
4. Verify:

   ```sh
   helm show chart oci://ghcr.io/krateo-platformops/charts/installer --version 0.3.12
   ```

Component version bumps are **not** releases of those components — they are edits to
[`chart/files/component-pins.yaml`](../chart/files/component-pins.yaml) followed by an
installer tag; the pins are chart content, so the new installer version carries them
([api](./api.md#component-pinsyaml--the-version-source-of-truth)).

### Before you merge a pin: run the root-schema gate on the component you touched

```sh
python3 hack/chart-root-schema-gate.py --pins chart/files/component-pins.yaml --only <component>
```

**This step is operator-side on purpose, and nothing in a pull request does it for you.**
`check-chart-root-schema-gate` (which does run on every PR) is the gate's OFFLINE FIXTURE SUITE:
it guards the gate's RULES and never pulls a chart. The only check that examines the PINS
themselves is `hack/chart-root-schema-gate.py --all`, and that runs **nightly**, because the
pull_request token carries `contents:read` + `checks:read` and not the `packages:read` a `helm
pull` needs — and because making every unrelated PR depend on GHCR being reachable produces a gate
people re-run until it passes. Both reasons are recorded in
[`nightly-chart-root-schema-all.yaml`](../.github/workflows/nightly-chart-root-schema-all.yaml).

So between a pin landing and 03:00 the next morning, **nothing has checked the real chart's root
schema**. Three charts — `agentgateway`, `cert-manager-issuers` and `external-dns` — sat latent
until 2026-10-31 and were found only because someone ran `--all` by hand. All three were behind
features defaulting false, which is exactly why nobody noticed: the latent cases are the ones
nobody has installed yet.

**What it catches.** core-provider injects values a composition chart's own CR does not declare —
`global` is the observed one — so a chart whose `values.schema.json` constrains its ROOT rejects
values nobody wrote, chart-inspector returns 500 while templating, and the composition can NEVER
deploy. Two root keys, one class: `additionalProperties: false` without declaring `global`, and a
root `required:` the installer's `componentValues` cannot satisfy.

**Exit 2 is WITHHELD, not a pass** — nothing was examined, or a chart could not be pulled. Read it
as a failure to measure, never as a clean result.

**Two instrument traps, if you check a schema by hand instead.** Both were hit on 2026-11-13:

- a walker that **enumerates paths** can never see a property of the ROOT, because the root has no
  path — so a root `required:` is invisible to it (this is how a root `required` reached a released
  chart with its author reporting the schema clean);
- a walker that treats any key named `required` as the KEYWORD reports a false positive on a schema
  that declares a PROPERTY named `required` — `krateo-keycloak-realm` 0.5.0 has both at
  `userProfile.attributes.items`, the keyword `['name']` and a field called `required`.

A `required` reachable only inside an array item is safe: it is evaluated only for items somebody
wrote. The dangerous one is reachable without supplying an array item. Prefer the gate above to a
hand-rolled walk; it has a validated predicate and a test suite asserting both directions.

> **The chart pin is the lever only when the cluster has not overridden it.** On an install whose
> live `Installer` CR carries its own `spec.components` list — a hand-managed or long-lived cluster
> where that list was frozen at some earlier version — **the CR wins and the chart pin is inert.**
> core-provider's cdc *renders* the platform Helm release from the CR, so editing that release's
> values (or bumping the chart) is reverted on the next reconcile, typically within minutes.
>
> Two consequences worth knowing before you touch such a cluster:
>
> - **The platform release looks operator-owned and is not — and this is STRUCTURAL, not a quirk of
>   that one release.** It holds for *every* composition, so do not read it as special-casing.
>   Verified in core-provider's `go/composition-dynamic-controller/pkg/meta/meta.go`:
>   `CalculateReleaseName` returns `obj.GetName()` (or `<name>-<uid-hash>` when safe names are on),
>   so a composition's Helm release is named after the composition and **is** its output;
>   `status.managed` is decoded from the objects rendered *inside* that release, so a release can
>   never be a member of its own managed list; and `krateo.io/composition-name` is the
>   `ParentCompositionNameLabel`, which marks a **child's parent** and is read only via
>   `IsParentRendered` — the release is not a child, so nothing stamps it.
>
>   So **both absences are guaranteed by construction**, for any composition: the missing ownership
>   label and the absence from `status.managed` are what correct behaviour looks like.
>   **Absence of an ownership marker is not absence of an owner** — and here it can never be
>   present, so it carries no information at all. Treating it as evidence is the same error as
>   reading an empty list as a real zero.
> - **Changing it means editing `spec.components` on the CR**, which is otherwise something this
>   repo tells you never to hand-edit. Guard the edit: a JSON-patch `test` op on both the component's
>   `name` and its current `version` at that index, so a shifted index fails the patch instead of
>   silently bumping the wrong component.
>
> Verify it **holds**, not that it converged: read the composition's version label, the component's
> own Helm release, the running image and the CR field together, over several minutes. A change that
> only moved the derived release looks correct for about two minutes and then reverts.
>
> See [upgrade-rollback](./runbooks/upgrade-rollback.md) for the same fact in its rollback context —
> that the live `Installer` CR is the source of truth and a Helm rollback never re-patches it.

## What a version bump means downstream

A chart version is not just a package label — it is the served version of the
`Installer` CRD (`0.3.11` → `composition.krateo.io/v0-3-11`,
[api](./api.md#the-installer-crd--generated-not-authored)). Upgrading a live cluster
is therefore a **GVK migration**, and the chart automates it
([`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml)):

1. `helm upgrade` re-applies the `installer` CompositionDefinition at the new version;
   core-provider regenerates the CRD to serve `v<new>` (eventually pruning the old
   served version) and deploys a **fresh** per-version cdc with a fresh
   `installers-v<new>` ServiceAccount.
2. The post-upgrade hook waits for `v<new>` to actually be served — via a raw
   discovery GET, immune to kubectl's stale on-disk discovery cache — then addresses
   the CR as `installers.v<new>.composition.krateo.io` and applies the surgical
   components/componentValues/exposure merge patch ([usage](./usage.md#upgrade)).
3. The chart pre-grants the lookup RBAC the fresh cdc SA needs (`services`, `nodes`,
   CRD read — [overview](./overview.md#bootstrap-mode--one-install-then-self-registration)),
   so the new controller's first render doesn't fail `forbidden`.

Downstream consumers (a CMP layering on the installer, the installer-agent) address
the CR by that versioned GVK, so they must track the pin when they hard-code a version
— which is why the hook, not the operator, owns the migration steps.
