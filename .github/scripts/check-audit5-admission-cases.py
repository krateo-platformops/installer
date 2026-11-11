#!/usr/bin/env python3
"""AUDIT-5 (installer#411) acceptance harness: the AdmissionReview shapes a record builder must
survive.

This is a TEST HARNESS, not the webhook. It encodes the cases that are easy to get wrong and are
NOT in #411's acceptance list, so that whatever implementation lands can be held to them before it
is wired to a cluster. It runs against a pluggable `build_record` callable; the reference
implementation here is deliberately minimal and exists only to prove the harness discriminates.

WHY A HARNESS FIRST. Every case below was either measured on a live cluster or comes from
installer#781's hook-Job inventory. A webhook that passes a happy-path create/update test and fails
these produces a trail that is confidently wrong -- which is worse than no trail, because a query
returning no records is then indistinguishable from nothing having happened.

Run: python3 .github/scripts/check-audit5-admission-cases.py
"""
import json, sys

# --- fixtures: minimal AdmissionReview requests, each one a real shape ------------------

def review(op, kind, name, *, ns="krateo-system", user="kubernetes-admin", groups=None,
           dry=False, extra=None, uid="req-1", subresource=None):
    r = {
        "uid": uid,
        "operation": op,
        "kind": {"group": "composition.krateo.io", "version": "v0-1-0", "kind": kind},
        "resource": {"group": "composition.krateo.io", "version": "v0-1-0",
                     "resource": kind.lower() + "s"},
        "namespace": ns,
        "name": name,
        "userInfo": {"username": user, "groups": groups or ["system:authenticated"]},
    }
    if dry:
        r["dryRun"] = True
    if extra:
        r["userInfo"]["extra"] = extra
    if subresource:
        r["subResource"] = subresource
    return r


CASES = []

def case(label, request, expect, why):
    CASES.append({"label": label, "request": request, "expect": expect, "why": why})

# 1. the happy path, so a harness failure is not just "everything fails"
case("create by a human",
     review("CREATE", "Portal", "portal"),
     {"emit": True, "actor": "kubernetes-admin"},
     "baseline: an ordinary accepted attempt must produce exactly one record")

# 2. DRY RUN -- the highest-volume false positive. The cdc renders through Helm's upgrade action
#    every reconcile, and those are server dry-runs.
case("server dry-run must NOT emit",
     review("CREATE", "Portal", "portal", dry=True),
     {"emit": False},
     "a dry run is an explicit request NOT to write; recording it puts every "
     "`helm template --dry-run=server` and `kubectl diff` in the trail")

# 3. DELETECOLLECTION -- measured on 057: reaches admission as DELETE with an EMPTY name.
case("deletecollection: DELETE with no name",
     review("DELETE", "Portal", ""),
     {"emit": True, "collection": True},
     "measured: deletecollection reaches admission as DELETE with request.name empty, so a record "
     "keyed on name is malformed or dropped for the operation that removes the most objects")

# 4. impersonation -- the impersonator must survive into the record
case("impersonation keeps the impersonator",
     review("UPDATE", "Portal", "portal",
            user="impersonated-user",
            extra={"originalUser": ["real-operator"]}),
     {"emit": True, "actor": "impersonated-user", "on_behalf_recorded": True},
     "a record keeping only the impersonated identity loses who actually acted, which is the "
     "question an audit trail exists to answer")

# 5..8. installer#781's hook Jobs: shell kubectl, no Go, so admission is their ONLY coverage
for sa, when in (("installer-self-bootstrap", "pre-delete deletes the installer CompositionDefinition"),
                 ("installer-self-bootstrap", "post-install/post-upgrade self-bootstrap"),
                 ("installer-core-provider", "pre-delete deletes Compositions and PVCs"),
                 ("installer-self-bootstrap", "post-delete cleanup")):
    case(f"hook Job as {sa} ({when})",
         review("DELETE", "Portal", "portal",
                user=f"system:serviceaccount:krateo-system:{sa}",
                groups=["system:serviceaccounts", "system:authenticated"]),
         {"emit": True, "actor": f"system:serviceaccount:krateo-system:{sa}"},
         "installer#781: these write *.krateo.io with shell kubectl and cannot emit from Go, so "
         "admission is their only coverage and the record must name the Job's ServiceAccount")

# 9. a subresource write -- status updates are the platform's highest-volume write
case("status subresource is identified as such",
     review("UPDATE", "Portal", "portal", subresource="status"),
     {"emit": True, "subresource": "status"},
     "measured ~84 status writes/min at idle on 057; if these are not distinguishable the trail is "
     "dominated by them and the scope decision in the design cannot be made from data")


# --- reference builder: minimal, and deliberately NOT the real implementation ------------

def build_record(req):
    """Returns None to mean 'do not emit'."""
    if req.get("dryRun") is True:
        return None
    ui = req.get("userInfo") or {}
    rec = {
        "actor": ui.get("username"),
        "verb": req.get("operation", "").lower(),
        "group": (req.get("resource") or {}).get("group"),
        "resource": (req.get("resource") or {}).get("resource"),
        "namespace": req.get("namespace"),
        "name": req.get("name") or None,
        "stage": "admission",
    }
    if not req.get("name"):
        rec["collection"] = True
    if req.get("subResource"):
        rec["subresource"] = req["subResource"]
    extra = ui.get("extra") or {}
    if extra.get("originalUser"):
        rec["on_behalf_of"] = extra["originalUser"][0]
    return rec


def check(c):
    rec = build_record(c["request"])
    exp = c["expect"]
    if exp.get("emit") is False:
        return (rec is None, f"expected no record, got {rec!r}")
    if rec is None:
        return (False, "expected a record, got none")
    for k, v in exp.items():
        if k == "emit":
            continue
        if k == "on_behalf_recorded":
            if not rec.get("on_behalf_of"):
                return (False, "impersonator not recorded (on_behalf_of missing)")
            continue
        if k == "collection":
            if rec.get("collection") is not True:
                return (False, "nameless delete not marked as a collection")
            continue
        if rec.get(k) != v:
            return (False, f"{k}: expected {v!r}, got {rec.get(k)!r}")
    return (True, "")


def main():
    fails = 0
    print(f"AUDIT-5 admission acceptance harness: {len(CASES)} cases\n")
    for c in CASES:
        ok, why = check(c)
        print(f"  {'PASS' if ok else 'FAIL'}  {c['label']}")
        if not ok:
            print(f"        {why}")
            print(f"        rationale: {c['why']}")
            fails += 1

    # THE HARNESS MUST BE ABLE TO FAIL. A suite that passes against a broken builder proves
    # nothing -- the same defect as a gate that reports OK over zero rows.
    print("\n  mutation check (a broken builder MUST be caught):")
    global build_record
    good = build_record
    def emits_dry_runs(req):
        r = good(req)
        if r is None and req.get("dryRun"):
            return {"actor": "x", "stage": "admission"}   # the exact bug we care about
        return r
    build_record = emits_dry_runs
    caught = sum(0 if check(c)[0] else 1 for c in CASES)
    build_record = good
    print(f"    builder that emits dry-runs -> {caught} case(s) failed "
          f"({'OK, detected' if caught else 'NOT DETECTED -- harness is vacuous'})")
    if caught == 0:
        fails += 1

    print()
    if fails:
        print(f"::error::audit5-admission-cases: {fails} failure(s)")
        return 1
    print(f"audit5-admission-cases: OK -- all {len(CASES)} cases pass and the harness "
          f"detects a broken builder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
