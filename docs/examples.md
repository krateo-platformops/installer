---
type: ExampleIndex
title: installer — examples
description: Runnable examples under examples/ — a minimal NodePort bootstrap install and a componentValues override set — each paired with a README stating preconditions and the one command.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [examples, install, componentvalues]
timestamp: 2026-08-07T00:00:00Z
---

# Examples

Each example is a values file + a README with preconditions and the one command. Both
validate offline with `helm template` (after `helm dependency build chart/` and a
`CHART_VERSION` substitution — see [usage](./usage.md#local-render-no-cluster)).

- [bootstrap-install](../examples/bootstrap-install/README.md) — the ONE canonical
  bare-cluster install with `exposure.type: NodePort` for kind/bare-metal; agent tiers
  off, public charts only.
- [component-overrides](../examples/component-overrides/README.md) —
  `componentValues` deep-merge overrides (snowplow `replicaCount`, a static frontend
  `AUTHN_API_BASE_URL`), applied at install time or as a merge patch on the live
  Installer CR.
