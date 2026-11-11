---
type: Architecture
title: installer — AUDIT-5 admission records for direct writes
description: Design for installer#411. Why an admission record is a record of an accepted ATTEMPT and not of a write, why server dry-runs must be dropped rather than labelled, why the completeness signal cannot come from the webhook itself, and the acceptance cases #411 does not list (nameless deletecollection, impersonation, installer#781's hook Jobs, status subresource volume).
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [design, audit, admission-webhook, audit-5]
timestamp: 2026-11-11T00:00:00Z
---

# AUDIT-5 — admission records for direct writes to `*.krateo.io`

Design for installer#411. Parent #391. Depends-on nothing; installer#781's hook Jobs are
acceptance cases for it.

**Status: design only.** No webhook is registered and no component is pinned by this document. It
exists so the failure-mode decisions are made before code, because every one of them is hard to
reverse once writes depend on it.

## What the issue asks for, and the one place its framing needs correcting

> A validating admission webhook on `*.krateo.io` resources emits an audit record for any write the
> apiserver accepts, whatever the origin.

**Admission does not see writes the apiserver accepts. It sees writes the apiserver is considering.**
That difference is the whole design, and getting it wrong replaces today's under-reporting with
over-reporting — which is worse, because an over-complete trail cannot be distinguished from a
correct one by inspection.

Three ways an admitted request never becomes a write:

| case | what admission sees | what is stored |
|---|---|---|
| `--dry-run=server` | a full `AdmissionReview` with `dryRun: true` | nothing |
| a later webhook denies | our ALLOW, then someone else's DENY | nothing |
| etcd conflict / precondition failure after admission | our ALLOW | nothing |

So **an admission record is a record of an accepted *attempt*.** That is still strictly more than
AUDIT-4's convention-based emission gives us, and it is the right thing to ship — but the record
must say so, and the trail's consumers must know it. This is why #409's `stage` pair exists; the
Kubernetes audit log has `RequestReceived` / `ResponseComplete` for exactly this reason.

**Decision: emit one record at admission with `stage: admission`, and do NOT attempt to confirm the
write.** Confirming would need a second observation path (informer or the apiserver audit log),
which is a larger project and reintroduces a different completeness question. Over-reporting that is
labelled is honest; over-reporting that is silent is not.

**`dryRun: true` MUST be dropped, not labelled.** A dry run is not an attempt to write — it is
explicitly a request not to. Recording it would make every `helm template --dry-run=server` and
every `kubectl diff` appear in the trail, and the installer's own render path uses server dry-runs
constantly (the cdc renders through Helm's upgrade action every reconcile). This is the single
highest-volume false-positive source and it is a one-line guard.

## failurePolicy

`Ignore`, as the issue recommends, and the reasoning is worth recording because it will be
challenged:

- `Fail` makes the audit webhook a **write-path dependency for every `*.krateo.io` CR**. Our own
  control plane writes those constantly — every cdc reconcile, every composition status update. A
  webhook outage would stop the platform reconciling, which is a self-inflicted outage in exchange
  for an audit guarantee.
- `Ignore` leaves a gap, but a **measurable** one, which is strictly better than today's implicit
  gap.

Revisit only if a compliance requirement forces `Fail`, and if it does, pair it with
`timeoutSeconds` at the floor and a `namespaceSelector`/`objectSelector` that excludes the platform's
own service accounts — otherwise the first webhook restart takes the platform down.

## The completeness signal — and why the webhook cannot be its own source

> a counter of skipped admissions, webhook downtime, or emit failures

**A counter inside the webhook cannot count the admissions it never saw.** If the webhook is down,
it is not running, so it emits nothing *and* counts nothing: the gap is invisible precisely when it
exists. This is the "measured absence is not an invariant" trap in its purest form — the instrument
and the subject fail together.

**The only honest source for fail-open events is the apiserver**, which knows it gave up on us:

```
apiserver_admission_webhook_fail_open_count{name="<our webhook>"}
apiserver_admission_webhook_rejection_count{name="<our webhook>"}
apiserver_admission_webhook_admission_duration_seconds{name="<our webhook>"}
```

**OPEN CONSTRAINT, and it may invalidate the whole signal on our clusters: GKE does not expose the
full apiserver metrics endpoint.** Both 057 and krateo-demo are GKE. Before this design is accepted,
someone must establish whether `apiserver_admission_webhook_fail_open_count` is reachable there —
via GKE control-plane metrics, or not at all. If it is not:

1. the webhook's own emit-failure and emit-success counters still work for the *up* case;
2. downtime can be inferred from the webhook's own liveness/scrape gaps rather than from the
   apiserver, which is weaker but not nothing;
3. and the "is the trail complete for window X" answer must say *which* of the two sources it rests
   on, rather than implying both.

**Do not ship a completeness signal whose failure mode is silence.** A trail that reports complete
because its gap counter was also down is worse than one that admits it does not know.

## Acceptance cases the issue does not list

These come from behaviour measured on 057 and from installer#781.

1. **`deletecollection` reaches admission with NO object name.** It arrives as operation `DELETE`
   with an empty `request.name` (measured). A record built from `request.name` is therefore malformed
   or dropped for exactly the operation that removes the most objects. The record must either carry
   the selector or be explicitly marked as a collection delete.
2. **`kubectl delete --all` is NOT a deletecollection** — it lists, then issues individual deletes,
   each with a name. So the two look identical to an operator and different to admission.
3. **Impersonation.** `UserInfo` carries the impersonated identity, and the impersonator is in
   `request.userInfo.extra`. A record that keeps only the impersonated user loses who actually
   acted — the question an audit trail exists to answer.
4. **installer#781's four hook Jobs**, each of which writes `*.krateo.io` with shell `kubectl` and
   cannot emit from Go. The record must name the Job's ServiceAccount:
   `self-bootstrap` (post-install/post-upgrade), `installer-self-bootstrap` (pre-delete, deletes the
   installer CompositionDefinition), the cdc ServiceAccount (pre-delete, deletes Compositions and
   PVCs), and the post-delete cleanup Job.
5. **The platform's own write volume is the load risk.** This sits in front of every `*.krateo.io`
   write, and the cdc writes constantly — measured on 057: ~85 reconcile spans/min and ~84 status
   writes/min at idle, 135 managed children, 222 incidents, and several hundred widget CRs. The
   load measurement must use the platform's own idle write rate as the baseline, not a synthetic one.

## What the installer owns, and what it does not

The installer ships **registration and wiring**, not the webhook:

- the `ValidatingWebhookConfiguration` (rules, `failurePolicy`, selectors, `timeoutSeconds`),
  rendered behind a feature flag so it is off until deliberately enabled;
- the component pin for whatever implements the webhook;
- the `componentValues` node for its settings — which, because `componentValues` is
  `additionalProperties: false`, **must be declared in a tag that ships BEFORE anything sets it**,
  or the keys are silently pruned with every check green.

The webhook implementation and the record schema belong with the audit library
(krateo-platformops/audit), not here.

## Open questions for Diego

1. **Is `apiserver_admission_webhook_fail_open_count` reachable on GKE?** If not, the completeness
   signal rests on a weaker source and that should be an explicit decision rather than a discovery.
2. **Does the trail want admission *attempts*, or only confirmed writes?** Attempts are what
   admission can give cheaply and honestly. Confirmed writes need a second observation path.
3. **Scope of `*.krateo.io`:** all groups, or exclude the high-volume internal ones
   (`composition.krateo.io` status writes) to cut load? Excluding them is a real audit gap and
   should be named as one if chosen.
