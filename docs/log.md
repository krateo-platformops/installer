---
type: Log
title: installer — log
description: Curated chronological history of the umbrella installer since its migration to krateo-platformops — notable changes and decisions, newest first; release notes stay in GitHub Releases.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [history]
timestamp: 2026-08-20T00:00:00Z
---

# Log

Curated history, newest first. This repo starts at the 2026-08-04 migration of the
umbrella chart into `krateo-platformops` (0.3.1); the pre-migration 0.2.x line lived
in the predecessor personal-org repo and is not mirrored here.

## 2026-08-25 — guardrails: the agent fleet's LLM traffic, filtered

`features.agentGateway` now also puts guardrails on the fleet's LLM traffic — PII and secret
masking, company-data redaction, prompt-injection blocking, and optional OpenAI moderation, Google
Model Armor, AWS Bedrock Guardrails or a bring-your-own webhook. Every knob is the policies chart's
own (`componentValues.agentgateway-policies.guardrails`), fully typed in this chart's
`values.schema.json` so it is settable from the Installer CR; `guardrails.enabled` is the single
switch that turns the layer off and on.

The wiring this chart adds is the half no component can do for itself. An agentgateway
`backend.ai` policy runs only on a route whose backend declares an LLM provider, so the agents' model
calls have to arrive on the gateway: `compositions.yaml` sets
`krateo-autopilot.llmGateway.enabled` + `baseUrl`, read from
`componentValues.agentgateway-policies.llm.*` so one flag there moves both halves and they cannot
drift. It follows `llm.enabled`, not `guardrails.enabled`: in the policies chart the LLM route is a
top-level block because it is infrastructure — every `backend.ai` policy attaches to it, guardrails
being the first — so turning the content filtering off leaves the traffic flowing through the
gateway rather than yanking the hop out from under it. When `vertexAI.enabled`, the installer also
fills `llm.vertexai.projectId`/`region` (and the SA-key Secret when set) so the gateway reaches the
same provider the agents were reaching directly — the same mirror-the-existing-switch shape as the
per-agent `vertexAI` injection. Both are fill-if-absent, so a `componentValues` override wins.

Skipped on a `localModel` install: that path deliberately points every ModelConfig at an in-cluster
Ollama, and the gateway's own default upstream is Gemini, so repointing it would take a no-cloud
install back to the cloud.

Schema drift fixed while in there: `componentValues.agentgateway-policies.sessionTrace` was added to
the policies chart with the Evidence panel (0.1.1) and never mirrored here, so an override of it was
rejected by the generated Installer CRD as an unknown field. The whole `agentgateway-policies` block
is now byte-identical to the chart's own schema with `$ref`s inlined (this file carries no `$ref`,
because crdgen consumes it).

Verified on kind-krateo end to end: with the flag on, an Autopilot A2A turn's model call traverses
the gateway (`route=krateo-system/agentgateway-policies-llm`, `gen_ai.provider.name=gcp.gemini`), a
prompt-injection attempt fails at the model call, `4242424242424242` reaches the model as
`<CREDIT_CARD>`, and delegation to a specialist still works. The trade is that the gateway is now on
the critical path of every agent turn.

## 2026-08-20 — agent gateway (JWT auth + per-user RBAC for the fleet)

New opt-in `features.agentGateway` (default `false`). It installs two dep-chained
`krateo-agentiko` components and wires two others:

- `agentgateway-controller` (kind `AgentgatewayController`) — the Gateway API CRDs, the
  agentgateway CRDs and the controller, subcharted from `oci://cr.agentgateway.dev/charts`
  (upstream's own registry; the wrapper exists only because a Composition needs a
  `values.schema.json`, which the upstream chart does not ship). Separate from the policies because helm maps every kind
  in a manifest before applying any of it, so the release shipping the Gateway API CRDs can carry
  no Gateway API resource.
- `agentgateway-policies` (kind `AgentgatewayPolicies`, deps `agentgateway-controller` + `kagent`) — the
  `GatewayClass`, the agent `Gateway`, the routes and the JWT/RBAC policies. Its deps stop at the
  substrate: depending on the agents would stall it on any profile where one is absent.
