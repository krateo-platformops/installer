#!/usr/bin/env python3
"""Capture a roll baseline that can actually be DIFFED afterwards, and diff two captures.

WHY THIS EXISTS. On 2026-11-16 I rolled krateo-057 to 0.3.671 with what I thought was a thorough
pre-roll baseline: the installer's helm revision, the release COUNT, the composition count, the
condition census, pod states and the specific component versions that had to move. Every one of
those was useful. Then frontend asked me to confirm their portal-line signature:

    all 17 BuilderPublish compositions at composition.krateo.io/v1-8-129   -> I could answer
    each release's latest revision exactly ONE above its pre-roll revision -> I COULD NOT
    status.managed unchanged per composition                               -> I COULD NOT

The two I could not answer were not harder to measure. I simply had not captured the PER-OBJECT
values, only the aggregates. And the current revisions are not uniform (72, 73, 75, 76, 79, 80, 81,
83, 84, 87, 217 across the 17), so there is no shortcut and no way to reconstruct them: I would be
guessing at sixteen different previous numbers.

A BASELINE IS ONE-SHOT. The window to capture it closed when helm returned, and a predicate with no
pre-roll value cannot be checked afterwards -- not with more effort, not at all. frontend's reply
is the rule: "a predicate with no pre-roll value can't be checked afterwards." They are not counting
today's revision half as a pass, correctly.

WHAT IT CAPTURES, which is exactly what frontend asked for so their signatures become checkable:

    per helm release      name, namespace, chart, chart version, latest revision, status
    per composition       apiVersion, status.managed COUNT, Ready status + reason, Synced,
                          uid (so a recreate is distinguishable from an update)

The uid is mine, not theirs: without it "updated in place" and "deleted and recreated with the same
name" look identical in every other field, and I have already made that mistake today in the
opposite direction by reading creationTimestamp as a write time.

WHAT IT DELIBERATELY DOES NOT CAPTURE: anything inside a Secret. Secret NAMES and their data KEY
NAMES are recorded, never a value. For "was this credential touched", the answer is
`managedFields[].time` plus the fieldsV1 key set, which this tool records for Secrets it is asked
about -- and even then "written" is not "changed", because a controller re-applying identical bytes
produces the same trace.

    python3 hack/roll-snapshot.py capture --context <ctx> -o pre.json
    python3 hack/roll-snapshot.py capture --context <ctx> -o post.json
    python3 hack/roll-snapshot.py diff pre.json post.json
    python3 hack/roll-snapshot.py diff pre.json post.json --expect-release-revision-delta 1 \
        --releases-matching 'publish-'
    python3 hack/roll-snapshot.py --self-test
"""
import argparse
import json
import subprocess
import sys


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def capture(ctx, secrets=()):
    snap = {"context": ctx, "releases": {}, "compositions": {}, "secrets": {}}

    for rel in json.loads(_run(["helm", "--kube-context", ctx, "list", "-A", "-o", "json"]) or "[]"):
        snap["releases"]["%s/%s" % (rel["namespace"], rel["name"])] = {
            "chart": rel.get("chart"),
            "revision": str(rel.get("revision")),
            "status": rel.get("status"),
        }

    crds = json.loads(_run(["kubectl", "--context", ctx, "get", "crd", "-o", "json"]) or '{"items":[]}')["items"]
    for c in crds:
        if c["spec"]["group"] != "composition.krateo.io":
            continue
        plural = c["spec"]["names"]["plural"]
        if not [v for v in c["spec"]["versions"] if v.get("served")]:
            continue
        out = _run(["kubectl", "--context", ctx, "get",
                    "%s.composition.krateo.io" % plural, "-A", "-o", "json"])
        if not out:
            continue
        for o in json.loads(out).get("items", []):
            st = o.get("status") or {}
            conds = {x["type"]: "%s/%s" % (x["status"], x.get("reason", ""))
                     for x in (st.get("conditions") or [])}
            key = "%s/%s/%s" % (plural, o["metadata"]["namespace"], o["metadata"]["name"])
            snap["compositions"][key] = {
                "apiVersion": o["apiVersion"],
                "managed": len(st.get("managed") or []) if st.get("managed") is not None else None,
                "Ready": conds.get("Ready"),
                "Synced": conds.get("Synced"),
                "uid": o["metadata"].get("uid"),
            }

    # Secrets: metadata only. Never a value, and the data KEY NAMES only so a key appearing or
    # disappearing is visible.
    for ref in secrets:
        ns, _, name = ref.partition("/")
        out = _run(["kubectl", "--context", ctx, "get", "secret", name, "-n", ns,
                    "--show-managed-fields", "-o", "json"])
        if not out:
            snap["secrets"][ref] = {"present": False}
            continue
        s = json.loads(out)
        m = s["metadata"]
        snap["secrets"][ref] = {
            "present": True,
            "dataKeys": sorted((s.get("data") or {}).keys()),
            "resourceVersion": m.get("resourceVersion"),
            "writes": [{"manager": f.get("manager"), "op": f.get("operation"),
                        "time": f.get("time"), "fields": sorted((f.get("fieldsV1") or {}).keys())}
                       for f in (m.get("managedFields") or [])],
        }
    return snap


