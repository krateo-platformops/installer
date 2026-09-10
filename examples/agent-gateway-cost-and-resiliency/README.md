---
type: Example
title: agent gateway — cost, budgets, failover and resiliency
description: The day-2 layer on the agent gateway — model pricing, per-request audit rows, token budgets, provider failover, per-hop retries, span export and load shedding — with one overlay per feature, and a table of what is already on before you set anything.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [agentgateway, cost, budgets, failover, resiliency, observability, tracing, clickhouse]
timestamp: 2026-09-09T00:00:00Z
---

# Cost, budgets, failover and resiliency on the agent gateway

Every A2A, MCP and LLM call in the fleet crosses one proxy pod, which is why one place can price
every turn, cap it, retry it, fail it over to another provider and say who spent it. This example is
that layer. RBAC and the content guardrails are [../agent-gateway](../agent-gateway/README.md); the
provider path (Vertex vs a plain Gemini API key) is
[../agent-gateway-apikey](../agent-gateway-apikey/README.md).

**Most of it is already on.** Read the table first — several of these overlays only narrow or
document what `features.agentGateway` already gave you.

## What is on before you set anything

| | Default | Where |
|---|---|---|
| Token metering | **on** | agentgateway itself, no policy needed |
| Dollar pricing (`costCatalog`) | **on** | `costCatalog.enabled`, needs `gateway.parameters.enabled` |
| Proxy sizing + `AgentgatewayParameters` | **on** | `gateway.parameters.enabled` |
| Pod disruption budget | **on** (`minAvailable: 1`) | `gateway.parameters.pdb.enabled` |
| Per-team metric labels | **on** | `observability.metrics.enabled`, `.includeTeam` |
| Access-log rows in ClickHouse | **on** | `observability.accessLog.enabled` |
| Per-user attribution on those rows | **on** | `observability.accessLog.includeUser` |
| Guardrail verdicts (which guard fired) | **on** | `observability.accessLog.guardrails` |
| Tool-call extraction | **on** | `observability.accessLog.toolCalls.enabled` |
| Retries + timeouts, per hop | **on** | `resiliency.enabled` |
| Provider health checking / eviction | **on** | `llm.health.enabled` |
| — | | |
| Proxy autoscaling | **off** | `gateway.parameters.hpa.enabled` |
| Per-user metric labels | **off** (high-cardinality) | `observability.metrics.includeUser` |
| Prompt + completion text | **off** (a retention decision) | `observability.accessLog.promptLogging.enabled` |
| Token budgets | **off** (no safe fleet-wide number) | `budgets.enabled` |
| Budget-breach alerts → incidents | **off** | `observability.alerts.enabled` |
| Provider failover tiers | **off** (one upstream) | `llm.tiers` |
| Span export | **off** (the expensive signal) | `tracing.enabled` |
| Load shedding | **off** (needs load data) | `loadShedding.enabled` |

Health checking is on while failover is off on purpose: with one provider it is a no-op, and it is
already correct the day a second tier is added — **without it nothing is ever evicted and failover
never happens**, however many tiers are configured.

## Preconditions

- The [../agent-gateway](../agent-gateway/README.md) preconditions (a Krateo install with `authn`
  up, credentials for the private `krateo-agentiko` registry, and a model provider).
- **The ClickStack daemonset collector's `logs` pipeline must declare an `otlp` receiver.** The
  access log is on by default and exports OTLP `LogRecord`s there; the stock pipeline was
  `receivers: [filelog]` only, so without it the rows reach a port with no consumer and are dropped
  with no error at either end. `clickstack-chart` ships it — confirm rather than assume:

  ```console
  $ kubectl -n krateo-system get cm otel-collector-daemonset-opentelemetry-collector-agent \
      -o jsonpath='{.data.relay}' | grep -A3 'logs:'
          receivers: [filelog, otlp]
  ```

- **Chart versions.** Every key below needs `agentgateway-policies` **0.2.0**, and it must be paired
  with `agentgateway-controller` **0.2.0** — the installer pins both in
  [`chart/files/component-pins.yaml`](../../chart/files/component-pins.yaml). On an older pin the
  keys are rejected by the component chart's schema before helm sees them.
- **The pairing is not cosmetic.** `agentgateway-controller` 0.2.0 is what pins the controller *and*
  proxy images at **v1.5.0**. At controller v1.4.1 the control plane mounts the cost-catalog
  ConfigMap but never writes the line telling the proxy to read it, so every lookup answers
  `status="NoCatalog"`, `cost_usd` never appears, **and nothing logs an error anywhere**. The
  agentgateway CRDs stay at v1.4.1, which is what leaves load shedding's two counts unavailable.

  ```console
  $ kubectl -n krateo-system get deploy agentgateway-controller \
      -o jsonpath='{.spec.template.spec.containers[0].image}'
  $ kubectl -n krateo-system get cm krateo-agent-gateway -o jsonpath='{.data.config\.yaml}'
  # `config: {}` means the controller is still v1.4.1 — the one failure here that looks like success
  ```

## One command

`values.yaml` is the base install with everything at its shipped default. Layer on whichever
overlays you want; they are independent except where noted.

```console
$ helm upgrade -i installer oci://ghcr.io/krateo-platformops/charts/installer \
    -n krateo-system -f values.yaml -f budget-fleetwide.yaml
```

Every overlay writes only under `componentValues.agentgateway-policies`, so each also works as a
merge patch on the live `Installer` CR.

## The overlays