- `kagent` gets `controller.auth.mode=trusted-proxy` + `userIdClaim` and `proxy.url`; every agent
  component gets `agentgateway.enabled` (`KAGENT_PROPAGATE_TOKEN` in the pod). Those two carry the
  caller's identity past the gateway, which is what makes tool and delegation RBAC possible.
- `frontend` gets the same `agentgateway.enabled`, which repoints the portal's Autopilot A2A
  calls at the gateway instead of kagent-ui — otherwise the Bearer the browser sends reaches the
  `trusted-proxy` controller without ever being validated, and no RBAC layer applies to portal
  traffic. The gateway's browser-reachable origin arrives through the ordinary exposure model:
  `agentgateway-policies` is now an `expose: true` peer with `configKeys: [AUTOPILOT_API_BASE_URL]`.
  Its Service is provisioned by the agentgateway controller (not the chart), so
  compositions.yaml skips the service-flip for it by name instead of adding a new pin field/schema
  property — a one-off boolean would need its own entry in values.schema.json's `components[]`
  schema, and thus a regenerated Installer CRD, for a single component's plumbing quirk.
- `agentgateway-policies` gets `cors.enabled`, because that portal call is cross-origin: a browser
  sends an unauthenticated `OPTIONS` preflight first and the gateway's own authorization policy
  `403`s it. The CORS filter short-circuits the preflight only — a real request with no or a bad
  token is still `403`/`401`.

The injection is **fill-if-absent** (new `inst.fillPath` helper), unlike the exposure and vertexAI
wiring: a `componentValues` override wins on every leaf. Nothing else lands in this chart's values
— the gateway name, the JWKS endpoint, the claims and all three RBAC layers are the policies chart's
own values under `componentValues.agentgateway-policies`, with its defaults, and the installer reads
only `gateway.name`/`gateway.port`/`jwt.userClaim` back out so `proxy.url` follows an override.

The `ingress` pins are untouched. When both features are on, the edge blueprint already owns the
Gateway API CRDs, a controller and a `GatewayClass` of that name, so the installer stands this
feature's copies down. With `features.agentGateway: false` the render is unchanged except for the
two new Kinds in the ordered-teardown list.

## 2026-08-13 — actually removed the fetch-mcp-server pin

The 2026-08-12 entry below removed `fetch-mcp-server`'s schema and docs but missed the
`component-pins.yaml` entry itself, so the chart kept installing. Removed it now;
36 components confirmed by `check-pin-kinds.py`.

## 2026-08-12 — dropped fetch-mcp-server: repo-mcp-server is the fleet's only grounding path

`fetch-mcp-server` is no longer an installer component. The whole agent fleet moved to
`repo-mcp-server` as its single grounding source, and that server ships *inside* the
`krateo-autopilot` chart (Deployment + Service + `RemoteMCPServer`), so it needs no pin of
its own. Removed the `fetch-mcp-server` entry (kind `FetchMcpServer`) from
`component-pins.yaml` and its `componentValues` block from `values.schema.json`; nothing
declared it as a `deps` entry, so the `coreAgents` DAG shortens to
`kagent-crds` → `kagent` → `installer-agent` + `krateo-autopilot` with no other change.
Component count 37 → 36. `github-mcp-server` was never an installer component — it stays a
`RemoteMCPServer` CR registered by the autopilot chart, needed only by the opt-in
`codegenAgents`.

## 2026-08-08 — 0.3.20: NodePort pin — installer components schema

Declares `nodePort` in the installer's OWN `values.schema.json` `components.items` (which is
`additionalProperties:false`). Without it, crdgen generated an Installer CRD that STRICT-rejected
`spec.components[].nodePort` (`unknown field`), so the bootstrap's Installer CR apply failed
(`DeadlineExceeded` on the instance wave) the moment component-pins carried a nodePort. Completes
the NodePort-pin chain: component-pins carries it (0.3.18), component composition CRDs accept
spec.service.nodePort (0.3.19), and now the Installer CRD accepts spec.components[].nodePort.

