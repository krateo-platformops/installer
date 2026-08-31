---
type: Runbook
title: installer — agent-only-profile install on kind
description: Install only the agent layer (kagent + autopilot + installer-agent, portal off) on a local kind cluster, wire the private-registry auth and the agents' LLM, then let the autopilot turn the rest of the platform on by editing the Installer CR.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [kind, install, agents, registryAuth, vertexAI, runbook]
timestamp: 2026-08-08T00:00:00Z
---

# Agent-only-profile install on kind

This is the **agent-only** profile — bring up **only** the agent layer (kagent + the base
agents autopilot (which ships repo-mcp-server, the grounding server) and installer-agent; `features.portal=false`,
`oasgenProvider=false`) on a local [kind](https://kind.sigs.k8s.io) cluster, then let the
autopilot install the rest of Krateo later by editing the Installer CR. It composes the
agent-tier pieces from [configuration](../configuration.md) (`features`, `registryAuth`,
`vertexAI`) into one verified sequence. For the portal, see
[kind-default-profile](./kind-default-profile.md); for both together, see
[kind-full-profile](./kind-full-profile.md).

> **Resource reality.** kagent + PostgreSQL and the base agents want roughly **2–3 CPU**
> at rest — lighter than the full profile (no observability, no specialist agents), and
> comfortable on a laptop. There is **no portal** in this profile, so there are no
> browser-facing NodePorts to map.

## 0. Preconditions

- **Kubernetes ≥ 1.36** — core-provider 2.x is de-webhooked and relies on
  `MutatingAdmissionPolicy` (GA in 1.36). kind must use a `v1.36.x` node image.
- A **GitHub Personal Access Token** with `read:packages` scope that can pull the private
  `ghcr.io/krateo-agentiko/charts/*` agent charts (the agent tiers are private).
- The agents' **LLM backend** — one of:
  - a **GCP project with Vertex AI enabled** and a service-account key JSON with the
    Vertex User role (kind has no metadata server, so on kind you always supply an
    explicit key Secret — GKE node-SA ADC is not available); **or**
  - a **local LLM** via `localModel` (Ollama) — no cloud credentials at all (see
    [configuration](../configuration.md#localmodel); pick a tool-calling model such as
    qwen3.6, not gemma).

## 1. The kind cluster (k8s 1.36)

The agent-only profile has no browser-facing components, so no NodePort mappings are
needed — a plain 1.36 cluster is enough:

```yaml
# kind-agent-only-profile.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
```

```sh
kind create cluster --name krateo --image kindest/node:v1.36.1 --config kind-agent-only-profile.yaml
```

## 2. Private-registry auth (the agent charts)

The agent charts live in the private `krateo-agentiko` GHCR org. `registryAuth` on the
Installer CR authenticates the **in-cluster chart pull** (this is distinct from an image
`imagePullSecret`). Create a Secret holding the PAT and point `registryAuth.passwordRef`
at it:

```sh
kubectl create namespace krateo-system

kubectl -n krateo-system create secret generic krateo-agentiko-chart-pull \
  --from-literal=token='<YOUR_GITHUB_PAT_read:packages>'
```

`registryAuth` (see [configuration](../configuration.md#registryauth)) then reads
`username` + `passwordRef.{name,key}` — wired in the values file below.

## 3. Vertex AI credentials (the agents' LLM)

Create a Secret with your GCP service-account key JSON in the same namespace; `vertexAI`
references it by name (the whole fleet shares the `model-configs`-owned ModelConfigs, so this
one credential drives every agent):

```sh
kubectl -n krateo-system create secret generic vertex-sa-key \
  --from-file=key.json=/path/to/gcp-sa-key.json
```

> To run fully local instead (no GCP), skip this step and set `localModel` in the values
> file rather than `vertexAI` — the whole fleet then references the local Ollama-backed
> ModelConfigs ([configuration](../configuration.md#localmodel)).

## 4. The agent-only values + install

```yaml
# values-agent-only.yaml
bootstrap:
  coreProvider:
    enabled: true          # required on a bare cluster
features:
  coreProvider: true       # engine-present marker
  coreAgents: true          # kagent + installer-agent + autopilot (ships repo-mcp-server)
  portal: false             # the agent turns the platform on later
  oasgenProvider: false
  specialistAgents: false   # not the component specialist agents yet
registryAuth:
  enabled: true
  username: <your-github-username>
  passwordRef:
    name: krateo-agentiko-chart-pull
    key: token
vertexAI:
  enabled: true
  projectID: <your-gcp-project>
  secretName: vertex-sa-key
  secretKey: key.json
```

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version 0.3.20 \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values-agent-only.yaml
```

## 5. Watch it converge

```sh
kubectl get installers -n krateo-system                 # umbrella: want Synced=True Ready=True
kubectl get compositiondefinitions -n krateo-system     # Pass A registrations
kubectl -n krateo-system get pods                        # kagent + PostgreSQL + the base agent pods
kubectl -n krateo-system get agents.kagent.dev           # the agent fleet (all Agent CRs): want Ready=True
```

An agent stuck `ImagePullBackOff` means the PAT can't read the private package (step 2);
an agent up but failing LLM calls means the Vertex credential/project is wrong (step 3),
or the local model isn't reachable if you chose `localModel`.

## 6. Turn the rest of the platform on (day-2)

The point of this profile is that the **autopilot** installs the rest of Krateo for you:
ask it to "install Krateo" and it routes to the installer-agent, which patches the live
Installer CR (`features.portal=true`, and later `specialistAgents=true`) — core-provider
then provisions the platform in dependency order. If you prefer a deterministic path, do
the same patch yourself on the **live Installer CR** (never on the component CRs — they
re-render):

```sh
kubectl patch installers.v0-3-20.composition.krateo.io installer -n krateo-system \
  --type merge -p '{"spec":{"features":{"portal":true}}}'
```

Bringing the portal up adds browser-facing components (frontend/authn/snowplow/sse-proxy)
on the pinned NodePorts `31000`–`31003` — at that point follow
[kind-full-profile](./kind-full-profile.md) for the kind NodePort mappings and the
frontend peer-URL wiring.

## Uninstall

```sh
helm uninstall installer -n krateo-system
kind delete cluster --name krateo
```

See also: [usage](../usage.md#agent-only-profile), [configuration](../configuration.md),
[kind-default-profile](./kind-default-profile.md), [kind-full-profile](./kind-full-profile.md).
