---
type: Example
title: Minimal bootstrap install (NodePort)
description: One helm install that self-bootstraps the engine and rolls out the default portal platform, exposed via NodePort for clusters without a LoadBalancer controller.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [bootstrap, nodeport, install]
timestamp: 2026-08-07T00:00:00Z
---

# Minimal bootstrap install (NodePort)

The ONE canonical install ([usage](../../docs/usage.md)) with a values file that flips
`exposure.type` to `NodePort` — for kind/bare-metal clusters where the LoadBalancer
default has nothing to assign IPs. Browser-facing URLs are then resolved from a node
IP + each Service's allocated nodePort at reconcile time (`inst.nodeip` /
`inst.peerurl`; the chart ships the required `nodes`-read RBAC —
[configuration](../../docs/configuration.md#exposure--one-model-for-browser-facing-components)).

## Preconditions

- A bare cluster, Kubernetes ≥ 1.36, no prior Krateo install.
- Cluster-admin `kubectl`/`helm` access; nothing else — the agent tiers stay off, so
  only public `krateo-platformops` charts are pulled.

## The one command

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.11 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values.yaml
```

`bootstrap.coreProvider.enabled=true` stays on the command line, not in the values
file: it is the bootstrap-vs-composition mode switch and must be an explicit opt-in
([overview](../../docs/overview.md)). Watch the rollout converge with
`kubectl get compositiondefinitions,installers -n krateo-system`.