## 2026-08-08 — 0.3.19: NodePort pin — component schema half

Completes the 0.3.18 NodePort pinning. The pin was pruned by the apiserver (`unknown field
spec.service.nodePort`) because the browser-facing component composition CRDs (generated by crdgen
from each chart's values.schema.json) didn't declare `service.nodePort`. Bumps frontend 1.4.5,
authn 0.26.2, snowplow 1.9.3, krateo-sse-proxy 0.1.8 (+ their crds), which now declare it — so
crdgen includes it in the composition CRDs and the installer's pinned nodePort actually sticks.

## 2026-08-07 — 0.3.18: pin deterministic NodePorts

`exposure.type=NodePort` now PINS a deterministic, host-mappable nodePort per browser-facing
component (frontend 31000, authn 31001, snowplow 31002, krateo-sse-proxy 31003 — in
`files/component-pins.yaml`, wired in compositions.yaml) instead of letting k8s allocate random
ports (e.g. 31063) that no kind extraPortMapping / firewall rule can target and that leave the
portal unreachable. inst.peerurl reads the pinned `.spec.ports[].nodePort` unchanged, so the
frontend config wires to a known, reachable port. LoadBalancer exposure is unaffected.

## 2026-08-07 — 0.3.17: wave-structured bootstrap

Replaces the single monolithic self-bootstrap post-install hook Job with four ordered, bounded,
idempotent hook waves (register w0 / await-crd w5 / instance w10 / opt-in finalize w20,
`bootstrap.waitForSynced`). Every wave is `activeDeadlineSeconds`-bounded, so a stuck bootstrap fails
VISIBLY at its exact stage instead of hanging in silent limbo. Same SA/RBAC + `installer-self-cr`
ConfigMap + create-or-merge-patch as 0.3.16. Does NOT eliminate the client-interruption `pending-install`
wedge (helm still blocks on hooks) — that needs a promote-on-Synced finalizer outside the hook (#13) or
the prerequisite install path.

## 2026-08-07 — 0.3.16: node-IP RBAC no longer version-pinned

Fixes a version-migration wedge in NodePort exposure. `inst.nodeip` used a cluster
`lookup "v1" "Node"`, which needs a cluster grant the FRESH `installers-v<ver>` render SA
lacks on every version bump — the old `installers-nodeip-nodes` ClusterRoleBinding was
pinned to the bootstrap-time SA, so a migration (or any cdc-driven CompositionDefinition
version bump) failed the umbrella render with `nodes is forbidden` (Synced=False) until an
operator granted it by hand. Now the bootstrap resolves the node IP ONCE (rendered with the
operator's own credentials, which can list Nodes) into a namespace `krateo-nodeip` ConfigMap,
and `inst.nodeip` reads that ConfigMap — namespace-scoped, always readable by every per-version
SA, no cluster nodes grant. The `installers-nodeip-nodes` ClusterRole/Binding is removed.

## 2026-08-07 — 0.3.15: agent fleet on the Go ADK runtime

Pin-bump release that moves the whole agent fleet from the Python ADK to the kagent
**Go ADK** runtime — measured **~30x less memory** per agent (~6 MiB vs ~185 MiB RSS).
Each agent chart now sets `spec.declarative.runtime: go` and pins the runtime image
registry to `ghcr.io` via `spec.declarative.deployment.imageRegistry` (the `golang-adk`
image is published to ghcr.io but not mirrored to `cr.kagent.dev`, where the kagent
controller defaults). The agent-chart change also folded a latent duplicate
`declarative.deployment` key that had been dropping the Vertex SA-key mount, so
`vertexAI.secretName` (already injected by the installer) now actually reaches non-
Workload-Identity clusters like kind. Pins: authn-agent 0.22.26, snowplow-agent 1.0.91,
frontend-agent 1.3.98, clickstack-agent 3.0.36, core-provider-agent 0.53.18,
krateo-autopilot 0.1.57 (autopilot + k8s-agent + helm-agent).

## 2026-08-06 — 0.3.11: version-migration hardening (#5)

Four fixes that make a live `helm upgrade` across chart versions self-contained:

- **`installers-nodeip-nodes` RBAC shipped in-chart** — the NodePort node-IP `lookup`
  (`inst.nodeip`) needs cluster-scoped `nodes` read, which chart-inspector cannot
  derive from the render scan; every fresh `installers-v<ver>` cdc SA failed the
  umbrella render with `nodes is forbidden` until an operator granted it by hand. Now
  granted unconditionally in `self-bootstrap.yaml` (read-only; `exposure` is
  runtime-editable, so it must not be gated on bootstrap-time values).
- **Cache-proof CRD wait** — the self-bootstrap hook now polls a raw discovery GET for
  the exact new served version instead of `kubectl get installers`, which passed on
  any stale served version and could spin ~10 min on kubectl's on-disk discovery cache
  mid-migration until the helm hook timeout killed the upgrade.
- **Annotation band-aid removed** — the hook's old Synced-wait loop hand-cleared stale
  `krateo.io/external-create-*` annotations, papering over a cdc bug (a 409 on
  Observe's CR write tripped the runtime's incomplete-create recovery). cdc ≥ 2.12.4
  makes Observe side-effect-free, so the runtime self-recovers; the hook no longer
  waits for Synced at all — reconcile duration is decoupled from the hook timeout.
- Autopilot pin 0.1.53 → 0.1.54.

## 2026-08-06 — 0.3.10: core-provider 2.12.2 → 2.12.4 (#4)

Bootstrap-subchart bump bringing the engine fix the 0.3.11 hook simplification relies
on: side-effect-free Observe in the composition handler (core-provider #67), ending
the external-create-pending handshake wedges.

## 2026-08-06 — 0.3.9: specialist-agent SA-key releases (#1–#3)

- The 5 specialist agent chart pins bumped to their SA-key-hook releases, and
  `vertexAI.secretName`/`secretKey` passed through to **every** agent component (#2):
  portable explicit-key ADC, so the fleet authenticates to Vertex on any cluster
  (kind/EKS/on-prem), not just GKE metadata-server ADC.
- Core-provider subchart values repointed + resource CPU typed as a k8s Quantity in
  the schema (#1).

## 2026-08-04 — 0.3.7/0.3.8: observability stack as portal-gated components

ClickStack (clickhouse/mongodb operators → `krateo-observability` → OTel collector
deployment+daemonset → `krateo-sse-proxy`) added to the pins under `tier:
observability`, gated on `features.portal` — the operators are compositions now, not
bootstrap subcharts, so enabling observability at runtime brings them up in
dependency order.

## 2026-08-04 — 0.3.5/0.3.6: the agent layer

- 0.3.5: `coreAgents` + `specialistAgents` tiers added to the pins (kagent wrappers,
  fetch-mcp-server, krateo-autopilot, the 5 specialists + clickhouse-mcp-server), all
  pinned to the private `krateo-agentiko` registry.
- 0.3.6: frontend 1.4.3 (autopilot A2A rewrite + kagent-ui :8080), agent pins moved to
  prefix-free names/versions.

## 2026-08-04 — 0.3.1–0.3.4: migration + first hardening

- 0.3.1: the umbrella installer chart migrated to `krateo-platformops/installer` —
  canonical `release-oci`/`lint`/`security` CI, `CHART_VERSION` placeholder
  versioning, OCI publish to `ghcr.io/krateo-platformops/charts/installer`.
- 0.3.2/0.3.3: bootstrap `core-provider` dependency 2.12.0 → 2.12.2.
- 0.3.4: the frontend's Autopilot toggle grays out (instead of dead-clicking) on
  agent-less installs — Pass B sets `config.AUTOPILOT_AVAILABLE: "false"` whenever
  `features.coreAgents` is off; frontend pin 1.4.2.
