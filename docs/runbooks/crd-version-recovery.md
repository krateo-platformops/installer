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

## See also

- [Upgrade and rollback](./upgrade-rollback.md) — the CR is the source of truth, and why
  `helm rollback` is the wrong tool.