| File | Turns on | Turn it back off with |
|---|---|---|
| [observability-and-cost.yaml](observability-and-cost.yaml) | nothing — it is the already-on set, written out to be edited | see the table below |
| [budget-fleetwide.yaml](budget-fleetwide.yaml) | one token ceiling shared by every caller, plus the two breach alerts | `budgets.enabled: false` |
| [budget-tiers.yaml](budget-tiers.yaml) | independent per-group and per-user buckets, first match wins | `budgets.enabled: false` |
| [provider-failover.yaml](provider-failover.yaml) | ordered provider tiers with health-based eviction | `llm.tiers: []` |
| [retries-and-timeouts.yaml](retries-and-timeouts.yaml) | nothing — the per-hop defaults, written out to be tuned | `resiliency.enabled: false` |
| [tracing.yaml](tracing.yaml) | span export, and the export filter | `tracing.enabled: false` |
| [load-shedding.yaml](load-shedding.yaml) | a connection-duration cap (the two counts need newer CRDs) | `loadShedding.enabled: false` |

### Two combinations the chart refuses

Each would look like it worked, so a render guard rejects the install instead:

- `gateway.parameters.hpa.enabled` **with** `budgets.enabled` — a local rate-limit bucket lives in
  the proxy *process*, so N replicas enforce N times the budget you wrote.
- `llm.tiers` **with** `llm.health.enabled: false` — nothing would ever be evicted, so nothing would
  ever fail over; the tiers render, validate and report `Accepted`.

### Every off switch

All under `componentValues.agentgateway-policies`.

| Set this | Effect |
|---|---|
| `costCatalog.enabled: false` | No pricing. Tokens are still counted; `cost_usd` disappears. |
| `gateway.parameters.enabled: false` | Proxy takes the control plane's default sizing, and **the cost catalog stops working** — it can only reach the proxy through this object. |
| `observability.metrics.enabled: false` | Metrics keep being served, without the team/user labels. |
| `observability.accessLog.enabled: false` | No rows in ClickHouse. **Access control still enforces** — it just stops leaving a record, and the alerts have nothing to count. |
| `observability.accessLog.promptLogging.enabled: false` | Prompt and answer text stop being stored. The default. |
| `observability.alerts.enabled: false` | No `Alert` CRs. Budgets still return `429`; no incident is raised. The default. |
| `budgets.enabled: false` | Nothing is capped. The default. |
| `resiliency.enabled: false` | No retries and no timeouts on any hop. |
| `tracing.enabled: false` | No spans. The default. |
| `loadShedding.enabled: false` | No caps. The default. |
| `llm.tiers: []` | One provider, no failover. The default. |
| `llm.enabled: false` | The gateway leaves the LLM path entirely — the agents' ModelConfigs go back to the provider direct (the installer follows this flag, so there is no second switch). The escape hatch if the gateway is breaking model calls. |

## Verify

```console
$ kubectl -n krateo-system get agentgatewaypolicy.agentgateway.dev
$ kubectl -n krateo-system get agentgatewayparameters agentgateway-policies-params -o yaml
$ kubectl -n krateo-system get cm agentgateway-policies-model-catalog \
    -o jsonpath='{.data.catalog\.json}' | jq '.providers | keys'
$ kubectl -n krateo-system get alert            # budget-fleetwide.yaml only
$ kubectl -n krateo-system get hpa,pdb
```

Pricing is being *used* — `status="Exact"` and a non-zero cost — only on the pod's own metrics port,
which is not on the Service:

```console
$ kubectl -n krateo-system port-forward deploy/krateo-agent-gateway 15020:15020
$ curl -s localhost:15020/metrics | grep -E 'cost_catalog_lookups_total|cost_usd_total'
```

**Nothing in Krateo scrapes that port**, so it is a live gauge zeroed by a pod restart. The durable
record is the access log in ClickHouse — and the two are the same numbers on different clocks, not a
discrepancy. When they must agree, trust ClickHouse:

```sql
SELECT LogAttributes['user'] AS user, LogAttributes['team'] AS team, count() AS calls,
       sum(toUInt64OrZero(LogAttributes['tokens_in']))           AS tok_in,
       sum(toUInt64OrZero(LogAttributes['tokens_out']))          AS tok_out,
       round(sum(toFloat64OrZero(LogAttributes['cost_usd'])), 6) AS cost_usd
FROM default.otel_logs
WHERE ServiceName = 'krateo-agent-gateway' AND LogAttributes['protocol'] = 'llm'
GROUP BY user, team WITH TOTALS ORDER BY cost_usd DESC
```

Four things bite when writing that query yourself:

- **`LogAttributes['protocol'] = 'llm'` is not optional.** `ServiceName` alone matches every hop,
  and model turns are a small minority — an unfiltered `LIMIT 5` reliably returns five rows that
  were never model calls, with every cost column empty, which reads exactly like a broken catalog.
- **Every request lands in the table twice.** The gateway's OTLP row is the structured one and has
  everything in `LogAttributes` with an **empty `Body`**; the duplicate is the collector's `filelog`
  receiver scraping the same line off stdout. A query filtering on `Body` matches nothing and
  silently hits the filelog copies instead.
- **Every `LogAttributes` value is a `String`**, so a bare `sum()` fails and `max()` sorts lexically
  — and use the `OrZero` casts, because a failed row has an empty `cost_usd` and a strict cast
  aborts the whole query.
- **An absent attribute is absent, not zero.** `cost_usd` is missing on an unpriced turn and the
  guardrail pair is missing on a clean pass.

## Further reading

The policies chart's own `docs/configuration.md` carries every value, every default and the
reasoning; `docs/global-budgets.md` covers what a per-caller bucket or a window longer than an hour
would take (a rate limit service plus Redis, deliberately not built).
