---
type: Example
title: installer — full-profile values for kind
description: The values file for a complete platform (portal + observability + agent fleet) install on kind; pairs with the kind-full-profile runbook.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [kind, agents, example]
timestamp: 2026-08-07T00:00:00Z
---

# Full-profile values (kind)

`values-full.yaml` turns on every `features.*` tier and wires the private-registry auth
and Vertex AI the agents need. It is **not** standalone — follow
[docs/runbooks/kind-full-profile.md](../../docs/runbooks/kind-full-profile.md) for the
prerequisites (a k8s-1.36 kind cluster with a mapped NodePort range, the
`krateo-agentiko-chart-pull` PAT Secret, the `vertex-sa-key` Secret) and the browser
`exposure` wiring.

## Precondition
A kind cluster (k8s ≥ 1.36, NodePort range mapped to the host) and the two Secrets from
the runbook, in namespace `krateo-system`.

## Apply

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.13 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values-full.yaml
```
