---
type: Log
title: installer — log
description: Curated chronological history of the umbrella installer since its migration to krateo-platformops — notable changes and decisions, newest first; release notes stay in GitHub Releases.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [history]
timestamp: 2026-08-07T00:00:00Z
---

# Log

Curated history, newest first. This repo starts at the 2026-08-04 migration of the
umbrella chart into `krateo-platformops` (0.3.1); the pre-migration 0.2.x line lived
in the predecessor personal-org repo and is not mirrored here.

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
