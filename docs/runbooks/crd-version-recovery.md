---
type: Runbook
title: CRD version recovery — when an API read lies to you
description: Recovering a composition CRD whose version structure has been damaged, and the measured reason every ordinary API read may under-report during that recovery — pruning follows the request version's schema on reads as well as writes, and on GKE there is no etcd to appeal to.
tags: [runbook, crd, versions, recovery, apiserver]
timestamp: 2026-10-27T00:00:00Z
---

# CRD version recovery — when an API read lies to you

Covers two failures that share one mechanism: **the apiserver's version-dependent handling
decides what you see, and the stored document is not what you get.** In both cases every
ordinary read is wrong and the data is right.

Sources: core-provider#207 (the `vacuum` loss), installer#524 (the pruned field), and the
kind 1.36.4 measurements behind core-provider#188.

## Read this first: the instrument problem

**Pruning follows the request version's schema — on reads as well as writes.** An object
created at a wide version and fetched through a narrower served version comes back *already
missing* fields that are still in storage and still visible through the wide endpoint.

So during a recovery the API will actively tell you data is gone that is present. Any step
that reads an object to decide whether to proceed is unsafe unless it reads through a version
that declares the field.

### On GKE that appeal is not available. Measured on krateo-057:

```
control-plane pods visible in kube-system      0   (no etcd, no kube-apiserver — managed)
composition.krateo.io CRDs                    68
  with more than one SERVED version             0
  `vacuum` served                              false (storage version, every one of them)
  `vacuum` .spec x-kubernetes-preserve-unknown-fields: true   59 of the 59 that have it
```

Put together: there is **exactly one served version per CRD**, `vacuum` is never served, and
there is no etcd endpoint. An undeclared field under `spec` is therefore **preserved in storage
and unreadable by any means available on the cluster**. The etcd read that settled
installer#524 was run on kind, where the control plane is local. It cannot be repeated on 057
or on the demo parent.

