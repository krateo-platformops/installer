---
type: ExampleIndex
title: installer — examples
description: Runnable examples under examples/ — a minimal NodePort bootstrap install, a componentValues override set, a full kind profile, the agent-gateway RBAC profile, a distribution-agnostic Gemini-API-key gateway profile and the gateway's cost/budget/failover overlays — each paired with a README stating preconditions and the one command.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [examples, install, componentvalues]
timestamp: 2026-08-20T00:00:00Z
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
- [agent-gateway](../examples/agent-gateway/README.md) — `features.agentGateway` with a
  two-tier RBAC split (`admins` vs `devs`) over the whole fleet: what the installer installs and
  wires, and where the endpoint/tool/delegation rules live.
- [agent-gateway-apikey](../examples/agent-gateway-apikey/README.md) — the **distribution-agnostic**
  profile: `vertexAI.enabled: false` + a Google AI Studio API key held at the gateway
  (`componentValues.agentgateway-policies.llm`), so the fleet runs on Gemini with **no GCP/Vertex**.
  Also flips `tracing.enabled: true` to export LLM token/cost spans to ClickStack — see
  [configuration](./configuration.md#gemini-api-key--the-vertex-free-path).
- [agent-gateway-cost-and-resiliency](../examples/agent-gateway-cost-and-resiliency/README.md) —
  the day-2 layer on the gateway: model pricing, per-request audit rows in ClickHouse, token
  budgets, provider failover, per-hop retries, span export and load shedding. One overlay per
  feature, plus a table of what `features.agentGateway` already turned on — most of it.
- [kind-full-profile](../examples/kind-full-profile/README.md) — full-profile (portal + agents) values for a kind install; see [runbook](./runbooks/kind-full-profile.md).

For a step-by-step local install per profile, see the kind runbooks:
[default (portal)](./runbooks/kind-default-profile.md),
[agent-only](./runbooks/kind-agent-only-profile.md), and
[full](./runbooks/kind-full-profile.md).
