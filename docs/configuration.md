---
type: Configuration
title: installer — configuration
description: The whole values surface — feature gates, the exposure model (LoadBalancer/NodePort lookups), componentValues deep-merge, registryAuth, vertexAI, localModel, hitlApproval and the bootstrap switch — with the render-time mechanics behind each.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [configuration, values, exposure, features]
timestamp: 2026-08-07T00:00:00Z
---

# Configuration

Ground truth: [`chart/values.yaml`](../chart/values.yaml) (defaults + design notes) and
[`chart/values.schema.json`](../chart/values.schema.json) (the typed contract —
`additionalProperties: false` at every level, and the source the `Installer` CRD is
generated from, see [api](./api.md)). Everything here is also the **spec of the live
Installer CR**: after bootstrap, you set these keys by patching the CR, not by
`helm upgrade` ([usage](./usage.md#day-2-edit-the-installer-cr-not-the-components)).

## Top-level surface

| Key | Default | What it does |
|---|---|---|
| `ociRepo` | `oci://ghcr.io/krateo-platformops/charts` | Registry base for every component chart URL (per-component `repo` in the pins overrides it). |
| `installDefinitions` | `true` | Whether Pass A (CompositionDefinition registration) runs at all. |
| `bootstrap.coreProvider.enabled` | `false` | THE mode switch: `true` = bootstrap render (engine subcharts + self-registration), `false` = composition render (Pass A + Pass B). See [overview](./overview.md). |
| `namespaces.krateo` | `krateo-system` | The one namespace the umbrella manages (the old clickhouse-system split is gone). |
| `exposure.type` / `exposure.port` | `LoadBalancer` / unset | How browser-facing components are exposed — below. |
| `features.*` | see below | Feature gates over the component DAG. |
| `hitlApproval` | `true` | Human-in-the-loop gate on the agents' mutating tools — below. |
| `vertexAI.*` | `enabled: true`, `location: global` | Gemini via Vertex AI ADC for the agent fleet — below. |
| `localModel.*` | `enabled: false` | Opt-in: run the whole fleet on one local Ollama model — below. |
| `componentValues.<name>` | curated defaults | Per-component Composition-spec overrides, deep-merged — below. |
| `registryAuth.*` | `enabled: false` | Credentials for in-cluster chart pulls from private registries — below. |
| `components` | unset (pins file) | Advanced. Per-entry (matched by `name`): **override** a pinned component's `version` / `registerOnly` / `tier` (e.g. `registerOnly: true` demotes it to registered-but-not-installed — how a nested/multi-tenant child opts out of a pinned component like `otel-collector-daemonset`), or **append** a new `registerOnly` catalog blueprint. Only `name` is required for an override; an append needs `kind`+`version`. See [api](./api.md#component-pinsyaml--the-version-source-of-truth). |
| `core-provider.*` | `otel.endpoint` preset | Bootstrap-subchart passthrough (only used while `bootstrap.coreProvider.enabled=true`); engine OTel export stays off until `core-provider.otel.enabled=true`. |

## `features` — the gates over the DAG

Every component in [`component-pins.yaml`](../chart/files/component-pins.yaml) carries
a `feature`; Pass A/B gate on it via `inst.featureEnabled`. No feature is
force-enabled, so a minimal install can disable everything and let an agent enable it
later on the CR.

| Flag | Default | Gates |
|---|---|---|
| `coreProvider` | `true` | Nothing — an engine-present *marker* (the engine is always-on via bootstrap). |
| `portal` | `true` | The non-agent platform: `authn` → `snowplow` → `frontend` → `portal` + `krateo-helm-render-service`, their `*-crd` charts, **and the observability tier** (clickhouse/mongodb operators, `krateo-observability`, both OTel collectors, `krateo-sse-proxy`). |
| `oasgenProvider` | `true` | `oasgen-provider` + its CRD chart. |
| `coreAgents` | `false` | The base agent layer: `kagent-crds` → `kagent` → `installer-agent` + `krateo-autopilot` + `incident-agent`. The autopilot component brings up `repo-mcp-server`, the grounding server every agent in the fleet reads through. |
| `specialistAgents` | `false` | The 5 component specialist agents (`authn/snowplow/frontend/clickstack/core-provider-agent`) + `clickhouse-mcp-server`. Needs `coreAgents` (they all dep on `kagent`). |
| `ingress` | `false` | Opt-in **edge layer**, dep-chained: `gateway-api-crds` (the Gateway API CRDs) → `agentgateway` (the Gateway API controller + CRDs + the platform `GatewayClass`/`Gateway`) → `cert-manager` (operator + CRDs) → `cert-manager-issuers` (ACME/CA Issuers) → `external-dns` (DNS records) — all **public** `oci://ghcr.io/krateo-blueprints/charts`. Off by default; the base install pulls nothing from `krateo-blueprints` unless enabled. Leave off if you front Krateo another way (an existing ingress controller / cloud LB / mesh, or your own Gateway). |

**`ingress` is the whole edge, no BYO Gateway.** Everything-is-a-blueprint: the Gateway
itself (`agentgateway`) and its CRDs (`gateway-api-crds`) are installer components too, so
`exposure.type: Gateway` ([exposure](#exposure--one-model-for-browser-facing-components))
has a Gateway to attach its per-component `HTTPRoute`s to without a manual step. The
components are dep-ordered so the CRDs are served before the Gateway, and the Gateway exists
before cert-manager's `acme.gatewayRef` and external-dns' `HTTPRoute` watching.
`componentValues.agentgateway.gateway.name` defaults to `krateo-gateway`, matching
`exposure.gatewayRef.name` and `acme.gatewayRef.name` — override all three together to rename.

**`ingress` preconditions (when enabled).** Public charts (no `registryAuth` needed), but
`external-dns` reads its DNS-provider credential from a Secret you place yourself (referenced
via `env`) — enabling `ingress` without it is like enabling the agent tier without `vertexAI`:
the component registers but can't do its job until the Secret exists.

**Configuring each edge component.** The installer ships no default spec for these, and
their charts have **required** fields — so `ingress` is only useful once you set each
component's spec through [`componentValues`](#componentvalues--the-durable-per-component-override-channel)
(the same durable per-component channel as everything else; deep-merged into the
Composition at reconcile, editable later on the live CR). **external-dns config lives under
its `external-dns` subchart-passthrough key** (0.3.x; a top-level curated field is accepted
by the CRD but never reaches the workload):

```yaml
features:
  ingress: true
componentValues:
  external-dns:
    external-dns:              # the upstream-chart passthrough (required: provider, domainFilters, txtOwnerId, env)
      provider:
        name: cloudflare       # cloudflare | google | aws | azure
      domainFilters:
        - example.com
      txtOwnerId: krateo
      policy: upsert-only      # optional: sync | upsert-only | create-only
      env:                     # the out-of-band Secret holding the provider token (never in the blueprint)
        - name: CF_API_TOKEN
          valueFrom:
            secretKeyRef:
              name: external-dns-cloudflare
              key: api-token
  cert-manager-issuers:        # required: internalCA, acme
    internalCA:
      enabled: false
    acme:
      enabled: true
      email: platform@example.com
      gatewayRef:              # points the ACME HTTP-01 solver at the Gateway API edge
        name: krateo-gateway
        namespace: krateo-system
```

Turning a feature **off** on the live CR triggers the reverse-dependency drain
(`inst.dependentsGone`): components disappear leaves-first, never out from under a
dependent ([overview](./overview.md#composition-mode--pass-a--pass-b-every-reconcile)).

## `exposure` — one model for browser-facing components

Components marked `expose: true` in the pins (authn, snowplow, frontend,
krateo-sse-proxy) get `spec.service.type = exposure.type` injected by Pass B;
everything is resolved from live Services at reconcile time via Helm `lookup` — no
post-install `kubectl patch`.

- **`type: LoadBalancer`** (default) — each exposed component gets its own cloud L4 LB.
  The frontend (the `consumer: true` component) gets `spec.config.<KEY>` for every peer
  that declares `configKeys` (`AUTHN_API_BASE_URL`, `SNOWPLOW_API_BASE_URL`,
  `EVENTS_API_BASE_URL`, `EVENTS_PUSH_API_BASE_URL`), each a browser-reachable
  `http://<lb-ip>:<port>` resolved by `inst.peerurl`/`inst.lbip`. Until an LB IP is
  assigned the key is *omitted* — the next reconcile fills it in, and the frontend
  serves `config.json` statically so a page reload picks it up.
- **`type: NodePort`** — for kind/bare-metal. `inst.peerurl` reads the Service's
  allocated `nodePort` and `inst.nodeip` resolves a browser-reachable node IP
  (ExternalIP preferred, first InternalIP as fallback) by listing Nodes. That `lookup`
  needs cluster-scoped `nodes` read on the cdc SA, which the chart itself ships:
  the `installers-nodeip-nodes` ClusterRole+Binding in
  [`self-bootstrap.yaml`](../chart/templates/self-bootstrap.yaml) — granted
  unconditionally because `exposure` is runtime-editable and a later switch to
  NodePort must not depend on a bootstrap-time gate (before 0.3.11 every fresh
  `installers-v<ver>` SA failed the render with `nodes is forbidden`).
- **`type: Gateway`** — for a Gateway API edge (agentgateway / any Gateway controller).
  Exposed components' Services stay **`ClusterIP`** (snowplow's `LoadBalancer` chart default
  is forced down too), and the installer emits **one `HTTPRoute` per exposed component**
  ([`httproutes.yaml`](../chart/templates/httproutes.yaml)) routing its hostname to that
  Service. The frontend's peer URLs become `https://<host>`. Config:
  - **`gatewayRef: {name, namespace}`** — the BYO `Gateway` the routes attach to (`parentRef`).
    Required for `Gateway`; `namespace` defaults to `namespaces.krateo`.
  - **`baseDomain`** — per-component hostnames derived as `<svcMatch>.<baseDomain>`
    (`authn.<domain>`, `snowplow.<domain>`, `sse-proxy.<domain>`).
  - **`hosts.<component-name>`** — override a single component's host (e.g.
    `hosts.frontend: portal.<domain>` or the apex).

  The installer creates **neither** the `Gateway` nor the Gateway API CRDs — emission is
  gated on the `HTTPRoute` CRD being served, so setting `Gateway` before the CRDs exist is
  **inert** (no wedge) and fills in once they're installed. Pair with
  [`features.ingress`](#features--the-gates-over-the-dag) so external-dns publishes the
  records from the routes and `cert-manager-issuers` issues certs via the same `gatewayRef`
  (see installer#27). `componentValues.<c>.ingress` remains the *classic* `Ingress`
  alternative for an Ingress-controller deployment.

  **Required inputs / caveats when `type: Gateway`:**
  - `gatewayRef.name` **and** a host source (`baseDomain` or a `hosts.<name>` override) are
    both required. With neither, no routes are emitted **and** the frontend keeps its
    `localhost` dev-defaults — a *silent* misconfiguration (the same failure #203 guards
    against, reached via a different path), so always set both.
  - `sse-proxy` is routed like the others, but it is a **single-replica** stateful in-memory
    hub (its own chart pins `replicaCount: 1`) — a plain `HTTPRoute` gives no session
    stickiness, so do **not** scale it under `Gateway` without `sessionPersistence` (same
    constraint the LB path already carries).
  - A `gatewayRef.namespace` other than `namespaces.krateo` is a cross-namespace attach — the
    Gateway's listener must permit these routes via `allowedRoutes.namespaces`.

A **static override wins**: a real external hostname pinned in
`componentValues.frontend.config.<KEY>` (e.g. behind a Gateway) suppresses the
auto-compute for that key — but a *loopback* value (`http://localhost…`,
`http://127.0.0.1…`) is treated as unset, because `values.schema.json` seeds those
dev-defaults into the spec and they must not leak to the browser
([`compositions.yaml`](../chart/templates/compositions.yaml), installer #203).

## `componentValues` — the durable per-component override channel

`componentValues.<component-name>` is deep-merged into that component's rendered
Composition **spec** in Pass B. Two rules
([`compositions.yaml`](../chart/templates/compositions.yaml)):

1. **Installer-computed wiring wins.** The merge gives the installer-rendered spec
   precedence on leaf conflicts, so `service.type`, the frontend `config` URLs,
   `vertexAI`/`hitlApproval` injection and the autopilot `extraAgents` stay
   authoritative; you can safely add anything else (`resources`, `replicaCount`,
   `service.annotations`, nested keys).
2. **It is the only durable channel.** Pass B re-renders the component CRs every
   reconcile, so a direct `kubectl edit` of a component CR reverts; edits to
   `componentValues` on the live Installer CR stick.

The schema types this map **strictly** against each pinned component chart's own
schema (`additionalProperties: false` — a typo'd key fails validation instead of
silently doing nothing). Shipped defaults worth knowing
([`values.yaml`](../chart/values.yaml)): `kagent.kagentapp.fullnameOverride: kagent`
(the fleet references the `kagent-tool-server` RemoteMCPServer by its default-install
name) and `krateo-autopilot.agents.{k8s,helm}.enabled: true` (generic Kubernetes +
Helm sub-agents on in every profile).

For the autopilot, `componentValues.krateo-autopilot.extraAgents` pins an explicit
orchestration fleet; when absent the installer **auto-derives** it from every
feature-enabled `vertexAI` agent component (excluding the autopilot itself), and
either way the list is filtered to deployed agents — a reference to an absent Agent
would fail kagent's compile.

## Portal users, demo content, and a custom portal

This is the `componentValues.portal` surface.
The **login credentials are provisioned by the `portal` component** (chart `portal`, kind
`Portal`, pinned in [`component-pins.yaml`](../chart/files/component-pins.yaml)) — **not**
by the installer or authn. When `features.portal` is on, the Portal composition seeds the
User CRs and their `kubernetes.io/basic-auth` Secrets, controlled by
`componentValues.portal`:

| Key | Default | What it creates |
|---|---|---|
| `enableAdminUser` | `true` | the `admin` User + the **`admin-password`** Secret (key `password`), **cluster-admin** |
| `enableCyberjokerUser` | `true` | the `cyberjoker` demo User + `cyberjoker-password` Secret, namespace-scoped |
| `enableDemoSystemNamespace` | `true` | the `demo-system` namespace + demo `Route`s (and grants to `cyberjoker` if enabled) |

Retrieve the admin password from the Secret the portal created:

```sh
kubectl -n krateo-system get secret admin-password -o jsonpath='{.data.password}' | base64 -d
```

For a **custom portal**, override `componentValues.portal` on the Installer CR — e.g. keep
your admin, drop the demo user and demo content:

```yaml
componentValues:
  portal:
    enableAdminUser: true
    enableCyberjokerUser: false
    enableDemoSystemNamespace: false
    # additionalProperties: true on this key — any other `portal` chart value passes through.
```

Because it is `componentValues`, this is the durable channel: set it at install time or as
a merge patch on the live Installer CR (a direct edit of the Portal CR reverts on the next
reconcile — see the two rules above).

> **Deeper portal content is a blueprint-layer topic, not an installer value.** The portal
> *UI content* — columns/rows/cards/widgets and the compositions surfaced in it — is
> authored as portal blueprints and marketplace CompositionDefinitions at the composition
> layer, outside this chart. `componentValues.portal` only governs the users, the demo
> content, and the `portal` chart's own values; for custom UI content see the `portal`
> component and the Krateo marketplace/blueprint documentation.

## `registryAuth`

core-provider pulls every component chart **in-cluster** (and the umbrella's
self-reconcile pulls this installer chart), so a private/authenticated OCI registry
needs credentials beyond your local `helm registry login`:

```yaml
registryAuth:
  enabled: true
  username: <registry-user>
  passwordRef:
    name: <secret-name>
    namespace: ""          # empty = namespaces.krateo
    key: <secret-key>
  insecureSkipVerifyTLS: false
```

When enabled, every component CompositionDefinition **and** the installer's own get
`spec.chart.credentials` (rendered by `inst.chartExtras` in
[`_helpers.tpl`](../chart/templates/_helpers.tpl));
`insecureSkipVerifyTLS` maps to `spec.chart.insecureSkipVerifyTLS` independently of
auth. Required for the private `oci://ghcr.io/krateo-agentiko/charts` agent pins;
`krateo-platformops/*` is public.

### Multiple registries — `registryAuth.registries`

The global `username`/`passwordRef` above are applied to **every** component, so they
authenticate at most one registry, and the token is attached to every component's pull —
including a component pointing (via `repo`) at a different registry. For installs that pull
from **more than one authenticated registry**, use `registries` instead — credentials keyed
by registry base:

```yaml
registryAuth:
  registries:
    - repo: oci://ghcr.io/krateo-platformops/charts   # the platform tier (ociRepo)
      username: <user>
      passwordRef:
        name: platform-registry-pull
        key: token
    - repo: oci://ghcr.io/krateo-agentiko/charts       # the agent tier
      username: <user>
      passwordRef:
        name: agentiko-registry-pull
        key: token
```

Each component's credentials are selected by matching its **effective registry base** (its
`repo` override, else `ociRepo`) against `registries[].repo`. Two consequences:

- **Multiple authenticated registries** are now expressible (one entry each).
- **No over-sharing.** A component whose registry has **no** entry gets **no** credentials —
  so a token is never presented to a registry it does not belong to. (A component pulling
  from a public registry needs no entry.)

When `registries` is set, the global `username`/`passwordRef` are **ignored** (registry-keyed
mode). When it is empty (the default), the legacy global behaviour is unchanged, so existing
installs are unaffected. `insecureSkipVerifyTLS` stays a global toggle; a `registries[]` entry
may raise it for its own registry.

## `vertexAI`

Injected as `spec.vertexAI` into every component flagged `vertexAI: true` in the pins
(the autopilot + the specialist agents). Provider is GeminiVertexAI with Application
Default Credentials — **no API key**:

- `projectID` — **required when enabled**, no default: your GCP project hosting Vertex.
- `location` — `global` by default (Google's recommended cross-region routing for
  Gemini; override for regional data residency).
- `secretName` / `secretKey` — optional *portable* ADC: a k8s Secret holding a GCP
  service-account key JSON so agents authenticate on any cluster (kind/EKS/on-prem).
  Unset ⇒ metadata-server ADC, which requires GKE nodes with the `cloud-platform`
  OAuth scope and an `aiplatform.*` IAM role on the node SA.

## `localModel`

Opt-in (default off); when enabled it **takes precedence over `vertexAI`** in Pass B:
the model owner (`krateo-autopilot`, `modelOwner: true` in the pins) renders its shared
`gemini-flash`/`gemini-pro` ModelConfigs as provider Ollama pointing at `host` with
model id `model`; every other agent is pointed at the owner-created ModelConfig
(`modelConfig.create: false`, `name: refName`) — the whole fleet on one local LLM with
no per-agent-chart change. Use a tool-calling-capable model (`qwen3.6` is the default;
`gemma ≤ 3` cannot tool-call).

## `hitlApproval`

The coarse human-in-the-loop gate, injected as `spec.hitlApproval` into every
`vertexAI` agent — orchestrator and subagents alike (kagent ≥ 0.9.9 bubbles a
subagent's tool-approval interrupt up to the user's chat, so delegation does not
deadlock). `true` (default) pauses for approval before any mutating cluster tool;
`false` is autonomous remediation. A component may instead declare its own granular
`requireApproval: [tool, …]` list in the pins — then that exact list is threaded onto
the agent spec and the coarse boolean is skipped (the frontend-agent ships
`requireApproval: []` because its only mutating-shaped tool is a server dry-run).

## `bootstrap.*` and the engine subchart passthrough

`bootstrap.coreProvider.enabled` is fully specified in
[overview](./overview.md#overview); the subchart passthroughs (top-level
`core-provider:` values) apply **only** while it is `true` — the engine is a subchart,
not a component, so its knobs are *not* `componentValues` entries. The chart presets
`core-provider.otel.endpoint` to the collector daemonset Service (the only one with an
OTLP :4318 listener) so that flipping `core-provider.otel.enabled=true` at deploy time
is the whole story of turning on engine telemetry — don't also pass an endpoint.
