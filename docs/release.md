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
