---
type: Example
title: telemetry retention (ClickHouse TTL)
description: Set the otel_* table retention from the Installer CR, understand why it is a no-op on a cluster that has already ingested, and see what per-signal retention would take.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [observability, retention, clickhouse, componentvalues, day-2]
timestamp: 2026-10-20T00:00:00Z
---

# Telemetry retention (ClickHouse TTL)

The two OTel collectors export to ClickHouse through the contrib `clickhouse`
exporter. Its `ttl` is stamped as a **native ClickHouse TTL clause into the
`CREATE TABLE`** the exporter issues for its signal, so retention is a property of
the table, not a setting the collector enforces at query time.

Both charts default to `ttl: 168h` (7 days) — the bound added after the 2026-07
incident, where no TTL anywhere grew the `otel_*` tables until the volume filled in
8 days, every insert failed with `code: 243 (NOT_ENOUGH_SPACE)`, and the collectors
buffered and OOM-crash-looped until telemetry went fully dead.

This example raises both to 90 days.

## Preconditions

- A running Krateo with the `portal` feature on (the observability backend is
  portal-gated), or a fresh install.
- Component pins at `otel-collector-deployment >= 0.3.3` and
  `otel-collector-daemonset >= 0.1.5`. Earlier charts have no `ttl` key at all.
- Headroom for the retention you choose. Measure before you raise it:
  `kubectl exec -n krateo-system <clickhouse-pod> -- df -h /var/lib/clickhouse`.

## The one command

On a running platform, patch the live `Installer` CR:

```sh
kubectl patch installer krateo -n krateo-system --type merge --patch-file values.yaml
```

At install time, add the file to the canonical install instead:

```sh
helm install installer \
  oci://ghcr.io/krateo-platformops/charts/installer \
  --version <CHART_VERSION> \
  --namespace krateo-system --create-namespace \
  --set bootstrap.coreProvider.enabled=true \
  -f values.yaml
```

## Read this before you believe the value took effect

`create_schema: true` issues `CREATE TABLE IF NOT EXISTS`. **The `ttl` therefore only
applies to tables that do not exist yet.** On any cluster that has ingested once, the
`otel_*` tables are already there, the CREATE is skipped, and the live retention stays
whatever the tables were created with — while `helm get values` happily shows your new
number. The config is applied; the table is not.

Verify what is actually live, never the values:

Query it from a Job that mounts ClickHouse's own credential — never pass the password
on a command line, and use the HTTP interface rather than `clickhouse-client`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: check-otel-ttl
  namespace: krateo-system
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: curl
          image: curlimages/curl:8.11.1
          env:
            - name: CH_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: clickstack-secret
                  key: CLICKHOUSE_PASSWORD
          command:
            - sh
            - -c
            - >-
              curl -sS --user "default:$CH_PASSWORD"
              --data-binary "SHOW CREATE TABLE default.otel_logs"
              http://krateo-clickstack-clickhouse-clickhouse-headless.krateo-system.svc:8123/
```

```sh
kubectl apply -f check-otel-ttl.yaml
kubectl wait --for=condition=complete job/check-otel-ttl -n krateo-system --timeout=60s
kubectl logs -n krateo-system job/check-otel-ttl | grep -i TTL
```

Expected after a fresh create at `2160h`:

```sql
TTL toDateTime(Timestamp) + toIntervalDay(90)
```

To change retention on tables that already exist, the TTL has to be altered in place
(this is a ClickHouse operation; no chart does it for you):

```sql
ALTER TABLE default.otel_logs   MODIFY TTL toDateTime(Timestamp) + toIntervalDay(90);
ALTER TABLE default.otel_traces MODIFY TTL toDateTime(Timestamp) + toIntervalDay(90);
```

Set the chart value in the same change, or the next fresh install silently goes back
to the old number.

## The footgun

`ttl: 0s` is accepted and means **no TTL clause at all** — unbounded growth, i.e. the
exact pre-incident state. `GenerateTTLExpr` returns an empty string for any value
`<= 0`. There is no "unlimited but bounded" setting; if you want no retention, you are
choosing the 2026-07 failure mode deliberately.

## Per-signal retention

Traces at 7 days and audit records at 90 are different requirements, and ClickHouse
expresses both. The exporter supports it: each signal's exporter creates **only its own
signal's tables** at `start()` (`createLogsTable`, `createTraceTables`), so two named
instances with different `ttl` values produce two tables with different retention.

```yaml
# Settable from the Installer CR as of installer#500 — see below.
exporters:
  clickhouse/logs:
    endpoint: *endpoint
    logs_table_name: otel_logs
    create_schema: true
    ttl: 2160h          # 90d — audit-grade
  clickhouse/traces:
    endpoint: *endpoint
    traces_table_name: otel_traces
    create_schema: true
    ttl: 168h           # 7d — traces are the volume
service:
  pipelines:
    logs:
      exporters: [clickhouse/logs]
    traces:
      exporters: [clickhouse/traces]
```

**This now works from the Installer CR.** Both collector blocks carry a
`patternProperties` entry for `^clickhouse/[a-z0-9][a-z0-9_-]*$` whose shape is a
`$ref` to the sibling unnamed `clickhouse` subschema — three lines per collector
instead of the ~100 duplicated ones the alternative would have cost, and the first
`$ref` in this schema.

The `$ref` is genuinely enforced rather than decoratively present, checked against
helm's own validator:

| case | result |
|---|---|
| `clickhouse/audit` with `ttl: 8760h`, referenced from a pipeline | accepted |
| same on the daemonset | accepted |
| `clickhouse/audit` with `ttl: 365d` | **rejected on the pattern, through the ref** |
| `clickhoose/audit` (typo) | **rejected** — `additionalProperties` still guards |
| `clickhouse/Audit` (uppercase) | **rejected** — the pattern is lowercase-only |
| `clickhouse/audit` with an undeclared key | **rejected** — inherited through the ref |
| plain `clickhouse.ttl`, and no override at all | unchanged |

Each collector's `patternProperties` points at **its own** `clickhouse` block, not the
other's. They are identical today, so a crossed ref would validate the same and hide
until the two diverge.

Note also that the daemonset's `config.service.pipelines` has no `traces` key under
`additionalProperties: false` — a traces pipeline there would need one. The deployment
already permits any shape.

## Sizing input

Measured on krateo-057, 2026-10-20, for the `AuditRecord` corpus specifically: mean
payload 767 B, 52.8 records/day gross. At that rate 90 days is ~3.6 MB raw and a year
is ~14.8 MB — before ClickHouse compression, against 8.4 GB free. Audit retention is
not what sizes this volume; **traces and pod logs are.** Size the disk against those,
and use audit's cheapness as the reason it can afford the longer TTL.
