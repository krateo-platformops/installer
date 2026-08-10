---
type: Runbook
title: installer — full-profile install on kind
description: End-to-end install of the complete platform (portal + observability + the agent fleet) on a local kind cluster, including the private-registry auth and Vertex AI wiring the agent tiers need.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [kind, install, agents, registryAuth, vertexAI, runbook]
timestamp: 2026-08-08T00:00:00Z
---

# Full-profile install on kind

The companion runbooks cover the [default (portal)](./kind-default-profile.md) profile
(portal + observability, agents off — public charts, no credentials) and the
[agent-only](./kind-agent-only-profile.md) profile (the agent layer, no portal). This
runbook is the third combination: the **full** platform — portal + observability **and**
the agent fleet — on a local [kind](https://kind.sigs.k8s.io) cluster. It composes the
pieces from [configuration](../configuration.md) (`features`, `registryAuth`, `vertexAI`,
`exposure`) into one verified sequence.

> **Resource reality (read this first).** The full fleet — kagent + PostgreSQL, the base
> agents (autopilot, installer-agent) + the fetch-mcp server, the five specialist agents +
> clickhouse-mcp, plus the whole observability stack (ClickHouse, Keeper, MongoDB, the
> OTel collectors) — wants roughly **9–10 CPU** at rest. On an 8-core laptop it will
> **oversubscribe** and pods flap (authn especially, which fronts login). For a laptop,
> prefer the [default (portal)](./kind-default-profile.md) profile; run the full profile
> on a machine with ≥ 12 cores or on GKE. Everything below still *installs* under
> contention — it just won't be snappy.

## 0. Preconditions

- **Kubernetes ≥ 1.36** — core-provider 2.x is de-webhooked and relies on
  `MutatingAdmissionPolicy` (GA in 1.36). kind must use a `v1.36.x` node image.
- A **GitHub Personal Access Token** with `read:packages` scope that can pull the private
  `ghcr.io/krateo-agentiko/charts/*` agent charts (the agent tiers are private; the portal
  tier is public).
- A **GCP project with Vertex AI enabled** and a service-account key JSON with the Vertex
  User role (the agents' LLM backend). Alternatively, GKE node-SA ADC — not available on
  kind, so on kind you supply an explicit key Secret.

## 1. The kind cluster (k8s 1.36 + NodePort mappings)

Browser-facing components are exposed as `NodePort` on kind (LoadBalancer has no cloud
controller). The installer **pins** the four browser-facing NodePorts by default —
frontend `31000`, authn `31001`, snowplow `31002`, krateo-sse-proxy `31003` — so open the
node-port range and map exactly those four ports to the host:

```yaml
# kind-full-profile.yaml
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
  - role: worker
```

> The observability UIs (HyperDX, the kagent UI) have their own Services and are not part
> of the pinned four; expose them via `componentValues` and add their own port mappings if
> you need them in the browser.

```sh
kind create cluster --name krateo --image kindest/node:v1.36.1 --config kind-full-profile.yaml
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
references it by name (the whole fleet shares the autopilot-owned ModelConfigs, so this
one credential drives every agent):

```sh
kubectl -n krateo-system create secret generic vertex-sa-key \
  --from-file=key.json=/path/to/gcp-sa-key.json
```

## 4. The full-profile values + install

```yaml
# values-full.yaml
bootstrap:
  coreProvider:
    enabled: true          # required on a bare cluster
features:
  portal: true             # authn -> snowplow -> frontend -> portal + observability
  oasgenProvider: true
  coreAgents: true         # kagent + fetch-mcp + installer-agent + autopilot
  specialistAgents: true   # the 5 component agents + clickhouse-mcp (needs coreAgents)
exposure:
  type: NodePort           # kind has no LoadBalancer controller
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
componentValues:
  # Point the SPA's peer URLs at *.localhost on the pinned NodePorts. Browsers resolve any
  # *.localhost name to 127.0.0.1 themselves, so these land on the kind host-port mappings
  # (see "Reach the portal" below — no /etc/hosts needed). A bare loopback literal
  # (http://localhost… / http://127.0.0.1…) can't be used here — the installer drops it as a
  # dev-default — so use the krateo.localhost hostname.
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
  -f values-full.yaml
```

> The `frontend.config` URLs match the **pinned** NodePorts, so they are correct as
> written; confirm with `kubectl -n krateo-system get svc` if you changed the pins. If you
> prefer one shared port for everything, set `exposure.port` instead (see the `exposure`
> note in [configuration](../configuration.md)) and map that single host port in the kind
> config.

## 5. Watch it converge

```sh
kubectl get installers -n krateo-system                 # umbrella: want Synced=True Ready=True
kubectl get compositiondefinitions -n krateo-system     # Pass A registrations
kubectl get pods -n krateo-system                        # portal + observability + agents
kubectl -n krateo-system get agents.kagent.dev           # the agent fleet (all Agent CRs): want Ready=True
```

The umbrella reaches `Ready` within a couple of minutes (Pass A); the component
Compositions (portal, snowplow, the agents) roll out on the engine's reconcile loop after
that. The agent tier depends on the private charts pulling (step 2) and Vertex being
reachable (step 3) — an agent stuck `ImagePullBackOff` means the PAT can't read the
package; an agent up but failing LLM calls means the Vertex credential/project is wrong.

## 6. Reach the portal

```sh
kubectl -n krateo-system get secret admin-password -o jsonpath='{.data.password}' | base64 -d
```

Open `http://localhost:31000/` (the pinned frontend NodePort) and log in as `admin`.

**Pointing the browser at localhost (no `/etc/hosts`).** The portal is at
`http://localhost:31000/` — the pinned frontend NodePort is mapped to host port `31000` by
the kind config. The SPA's peer calls go to `http://krateo.localhost:31001`–`31003`, and you
do **not** need `/etc/hosts`: browsers (Chrome/Firefox/Safari) resolve any `*.localhost` name
to `127.0.0.1` themselves (RFC 6761), so `krateo.localhost` lands on your mapped host ports —
it's all loopback. Bare `localhost` can't be used for the peer URLs (the installer drops
`http://localhost…` config values as dev-defaults, #203); `krateo.localhost` is
browser-resolvable loopback that survives. `/etc/hosts` is only needed to resolve
`krateo.localhost` *outside* a browser (`curl` on macOS, or plain-glibc Linux without
systemd-resolved) — not for the portal.

> **Where the credentials come from.** The `admin` user and its `admin-password` Secret
> are seeded by the **`portal`** component (not the installer or authn), alongside a
> `cyberjoker` demo user and a `demo-system` namespace. Toggle them — or install a
> **custom portal** — via `componentValues.portal`; see
> [configuration](../configuration.md#portal-users-demo-content-and-a-custom-portal).

## 7. Turn agents off/on later (day-2)

The full profile is just `features.coreAgents` + `features.specialistAgents` flipped on.
Toggle them at runtime on the live Installer CR (never on the component CRs — they
re-render) as in [usage](../usage.md#day-2-edit-the-installer-cr-not-the-components):

```sh
kubectl patch installers.v0-3-20.composition.krateo.io installer -n krateo-system \
  --type merge -p '{"spec":{"features":{"specialistAgents":false}}}'
```

## Uninstall

```sh
helm uninstall installer -n krateo-system
kind delete cluster --name krateo
```

See also: [usage](../usage.md), [configuration](../configuration.md),
[kind-default-profile](./kind-default-profile.md),
[kind-agent-only](./kind-agent-only-profile.md),
[examples/kind-full-profile](../../examples/kind-full-profile/README.md).
