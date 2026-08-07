---
type: Example
title: componentValues overrides
description: Deep-merge per-component overrides into the rendered Composition specs — scale snowplow and pin a static external authn URL — at install time or on the live Installer CR.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [componentvalues, overrides, day-2]
timestamp: 2026-08-07T00:00:00Z
---

# componentValues overrides

`componentValues.<component>` is the **only durable** per-component customization
channel: Pass B re-renders every component CR each reconcile, so a direct
`kubectl edit` of a component CR reverts, while these overrides deep-merge into the
rendered spec with the installer-computed wiring keeping precedence
([configuration](../../docs/configuration.md#componentvalues--the-durable-per-component-override-channel)).
This example scales snowplow to 2 replicas and pins a real external hostname for the
browser-facing authn URL (a non-loopback value wins over the auto-computed one).

## Preconditions

- Either a fresh install (apply at bootstrap) or a running Krateo with the live
  `Installer` CR (apply as a merge patch).
- The keys must validate against the pinned component charts' schemas — a typo'd key
  fails loudly (`values.schema.json` is strict).

## The one command

At install time, add the file to the canonical install:

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.11 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values.yaml
```

Or on a **running** platform, patch the live Installer CR (spec key
`componentValues`; the CR's apiVersion is versioned — `v0-3-11` for chart 0.3.11):

```sh
kubectl patch installers.v0-3-11.composition.krateo.io installer \
  -n krateo-system --type merge --patch-file patch.yaml
```

where `patch.yaml` wraps this file's content under `spec:` in block YAML:

```yaml
spec:
  componentValues:
    snowplow:
      replicaCount: 2
    frontend:
      config:
        AUTHN_API_BASE_URL: https://authn.example.com
```

The next reconcile re-renders snowplow and the frontend with the merged spec; no
restarts to manage (the frontend serves its config statically — reload the page).
