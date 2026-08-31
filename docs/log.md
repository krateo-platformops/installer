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

## 2026-08-31 — the orchestrator renders after its fleet

`autopilot`'s Agent CR sat at `Accepted=False`, `failed to compile agent
krateo-system/autopilot: Agent.kagent.dev "incident-agent" not found`, on every fresh install and
stayed there — a `kagent-controller` restart was the only way out.

kagent 0.9.12 never retries a failed Agent translation. `ReconcileKagentAgent` logs the compile
error and then returns the *status write's* error, i.e. `nil`, so controller-runtime never requeues;
`For(&Agent{})` is predicated on generation-or-label change and the spec never changes afterwards.
The only dependency changes that re-enqueue an Agent are `ModelConfig`, `RemoteMCPServer`,
`Service`, `ConfigMap` and `MCPServer` — `agentWatchFinders` has no `Agent` entry. So an unresolved
Agent→Agent reference is permanent while an unresolved RemoteMCPServer reference self-heals.
Ordering is the installer's problem; nothing downstream will fix it.

The dep direction now matches what the references imply — a referrer renders after its referent:

- `incident-agent` loses `deps: - krateo-autopilot`. It was there to order `repo-mcp-server` (which
  the autopilot release ships), already covered by the RemoteMCPServer watch, and it put
  `incident-agent`'s Agent CR strictly *after* the orchestrator's, making the failure certain: on
  0.3.92 the seven other agent Compositions landed together at 11:43:19Z and `incident-agent`'s at
  11:44:26Z.
- `krateo-autopilot` deps all 12 `agent: true` components, and `frontend-agent` deps
  `snowplow-agent` (its chart attaches it as a delegatable sub-agent — the fleet's second
  Agent→Agent reference, exposed to the same bug).

Two gates had to learn about this, both in `_helpers.tpl`; `compositions.yaml` is untouched:

- **`inst.depsReady` skips a dep whose own feature is disabled.** It never had a feature check, so it
  waited on components that are never rendered and can therefore never go Ready — which is what made
  a cross-feature dep impossible and is why the orchestrator could not simply depend on a fleet
  spanning `coreAgents`, `specialistAgents` and `codegenAgents`. A cross-feature dep now reads
  "after it, if it is installed at all". This also lifts the restriction the `agentgateway-policies`
  pin comment records.
- **`inst.dependentsGone` exempts an `orchestrator` → `agent` edge.** The teardown gate keeps a
  component alive while any dependent still exists, which is right for a real dep and wrong for this
  one: the orchestrator does not need any agent to function (it drops feature-disabled ones from
  `extraAgents` on the next render), it only needs their CRs to pre-exist. Without the exemption,
  disabling `specialistAgents` or `codegenAgents` would leave those agents rendered — alive —
  forever, held by an orchestrator that no longer references them. Every other dep on an agent is a
  real one and still blocks, so `frontend-agent` → `snowplow-agent` still drains leaves-first.

Cost, deliberately accepted: `krateo-autopilot` moves from Pass-B depth 4 to 6 (two extra waves,
behind `snowplow-agent` → `frontend-agent`), and its FIRST creation is gated on the whole enabled
fleet being Ready — so one wedged specialist keeps the Autopilot rail down on a fresh install where
it previously came up alongside. Only first creation is gated; the `$exists` clause keeps it rendered
through later churn.

Footgun: a `deps` name matching no pin resolves to an empty Kind, never goes Ready, and silently
blocks that component forever. Verified at this commit that every entry resolves, that autopilot's
deps cover exactly the `agent: true` set, and that the graph is acyclic.

## 2026-08-26 — the fleet's ModelConfigs move to their own component; `llmGateway` is gone

Every agent chart carried a 6-field `llmGateway:` block that no installer path could reach: the
installer filled it only for the `modelOwner` component, and `componentValues.<agent>` (which is
`additionalProperties: false`) declared no `llmGateway` key for any component — so a user could not
set it and `fillPath` always won. It also only ever rendered inside `if modelConfig.create`, which
defaults to `false` in every specialist. Dead in both directions, in eight charts.

It collapses to one boolean (`agentgateway.enabled`) in every agent chart, and one 4-field block
(`agentgateway.modelRoute`) in **one new component, `model-configs`** (Kind `ModelConfigs`, feature
`coreAgents`, deps `kagent`), which is now the sole owner of the fleet's kagent ModelConfigs —
extracted out of `krateo-autopilot` along with `models`, `geminiApiKey` and `localModel`. Owning the
fleet's models has nothing to do with being an orchestrator, and it kept forcing an autopilot
release (the image that gets rebuilt constantly) for a model pin. `vertexAI` was **copied**, not
moved: autopilot still needs it for its own pods' ADC.

Three consequences in this chart:

- **`modelOwner` split.** It was doing two unrelated jobs — inject `localModel` / wire the LLM route
  (a *models* concern, now on `model-configs`) and auto-derive `extraAgents` (an *orchestrator*
  concern, still `krateo-autopilot`). The second moves to a new pin, `orchestrator: true`. Deleting
  `modelOwner` from the autopilot without that split would have silently disabled fleet
  auto-orchestration.
- **`vertexAI` un-overloaded into `agent`.** The pin meant two things at once — "inject
  `.Values.vertexAI`" and "is an agent" — which is why adding a non-agent component that still needs
  the Vertex block (`model-configs`) forced two `(not $c.modelOwner)` patches and two comment blocks
  explaining them. The flag is now `agent: true` on the 13 components that DEPLOY a kagent Agent,
  and it drives all four agent-shaped behaviours directly: the `.vertexAI` injection, the HITL gate,
  `agentgateway.enabled`, and `extraAgents` membership. `model-configs` carries `modelOwner: true`
  alone and is added explicitly where it genuinely needs the same wiring (the injection and
  `agentgateway.enabled`, since its ModelConfigs derive `modelRoute` from that flag). Both double
  negatives are gone. `.Values.vertexAI` — the operator-facing block — is untouched; only the pin
  field was renamed.
- **Explicit model assignment, as values not pins.** Which ModelConfig each agent runs on is now
  plain chart values: `componentValues.<agent>.modelConfig.name` in `values.yaml`, seven entries
  carrying the tier rationale. This replaces a naming *convention*: each agent chart used to default
  `modelConfig.name` to a ModelConfig it did not own, so renaming a slot pointed seven charts at
  nothing and every Agent CR failed to compile. The charts now default to kagent's inert
  `default-model-config`, a reference that always resolves, and the installer states the choice.
  `localModel.refName` is gone with it — on that path every slot renders as Ollama anyway.

  Deliberately NOT a pin field. A pin is chart content, and `.Values.components` overrides are
  whitelisted to `version`/`registerOnly`/`tier`/`repo`/`chart` — so a `model:` pin would have been
  unoverridable, while adding a `components[]` schema field (and therefore an Installer CRD field)
  purely to express operator policy. `componentValues` is already deep-merged into the spec, so this
  costs `compositions.yaml` nothing and makes the choice `--set`-able against a fixed release:
  `--set componentValues.core-provider-agent.modelConfig.name=gemini-flash`. The tradeoff taken
  knowingly: the names are literals, so renaming a slot means editing both it and these entries —
  an alias-resolution layer to auto-follow that rename was tried and removed as unearned (it cost a
  `modelSlots` map hand-synced with the model-configs chart, ~30 lines of `compositions.yaml`, a
  `fail` guard, and a pin field, to save one edit on an event nobody performs).
- **The gateway wiring shrank** to one fill of `agentgateway.modelRoute.url`. The installer is its
  single authority: it composes the URL from the values that actually render the Gateway and its
  route (`agentgateway-policies`' `gateway.name`/`gateway.port`/`llm.routePrefix` + `namespaces.
  krateo`) rather than guarding on whether those still match a default baked into `model-configs`.
  The chart's own derivation stays the fallback for a standalone install. The `localModel` skip
  moved into that chart's tri-state derivation, and the route's on/off follows the route's
  existence: `componentValues.agentgateway-policies.llm.enabled: false` leaves no LLM route on the
  Gateway, so the installer fills `model-configs`' `agentgateway.modelRoute.enabled: false` to
  match — fill-if-absent, and only in that direction, so the chart keeps deriving the ON case.

Breaking, deliberately: `spec.llmGateway` and `modelConfig.{create,provider,model,vertexAI,
apiKeySecret,apiKeySecretKey}` are removed from the component CRDs, and `localModel.refName` from
this chart's.

## 2026-08-26 — internalUrls/internalPorts reach the components schema

`internalUrls` (0.3.59) and `internalPorts` (0.3.66) were read off `$c` in `compositions.yaml` and
carried in `component-pins.yaml`, but never declared in `values.schema.json`'s
`components.items.properties` (`additionalProperties:false`). crdgen compiles that schema into the
Installer CRD and `self-bootstrap.yaml` applies the fully rendered pin list against it, so every
release from 0.3.59 on strict-rejected its own CR — `unknown field "spec.components[5].internalUrls"`
— and `installer-self-instance` failed on a clean install. The chart rendered, linted and published
cleanly throughout; nothing catches this before someone installs. Same shape as the 0.3.20 `nodePort`
half-landing below.

Both fields are now declared, and `check-pins` gained a second guard
(`.github/scripts/check-pin-fields.py`) asserting every field used in `component-pins.yaml` exists in
the components schema — the drift is only visible by comparing two hand-maintained files, so it needs
a machine to hold them together.

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

## 2026-08-31 — repo-mcp-server and structure-graph-mcp-server are components

Both MCP servers left the `krateo-autopilot` chart for their own repos
([repo-mcp-server](https://github.com/krateo-agentiko/repo-mcp-server),
[structure-graph-mcp-server](https://github.com/krateo-agentiko/structure-graph-mcp-server), both
`0.1.0`) and are now pinned components here.

**Why.** `krateo-autopilot` is the orchestrator: it names every specialist agent as an A2A
sub-agent, so `deps:` must render it after them. It also shipped `repo-mcp-server`, which every one
of those agents needs *before* it compiles. One component, two opposite orderings — so the grounding
server became the last thing installed. Measured on a fresh install: six agents created at
`15:33:59`, `repo-mcp-server` at `15:36:00`, every one of them compiled against a `RemoteMCPServer`
that did not exist yet.

That is a genuine cycle at component granularity, and no `deps:` edit can break it. Splitting the
server out of the orchestrator's component does: `repo-mcp-server` now has `deps: [kagent]` and an
edge *into* it from each of the eight agents whose charts name it. The orchestrator keeps its
sub-agent edges. Verified: the enabled graph is acyclic, `repo-mcp-server` lands in wave 2 and
`krateo-autopilot` in wave 5, with every grounding agent between them.

`structure-graph-mcp-server` had the same shape — opt-in, and so far unexercised. It gets its own
`structureGraph` feature rather than riding `coreAgents`: both its consumers default it off, so
gating it on `coreAgents` would deploy a server nothing names.

**Image-pull wiring.** `inst.privateImageComponents` now names the two servers instead of
`krateo-autopilot`, with an empty `path` — their imagePullSecrets knob is at the spec root, not
under `mcpServers.<name>`. The injection in `compositions.yaml` walked exactly two path segments;
it now delegates to `inst.fillPath`, which walks any length and already had the fill-if-absent
semantics the hand-rolled version implemented inline.