def diff(pre, post, expect_delta=None, matching=None):
    """Pure, so the self-test drives it without a cluster."""
    out = {"releases": {}, "compositions": {}, "secrets": {}, "problems": []}

    pr, po = pre.get("releases", {}), post.get("releases", {})
    out["releases"]["gone"] = sorted(set(pr) - set(po))
    out["releases"]["new"] = sorted(set(po) - set(pr))
    moved = {}
    for k in sorted(set(pr) & set(po)):
        a, b = pr[k], po[k]
        if a != b:
            moved[k] = {f: [a.get(f), b.get(f)] for f in ("chart", "revision", "status")
                        if a.get(f) != b.get(f)}
    out["releases"]["moved"] = moved
    if out["releases"]["gone"]:
        out["problems"].append("RELEASES DISAPPEARED: %s" % out["releases"]["gone"])
    bad_status = {k: v["status"] for k, v in po.items() if v.get("status") != "deployed"}
    if bad_status:
        out["problems"].append("RELEASES NOT deployed: %s" % json.dumps(bad_status, sort_keys=True))

    if expect_delta is not None:
        off = {}
        for k in sorted(set(pr) & set(po)):
            if matching and matching not in k:
                continue
            try:
                a, b = int(pr[k]["revision"]), int(po[k]["revision"])
            except (TypeError, ValueError):
                off[k] = ["unparseable", pr[k].get("revision"), po[k].get("revision")]
                continue
            if b - a != expect_delta:
                off[k] = [b - a, pr[k]["revision"], po[k]["revision"]]
        out["releases"]["revision_delta_not_%d" % expect_delta] = off
        if off:
            out["problems"].append(
                "REVISION DELTA != %d for %d release(s): %s"
                % (expect_delta, len(off), json.dumps(off, sort_keys=True)))

    pc, poc = pre.get("compositions", {}), post.get("compositions", {})
    out["compositions"]["gone"] = sorted(set(pc) - set(poc))
    out["compositions"]["new"] = sorted(set(poc) - set(pc))
    ch = {}
    for k in sorted(set(pc) & set(poc)):
        a, b = pc[k], poc[k]
        d = {f: [a.get(f), b.get(f)] for f in ("apiVersion", "managed", "Ready", "Synced", "uid")
             if a.get(f) != b.get(f)}
        if d:
            ch[k] = d
    out["compositions"]["changed"] = ch
    recreated = {k: v["uid"] for k, v in ch.items() if "uid" in v}
    if recreated:
        out["problems"].append(
            "COMPOSITIONS RECREATED (uid changed -- NOT an in-place update): %s"
            % json.dumps(recreated, sort_keys=True))
    if out["compositions"]["gone"]:
        out["problems"].append("COMPOSITIONS DISAPPEARED: %s" % out["compositions"]["gone"])

    ps, pos = pre.get("secrets", {}), post.get("secrets", {})
    for k in sorted(set(ps) | set(pos)):
        a, b = ps.get(k, {}), pos.get(k, {})
        if a.get("present") and not b.get("present"):
            out["problems"].append("SECRET DISAPPEARED: %s" % k)
            continue
        if a.get("dataKeys") != b.get("dataKeys"):
            out["problems"].append("SECRET KEY SET CHANGED: %s %s -> %s"
                                   % (k, a.get("dataKeys"), b.get("dataKeys")))
        if a.get("resourceVersion") != b.get("resourceVersion"):
            out["secrets"][k] = {
                "written": True,
                "note": ("resourceVersion moved, so it WAS written. This does NOT establish that "
                         "any VALUE changed -- a controller re-applying identical bytes produces "
                         "the same trace. Only a functional test with the credential can answer "
                         "that, and this tool never reads one."),
                "writes": b.get("writes"),
            }
    return out


