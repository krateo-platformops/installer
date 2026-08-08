---
type: Runbook
title: installer — default-profile (portal) install on kind
description: End-to-end install of the default platform (portal + observability, agents off) on a local kind cluster — public charts only, no credentials, browser-facing components on pinned NodePorts.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [kind, install, portal, nodeport, runbook]
timestamp: 2026-08-08T00:00:00Z
---

# Default-profile (portal) install on kind

This is the **default** profile — the non-agent platform: authn → snowplow → frontend →
portal, plus the observability stack — on a local [kind](https://kind.sigs.k8s.io)
cluster. It is the lightest, credential-free way to stand the platform up: the portal
tier pulls only **public** `ghcr.io/krateo-platformops/charts/*` charts, so there is **no
`registryAuth`, no Vertex AI, no Secret** to create. For the agent tiers, see
[kind-agent-only](./kind-agent-only-profile.md) and [kind-full-profile](./kind-full-profile.md).

> **Resource reality.** The default profile is portal + observability (ClickHouse,
> Keeper, MongoDB, the OTel collectors) — no agent fleet. It fits a modern laptop
> (≈ 4 cores / 8 GB free), but the observability stack still needs a few cores to be
> snappy. If you only want the portal UI and can skip observability internals, it still
> comes up under contention — just not instantly.

## 0. Preconditions

- **Kubernetes ≥ 1.36** — core-provider 2.x is de-webhooked and relies on
  `MutatingAdmissionPolicy` (GA in 1.36). kind must use a `v1.36.x` node image.
- **No credentials.** The default profile pulls only public charts; there is nothing to
  authenticate. (`registryAuth` and `vertexAI` are for the agent tiers.)

## 1. The kind cluster (k8s 1.36 + NodePort mappings)

Browser-facing components are exposed as `NodePort` on kind (LoadBalancer has no cloud
controller). The installer **pins** the four browser-facing NodePorts by default —
frontend `31000`, authn `31001`, snowplow `31002`, krateo-sse-proxy `31003` — so the
kind config only has to open the node-port range and map those four ports to the host:

```yaml
# kind-default-profile.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
kubeadmConfigPatches:
  - |
    kind: ClusterConfiguration
    apiServer:
      extraArgs:
        service-node-port-range: "31000-31100"
nodes:
  - role: control-plane
    extraPortMappings:
      - { containerPort: 31000, hostPort: 31000, protocol: TCP }   # frontend / portal
      - { containerPort: 31001, hostPort: 31001, protocol: TCP }   # authn
      - { containerPort: 31002, hostPort: 31002, protocol: TCP }   # snowplow
      - { containerPort: 31003, hostPort: 31003, protocol: TCP }   # sse-proxy (events)
  - role: worker
```

```sh
kind create cluster --name krateo --image kindest/node:v1.36.1 --config kind-default-profile.yaml
```

## 2. The default-profile values + install

The default `features` already are the portal profile (`portal: true`,
`oasgenProvider: true`, `coreAgents: false`, `specialistAgents: false`), so the values
file only sets kind-specific exposure and the frontend's browser-facing peer URLs:

```yaml
# values-default.yaml
bootstrap:
  coreProvider:
    enabled: true          # required on a bare cluster
features:
  portal: true             # authn -> snowplow -> frontend -> portal + observability
  oasgenProvider: true
  coreAgents: false         # agents off — public charts only, no credentials
  specialistAgents: false
exposure:
  type: NodePort           # kind has no LoadBalancer controller
componentValues:
  # kind is not browser-reachable by the node IP; point the SPA at *.localhost, which
  # browsers resolve to 127.0.0.1 (RFC 6761). A loopback literal (localhost/127.0.0.1) is
  # rejected as a dev-default and treated as unset, so use a *.localhost hostname. Ports
  # are the pinned NodePorts of each peer. If your OS resolver doesn't map *.localhost
  # (e.g. plain glibc without systemd-resolved), add `127.0.0.1 krateo.localhost` to /etc/hosts.
  frontend:
    config:
      AUTHN_API_BASE_URL: http://krateo.localhost:31001
      SNOWPLOW_API_BASE_URL: http://krateo.localhost:31002
      EVENTS_API_BASE_URL: http://krateo.localhost:31003
      EVENTS_PUSH_API_BASE_URL: http://krateo.localhost:31003
```

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.20 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values-default.yaml
```

## 3. Watch it converge

```sh
kubectl get installers -n krateo-system                 # umbrella: want Synced=True Ready=True
kubectl get compositiondefinitions -n krateo-system     # Pass A registrations
kubectl get pods -n krateo-system                        # authn, snowplow, frontend, portal, observability
kubectl get svc -n krateo-system                          # frontend/authn/snowplow/sse-proxy on pinned NodePorts 31000-31003
```

The umbrella reaches `Ready` within a couple of minutes (Pass A); the component
Compositions (authn → snowplow → frontend → portal, plus observability) roll out on the
engine's reconcile loop after that in dependency order.

## 4. Reach the portal

```sh
kubectl -n krateo-system get secret admin-password -o jsonpath='{.data.password}' | base64 -d
```

Open `http://localhost:31000/` (the pinned frontend NodePort) and log in as `admin` with
that password. The events bell in the portal header is served by sse-proxy over
`31003`; authn and snowplow are reached by the SPA over `31001`/`31002`.

## 5. Turn agents on later (day-2)

The default profile is one flag short of the agent tiers. Enabling them at runtime is a
patch on the **live Installer CR** (never on the component CRs — they re-render), but the
agents also need `registryAuth` (private charts) and `vertexAI` (their LLM) — set those
first (see [kind-agent-only](./kind-agent-only-profile.md) /
[kind-full-profile](./kind-full-profile.md)):

```sh
kubectl patch installers.v0-3-20.composition.krateo.io installer -n krateo-system \
  --type merge -p '{"spec":{"features":{"coreAgents":true}}}'
```

## Uninstall

```sh
helm uninstall installer -n krateo-system
kind delete cluster --name krateo
```

See also: [usage](../usage.md), [configuration](../configuration.md),
[kind-agent-only](./kind-agent-only-profile.md), [kind-full-profile](./kind-full-profile.md).