**Consequence for this runbook:** on a managed cluster you cannot distinguish *pruned on read*
from *absent in storage*. Do not write a recovery step whose branch depends on telling those
apart. Where you must, see [break-glass](#break-glass-making-the-storage-version-readable).

### The trap in the other direction

A **schema default** at the request version can defeat a removal just as pruning defeats a
read. On installer 0.3.429 a `{"acceptance": null}` merge patch is a **no-op**, because that
CRD re-defaults `s6Probe=false` and the apiserver re-materialises the subtree the patch meant
to remove. The patch succeeds, changes nothing, and invites the conclusion that the field was
already gone.

So: **a successful write is not evidence the field is gone, and a clean read is not evidence
it was ever there.** Verify a removal by rendering what the chart now declares, not by reading
the object back.

## Failure A — a field you expect is missing from a read

Symptom: a field that should be on the object is absent, or a patch that should have removed
one appears to have done nothing.

1. Identify the version the field is declared at — `kubectl get crd <crd> -o json` and look
   for it in each `spec.versions[].schema`.
2. Read through **that** version explicitly: `kubectl get <plural>.<version>.<group> <name>`.
   An unqualified `kubectl get` routes to the client-preferred served version, which may not
   declare the field.
3. If no **served** version declares it, stop. On a managed cluster you have reached the limit
   of what is observable; treat the field as unknown rather than absent.

This is also why a write that must carry a new field has to be addressed through the
version-qualified GVR — an unqualified `apply` or `patch` is pruned at the preferred version
and the value silently never persists.

## Failure B — a CRD has lost `vacuum`

Symptom: the Kind becomes unlistable and reads fail with `invalid group/version`. In an ArgoCD
estate this takes out the whole cluster cache, not just the affected Applications.

### How to tell this apart from the normal state, which looks similar

A CRD with **one** version that is both `served` and `storage` has no `vacuum` and is
**healthy** — `vacuum` is injected only when a *second* version is appended, so a component
that has never been version-bumped simply does not have one. On krateo-057 **9 of 68**
composition CRDs are in that state and all are fine.

The damaged state is narrower: **objects encoded at a version the CRD no longer declares.**
Check `status.storedVersions` against `spec.versions[].name` — damage is a stored version with
no matching declared version.

```
healthy, never bumped   versions=[(v0-1-0, served, storage)]   storedVersions=[v0-1-0]
healthy, bumped         versions=[vacuum(storage), vX(served)] storedVersions=[vacuum]
DAMAGED                 storedVersions contains a name absent from spec.versions
```

### So "`vacuum` is absent" is NOT the damage state, and any repair keyed on it does harm

This matters beyond diagnosis, because it is the difference between a correct fix and a destructive
one. `vacuum` is the storage version in **59 of the 59** CRDs that have one on krateo-057 — zero
exceptions. So restoring it unconditionally, on the grounds that the injection is idempotent,
necessarily **demotes a never-bumped CRD's only version to `storage: false` and adds `vacuum` to
`status.storedVersions`** — which the apiserver will not release without a storage migration.

On 057 that would permanently alter **9 healthy CRDs** to repair a state that occurs **0** times.

A repair must therefore key on `status.storedVersions` naming a version `spec.versions` does not —
never on the absence of `vacuum`. "Idempotent" is a claim about a function, not about a cluster; the
question it skips is what the function does to the objects that are *not* in the failure state.

### Why it never self-heals

`vacuum` is constructed in exactly one place — `generation.go:85-87` inside `AppendVersion` —
behind `if !exist` (the version being added is not already present) and then `if !vacuum`.
`AppendVersion` has one caller, `crd.go:220`, and since core-provider#192 that call sits behind
its own presence check. Reaching the injection therefore requires the version to be absent
**twice over**. A CRD that already carries the version being reconciled but has lost `vacuum`
is never repaired, on any reconcile, forever.

Nothing in current core-provider removes `vacuum` — `RemoveStaleVersions` excludes it, the
apiserver refuses to drop a storage version still in `status.storedVersions`, and #194 retains
the hub's. The damage always arrives from outside: a hand-applied or `kubectl apply`-replaced
CRD, a restore from backup, a wholesale copy between clusters, or an upgrade from a
core-provider old enough never to have added it.

### Recovery

**From core-provider 2.13.32 the `vacuum` case is automatic.** A CRD whose
`status.storedVersions` names `vacuum` while `spec.versions` does not is repaired on the next
reconcile, with the single-storage-version bookkeeping handled in the same apply
(core-provider#209). Verified pullable: chart and `core-provider-crds` both at 2.13.32.

**The two manual routes below stay correct and stay necessary**, for two cases the automatic repair
deliberately does not cover:

- an orphaned **chart** version — #209 declines to guess at a schema it cannot synthesize, which is
  the right call: a wrong schema would validate writes against something nobody authored
- recovery on a **managed cluster**, where the instrument that would confirm the repair does not
  exist (see the measurements above)


**Route 1 — preferred. Append a version the CRD has never seen.** Bump the component's pin to
a version not previously registered. That trips `!exist` on both guards and `vacuum` is
restored as a side effect.

This is the counter-intuitive one — *the fix is to add something unrelated* — and nobody
guesses it. Prefer it anyway, because it goes **through the controller**: no hand-edit of a
CRD the platform owns, which is the rule this runbook will not ask you to break.

**Route 2 — break-glass. Patch `vacuum` back by hand.** It must clear the other version's
`storage: true` in the same apply: the apiserver enforces exactly one storage version and
rejects the patch otherwise. Same invariant `syncHubCompositionCRDVersions` handles.

Hand-editing a generated CRD is break-glass precisely because the next reconcile is entitled to
disagree with you. Use Route 1 unless the Kind's unlistability is itself blocking the pin bump.

### Break-glass: making the storage version readable

If you genuinely must see what is in storage on a managed cluster, flipping `vacuum` to
`served: true` makes the preserve-unknown schema readable, since `vacuum` declares
`spec.x-kubernetes-preserve-unknown-fields: true`.

This is a hand-edit of a platform-generated CRD and it changes what the API serves for every
client, so it is a last resort, done deliberately, and reverted. Read, revert, then decide —
do not leave a second served version in place while anything else reconciles.

## What not to do

- **Do not conclude a field is absent from one read.** State which version you read through,
  every time; an unqualified read names no version and proves nothing about an undeclared field.
- **Do not delete a CompositionDefinition to force a rebuild.** That uninstalls that version's
  Compositions — correct by design, and destructive.
- **Do not trust a successful merge patch as evidence of removal** (the `s6Probe` default above).
- **Do not read an object straight after a delete and call it failure.** Deletion through an
  operator's finalizer is asynchronous; poll, and distinguish *present with a
  `deletionTimestamp`* (the delete was issued, finalization is incomplete) from *present with
  none* (the delete never happened). Those point at different layers, and a single read races
  the finalizer in the direction that looks like a product bug.

## Verifying you are done

- `status.storedVersions` ⊆ `spec.versions[].name`.
- The Kind lists: `kubectl get <plural>.<group> -A` returns without `invalid group/version`.
- `vacuum` is present, `storage: true`, `served: false`, and is the only storage version.
- The component's Composition is `Ready` and `Synced`.

**Assert the check found something.** When surveying CRDs for this, read
`spec.versions[].schema.openAPIV3Schema.properties.spec.x-kubernetes-preserve-unknown-fields`
— **not** the root. The flag sits on `spec`, not at the top level, and a survey reading the root
returns `0` across all 68 CRDs on a healthy cluster: a false negative that looks exactly like a
clean finding. That mistake was made while writing this runbook.

## Failure C — `installer-platform` loops `pending-rollback` after a pin bump pruned a version

**EXECUTED AND VERIFIED on the demo parent, 2026-10-27.** Unlike the two routes above, this
procedure has been run, and the honest scope is stated at the end.

Symptom: a new `pending-rollback` revision every ~6 minutes, each targeting the previous stuck one,
and the controller logging

```
rollback failed: unable to build kubernetes objects from current release manifest:
resource mapping not found for name "frontend" ... no matches for kind "Frontend"
in version "composition.krateo.io/v1-8-11"
```

while the Installer CR reports `Ready=False`, *"waiting for an in-flight helm operation to settle"*.

### The pruning is NOT the fault

A `1.8.11 -> 1.8.12` pin bump prunes `v1-8-11` from the `frontends` CRD. **That is normal:**
krateo-057 did exactly the same on the same step and did not wedge — its compositions were recreated
at `v1-8-12` within the reconcile and its release never went pending. The fault is an *interrupted*
upgrade leaving the release `pending-rollback`, after which the rollback cannot build a manifest that
references the pruned version.

### Why it never self-heals

```
composition.go:1244   isIncompleteHelmOperation = {pending-install, pending-upgrade,
                                                  pending-rollback, uninstalling}   -- `failed` NOT in it
composition.go:301    pendingFor < pendingOperationGrace (default 5m, env-overridable)
                        -> report the pending condition, return UpToDate, DO NOTHING
composition.go:328    past the grace -> Rollback once, targeting ReleaseVersion: rel.Revision
```

`Observe` enters stuck-release recovery and **early-returns before reaching the repair that exists
for this exact manifest problem**. plumbing's `dropUnservedFromStoredManifest` runs at
`reconcile.go:109`, step **1.5 of the UPGRADE path**. The `hc.Rollback` call never reaches it. So the
loop cannot repair itself however long it runs, and **a controller restart does not clear it.**

### DO NOT re-serve the pruned version

It would work, and that is the danger. The thing failing is the *rollback*, so making it buildable
makes the rollback **succeed** — reverting the whole umbrella to the old release's pin set and then
rendering compositions at a version that no longer exists. Same lesson as the portal repair: clearing
a blocker while a teardown is pending **authorises the teardown**. Go forward, never back.

### The procedure

1. **Wait for a NEW pending revision to appear.** You are then inside the grace period, during which
   the controller sets the pending condition and takes no action — a window you can derive rather
   than guess at. Check `pendingGraceEnvvar` on the `installers-v<version>-controller` deployment
   rather than assuming 5 minutes.
2. **Back up the DEPLOYED revision's secret first.** Step 1.5 does not read the stored manifest, it
   **rewrites it in place** — `rel.Manifest = repaired; actionConfig.Releases.Update(rel)`, on
   `Releases.Deployed(releaseName)`. Skip this and the pristine release is gone.
3. **Back up, then delete, the `pending-rollback` secrets** — inside that window, mode 600.
4. The newest revision is now the deployed one, so `isIncompleteHelmOperation` is false, `Observe`
   reaches the upgrade path, step 1.5 sanitises the stored manifest, and the render proceeds.

Prefer deleting the pending secrets over flipping a status to `failed`: `failed` converges normally
(it is not in the set above), but editing it means rewriting a gzipped release payload, which is a
larger change than a delete — and restoring the two backed-up secrets reverts a delete exactly.

### Afterwards: the release is FORWARD-ONLY

The deployed revision's manifest has had the unmappable documents dropped, so **rolling back to it
will not recreate them.** With `helmMaxHistory = 3` (`composition.go:50`) there may be no revision
left that restores those compositions. Treat the cluster as having no rollback target until the
sanitised revision ages out of the retained three, and label any backup taken afterwards
**POST-SANITIZE** — a backup whose provenance is stated is usable; one whose provenance is assumed
traps whoever restores it expecting the old release.

### Never ship an engine fix and the operation it fixes in the same upgrade

Generalised from the acmp session's analysis while planning the re-home, and it applies well beyond
this one case.

A single `helm upgrade` that **both** bumps core-provider **and** changes something that triggers a
CompositionDefinition move **races the fix against the thing it fixes.** The core-provider Deployment
rolls during the upgrade, and the old pod keeps serving reconciles until the new one is Ready — while
the post-upgrade hook has already patched the CR, so `installer-platform` re-renders and performs the
create-then-delete. If the **old** engine is still the active reconciler at that moment, the race is
reachable again and the new engine's fix is irrelevant.

**Split it into two upgrades:**

```
phase 1   bump the engine ONLY -- the moving entry unchanged
          wait for the new core-provider to be rolled AND the platform settled
phase 2   the same chart, adding only the namespace / readerGroups change
```

**There is a second, independent and worse reason, and #212 does NOT cover it** (core-provider#214).
Collapsing the phases does not merely race the engine bump — it produces a CRD **deletion**:

```
skipCRD = len(lst) > 1                 from GetCompositionDefinitions(GroupKind)  -- VERSION-AGNOSTIC
                                       but filtered on Status.CurrentGVK() -- so status-keyed
successor                              matched on the GVK being torn down        -- VERSION-SPECIFIC
gate:  if skipCRD || successor != nil  -> keep the CRD
```

A sibling definition on the **same Kind at a different version** is exactly who the CRD is being kept
for — and it is *correctly* not a "successor" for the version being torn down. So:

```
CD-old at v0-2-45 deleted; CD-new at v0-2-46 exists, not yet reconciled
  lst misses CD-new (no status)      -> skipCRD = false
  successor for v0-2-45              -> nil  (CD-new targets v0-2-46)
  Undeploy SkipCRD=false             -> CRD DELETED, taking CD-new's Kind with it
```

**Which path the installer can reach, measured.** The installer declares **65 distinct Kinds with
zero overlap** — no two component pins share a `kind`, so it never emits two definitions on one Kind
by itself. The only operation that creates a transient second definition for one Kind is the
installer#522 re-home. Therefore:

| what you do | the two definitions | outcome |
|---|---|---|
| version bump alone | one definition | not reachable |
| namespace move alone (phase 2) | **same** version | `successor` found ⇒ `\|\| successor != nil` keeps the CRD — **covered by #212** |
| both in one upgrade | **different** versions | successor nil, `skipCRD` false ⇒ **CRD deleted — NOT covered** |

So the two-phase split prevents this as well as the engine race, and the collapsed form is worse than
a race: a race leaves you diagnosing a teardown, a deleted CRD takes the Kind with it.

Worth noting #212's own comment at `compositiondefinitions.go:1803` already says `skipCRD` "misses a
successor that has not reconciled yet" and relies on `successor != nil` to cover it. That reasoning
is correct for the same-version case and silently incomplete for the different-version one — a
disjunction whose second term does not span the first term's gap. The same shape as the conjunction
in #210, one gate over.

**FIXED AND SHIPPED: core-provider#217, in 2.13.35.** Verified as *contained in the tag*, not inferred
from tag ordering: GitHub's compare API puts #217's merge commit `89357e0` **ahead** of `2.13.34` (so
2.13.34 does not contain it — as recorded here before) and **behind** `2.13.35` (so 2.13.35 does). The
shipped gate at that tag no longer counts definitions at all:

```go
skipCRD := keep.keep              // a dedicated reference check (controllerowner.go), Kind+version aware
switch {
case skipCRD:                     // "another CompositionDefinition still needs this Kind", with referencedBy
case successor != nil:            // "handing this version over"
default:                          // delete
}
```

The two reasons to keep a CRD are now *separate branches that say which one applied*, instead of a
disjunction where either term silently covering for the other is indistinguishable from neither
covering. **The two-phase re-home is still the procedure below 2.13.35**, and above it the split is no
longer load-bearing for this mechanism — but it remains load-bearing for the other two (the engine-roll
race, and #218 next).

### Still open: a move plus a chart-version bump loses the instances (core-provider#218)

A third mechanism, **not** fixed by #212 or #217 and **open** as of 2026-10-27. A definition that is
moved *and* chart-version-bumped in **one apply** uninstalls its instances before the successor adopts
them. The bound is specific and worth holding onto, because it is what makes the two-phase split still
mandatory:

| what you do | #218 |
|---|---|
| namespace move alone | **safe** — chartRefs compare url + repo + **version**, so the ref is unchanged and the successor is recognised |
| move + chart version bump in one apply | **instances lost** |

So phase 2 of the re-home must carry the namespace/readerGroups change **and nothing else**. A chart
version bump smuggled into that same apply re-opens a hole that neither shipped fix covers.

The general rule: **a fix only protects operations that begin after it is the one running.** Shipping
both in one transaction means the protected window and the dangerous window overlap, and which one
wins is a scheduling accident. This is the deployment-time twin of the status-write window above — in
both cases the fix exists and the question is only whether it is *in effect yet*.

### Scope, stated so nobody over-cites this

The run exercised the **sanitize plus a plain create**. Step 4.7's *adopt* never ran, because the
dropped compositions were not live — the step 1.5 comment assumes they are. So this is not evidence
the adopt path works.

## The same shape one layer down: a controller's own write window

Everything above is the **apiserver's** version-dependent handling deciding what a reader sees.
core-provider has a cousin: **its own status-write window deciding what a gate sees.** Worth knowing
because the symptom is a teardown that looks malicious rather than a read that looks empty.

A helm upgrade that moves a CompositionDefinition (installer#522's re-home shape) creates the new
definition and deletes the old one within about a second. The successor lookup filtered on
`cd.Status.CurrentGVK()`, so a definition whose status had not yet been written was **invisible** to
it, and the delete path took the last-definition branch: uninstalled the instances, then removed the
CRD. Demonstrated on kind against installer 0.3.457 — a live composition instance and its CRD were
deleted.

It was not introduced by the hand-over work, which is the part worth carrying:

```
2.13.30   if remote || len(lst) == 1                        -> FIRES
2.13.31   if successor == nil && (remote || len(lst) == 1)  -> FIRES
```

`successor` was resolved by the same status-keyed mechanism, so both terms of the conjunction went
false-negative together. **Two signals that share a failure mode are one signal wearing a second
hat** — narrowing a gate along an axis that shares the fault narrows nothing. So pinning backwards
protected nothing; every version carried it.

**Fixed in core-provider#212, shipping as 2.13.33:** the successor is found from
`cd.Spec.Chart` — a **spec-derived** signal that exists before any status is written — and both
halves of the destructive path are gated (`if skipCRD || successor != nil`). Equality on the chart
ref is used only to *add* a successor, never to rule one out, so the failure direction stays "miss a
successor", never "invent one".

**The hold is LIFTED on core-provider >= 2.13.33, and only there.** Proven end to end on 2026-10-27,
first on a demo-sized disposable GKE cluster and then on the real demo parent — a `selfservice-krateo`
re-home across namespaces, with the objects the bug destroys left intact:

```
acme    uid f0e51514   UNCHANGED, Ready, no deletionTimestamp
globex  uid 369e0ba7   UNCHANGED, Ready, no deletionTimestamp
CRD     uid 2cc2d8ec   UNCHANGED, not terminating
old CD began deleting 11:41:04Z   new CD Ready ~11:41:30Z   cdc + CRB subject moved
real tenants can list the definition ONLY in the new namespace, not in krateo-system
```

**Below 2.13.33 the hold still stands** — installer 0.3.455/0.3.456 pin core-provider 2.13.30 and
0.3.457 pins 2.13.31, all of which carry the race. Their release notes are annotated accordingly.

**And it only holds with the two-phase sequence** above: a single upgrade that both bumps the engine
and adds the `namespace` key races the fix against the operation. One demonstration is not a
controlled pair — we have one run showing 2.13.33 is safe and one earlier run showing 2.13.30 is not,
on different clusters. That is enough to proceed and not enough to call the race characterised.

One caveat the acmp session volunteered against its own result, worth keeping: core-provider runs at
INFO while both the hand-over and destructive-path lines are DEBUG, **so their absence from the log
proves nothing.** The verdict rests on the uids and the live state. A detector that cannot fire reads
exactly like a system that did nothing wrong.

Ordinary component pin bumps and engine bumps were never affected — they update a definition in place
and delete/recreate the *composition CR*, never the definition. Measured across that path on the
0.3.458 roll: exactly 2 of 83 composition uids changed, and they were the two components whose
version moved.

**Do not pin backwards to avoid any of this.** Below core-provider 2.13.29 the #199
standalone-composition protection is lost, and that is live and load-bearing on krateo-057.

**One sibling gate still reads status only** (core-provider#212's review): the reference count behind
served-version retirement. An unreconciled definition targeting an older version is invisible to it,
so that version can be pruned out from under it — self-correcting, since the version is re-appended
on the next reconcile, and #189's unaccounted-instance gate blocks pruning while any instance is
unaccounted for. If a teardown ever does something inexplicable, that call site and the successor
lookup are the two places to look.

## See also

- [Upgrade and rollback](./upgrade-rollback.md) — the CR is the source of truth, and why
  `helm rollback` is the wrong tool.