def _self_test():
    PRE = {
        "releases": {
            "krateo-system/publish-a": {"chart": "builder-publish-1.8.128", "revision": "72", "status": "deployed"},
            "krateo-system/publish-b": {"chart": "builder-publish-1.8.128", "revision": "80", "status": "deployed"},
            "krateo-system/installer": {"chart": "installer-0.3.670", "revision": "579", "status": "deployed"},
        },
        "compositions": {
            "portals/krateo-system/portal": {"apiVersion": "composition.krateo.io/v1-8-128",
                                             "managed": 64, "Ready": "True/Available",
                                             "Synced": "True/ReconcileSuccess", "uid": "u-portal"},
        },
        "secrets": {"krateo-system/cj": {"present": True, "dataKeys": ["password"],
                                         "resourceVersion": "100", "writes": []}},
    }

    def post(**kw):
        import copy
        p = copy.deepcopy(PRE)
        for k, v in kw.items():
            if k == "rev_a": p["releases"]["krateo-system/publish-a"]["revision"] = v
            if k == "rev_b": p["releases"]["krateo-system/publish-b"]["revision"] = v
            if k == "drop_a": del p["releases"]["krateo-system/publish-a"]
            if k == "uid": p["compositions"]["portals/krateo-system/portal"]["uid"] = v
            if k == "av": p["compositions"]["portals/krateo-system/portal"]["apiVersion"] = v
            if k == "managed": p["compositions"]["portals/krateo-system/portal"]["managed"] = v
            if k == "secret_rv": p["secrets"]["krateo-system/cj"]["resourceVersion"] = v
            if k == "secret_keys": p["secrets"]["krateo-system/cj"]["dataKeys"] = v
        return p

    cases = [
        ("identical snapshots -> no problems", PRE, None, None, 0),
        ("both releases +1, expect 1 -> PASS", post(rev_a="73", rev_b="81"), 1, "publish-", 0),
        ("one release +2, expect 1 -> REFUSED", post(rev_a="74", rev_b="81"), 1, "publish-", 1),
        ("one release unmoved, expect 1 -> REFUSED", post(rev_a="72", rev_b="81"), 1, "publish-", 1),
        ("FALSIFIER: the installer release is EXCLUDED by the matching filter, so its own "
         "revision not moving by 1 is not a finding",
         post(rev_a="73", rev_b="81"), 1, "publish-", 0),
        ("a release DISAPPEARING is always a problem, with no expectation set",
         post(drop_a=True), None, None, 1),
        ("a composition's uid changing = RECREATED, not updated in place",
         post(uid="u-NEW"), None, None, 1),
        ("FALSIFIER: an apiVersion move with the SAME uid is an in-place update, not a problem",
         post(av="composition.krateo.io/v1-8-129"), None, None, 0),
        ("FALSIFIER: managed count changing is REPORTED but is not by itself a problem",
         post(managed=65), None, None, 0),
        ("a Secret's resourceVersion moving is reported as WRITTEN, not as a problem",
         post(secret_rv="200"), None, None, 0),
        ("a Secret's KEY SET changing IS a problem", post(secret_keys=["password", "token"]), None, None, 1),
    ]
    bad = 0
    for label, po, delta, match, want in cases:
        d = diff(PRE, po, expect_delta=delta, matching=match)
        got = len(d["problems"])
        ok = (got > 0) == (want > 0)
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want_problems=%s got=%s" % (want, d["problems"]))
    print("self-test: %d case(s), %d failure(s)" % (len(cases), bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("capture")
    c.add_argument("--context", required=True)
    c.add_argument("--secret", action="append", default=[],
                   metavar="NS/NAME", help="metadata only; never a value")
    c.add_argument("-o", "--out", required=True)
    g = sub.add_parser("diff")
    g.add_argument("pre"); g.add_argument("post")
    g.add_argument("--expect-release-revision-delta", type=int, default=None)
    g.add_argument("--releases-matching", default=None)
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if a.cmd == "capture":
        snap = capture(a.context, a.secret)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1, sort_keys=True)
        print("captured %d release(s), %d composition(s), %d secret(s) -> %s"
              % (len(snap["releases"]), len(snap["compositions"]), len(snap["secrets"]), a.out))
        if not snap["releases"] or not snap["compositions"]:
            print("REFUSED: a snapshot with no releases or no compositions is not a baseline. "
                  "Check the context and try again -- an empty capture read as a baseline is "
                  "worse than none, because the diff will later report everything as new.")
            return 2
        return 0
    if a.cmd == "diff":
        pre = json.load(open(a.pre, encoding="utf-8"))
        post = json.load(open(a.post, encoding="utf-8"))
        d = diff(pre, post, a.expect_release_revision_delta, a.releases_matching)
        print(json.dumps(d, indent=1, sort_keys=True))
        if d["problems"]:
            print("\n%d PROBLEM(S) -- see above." % len(d["problems"]))
            return 1
        print("\nno problems.")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
