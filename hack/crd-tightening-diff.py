#!/usr/bin/env python3
"""Pin-time tool: what does a -crd bump TIGHTEN, and can it reach objects that already exist?

WHY THIS EXISTS, and it is a specific pair of mistakes rather than a hypothesis.

A CEL rule or a `required` added to an EXISTING shape makes stored objects UNUPDATABLE at their
next write -- silently, because a CRD tightening bites at the next WRITE and not at the CRD apply,
so the symptom surfaces long after the roll as a failed helm release for whatever component next
writes that kind. A rule or `required` INSIDE A NEW optional field cannot do that: no stored object
carries the field, so nothing can be rejected. The whole decision is which of those two it is, and
I got it wrong in both directions on authn 0.47.0 in one sitting:

  FIRST PASS   reported a new `required ('audiences','oidcConfig')` -- REAL -- alongside a bogus
               "shallowest new ancestor spec.versions[0]", which came from a walker that builds
               paths by string concatenation and never emits list-element paths.
  SECOND PASS  "corrected" the real finding to an artifact. It descended properties/items BY NAME
               along paths I named, I asked for spec.delegation.oidcConfig, got ABSENT, and
               reported oidcConfig as absent from BOTH versions. It exists -- at
               spec.delegation.subjectTokens[].oidcConfig. Absence scoped to my query, reported as
               a fact about the schema.

authn caught the second one and asked for exactly the fixture below: a CRD whose array-of-objects
items gain a `required`, asserted to be REPORTED. The safety verdict on 0.47.0 was right either
way, because the parent list is new -- but the instrument would have hidden the same `required` on
an EXISTING list, which is the refusal case.

WHAT IT REPORTS, per served version of every CRD a chart renders:

    path                      semantic: properties by NAME, array items as `[]`
    required / cel / default  the keywords that change what the apiserver accepts
    bounds                    minItems/maxItems/minLength/maxLength/minimum/maximum
    PARENT EXISTED            whether that path was present in the OLD chart

`PARENT EXISTED: True` on a tightening is the refusal class. False means a new shape.
A `default` appearing ANYWHERE is reported regardless, because a default is a value and not a
constraint -- see check-fill-defaults.py for what a default in the wrong place costs.

Loosenings (a `required` removed, a bound widened) are reported too, under RELAXED, because they
are safe for existing objects but tell the reader the shape moved.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required by hack/crd-tightening-diff.py")

KEYWORDS = ("required", "x-kubernetes-validations", "default",
            "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum")


def schema_walk(node, path):
    """Walk an openAPIV3Schema SEMANTICALLY. Properties by name; array items as `[]`.

    Descending node['items'] is the part the broken walker lacked: an array-of-objects keeps its
    fields and its `required` under items, so a walker that only follows `properties` cannot see
    them at all.
    """
    if not isinstance(node, dict):
        return
    yield path, node
    for k, v in (node.get("properties") or {}).items():
        yield from schema_walk(v, "%s.%s" % (path, k))
    if isinstance(node.get("items"), dict):
        yield from schema_walk(node["items"], path + "[]")


def facts_from_docs(docs):
    """path -> {keyword: value} for every constrained node, keyed <crd>/<version>.<path>."""
    out = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "CustomResourceDefinition":
            continue
        name = (doc.get("metadata") or {}).get("name")
        for ver in ((doc.get("spec") or {}).get("versions") or []):
            root = ((ver.get("schema") or {}).get("openAPIV3Schema"))
            if not isinstance(root, dict):
                continue
            for path, n in schema_walk(root, "%s/%s" % (name, ver.get("name"))):
                rec = {}
                for k in KEYWORDS:
                    if k not in n:
                        continue
                    v = n[k]
                    if k == "required" and isinstance(v, list):
                        rec[k] = tuple(sorted(str(x) for x in v))
                    elif k == "x-kubernetes-validations":
                        rec["cel"] = [r.get("rule") for r in (v or []) if isinstance(r, dict)]
                    elif k == "default":
                        rec[k] = json.dumps(v, sort_keys=True)
                    else:
                        rec[k] = v
                # EVERY path is recorded, constrained or not. The first draft stored only
                # paths that HAD a constraint, so "did the parent exist in the old chart?" was
                # silently answered by "did it already have a constraint?" -- and a CEL rule or
                # `required` added to a previously UNCONSTRAINED existing field read as NEW-SHAPE,
                # i.e. safe. That is the exact refusal case this tool exists to catch, so the bug
                # inverted its only important verdict. Three self-test cases failed on it.
                out[path] = rec
    return out


def render(chart_dir):
    r = subprocess.run(["helm", "template", "x", chart_dir, "--namespace", "krateo-system"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("::error::helm template failed for %s: %s" % (chart_dir, (r.stderr or "")[:300]))
        return None
    return [d for d in yaml.safe_load_all(r.stdout) if isinstance(d, dict)]


TIGHTENING = ("required", "cel", "minItems", "minLength", "minimum")
LOOSENING = ("maxItems", "maxLength", "maximum")


def _cel_near(facts, path):
    """Does a CEL rule sit on this node or on its array items, in this version of the schema?"""
    return bool((facts.get(path) or {}).get("cel") or (facts.get(path + "[]") or {}).get("cel"))


def compare(old, new):
    """[(severity, path, keyword, before, after, parent_existed)] -- severity in
    REACHES-EXISTING / COST-RECHECK / NEW-SHAPE / RELAXED / DEFAULT."""
    findings = []
    for path in sorted(set(old) | set(new)):
        o, n = old.get(path) or {}, new.get(path) or {}
        parent_existed = path in old
        for k in sorted(set(o) | set(n)):
            if o.get(k) == n.get(k):
                continue
            if k == "default":
                # DEFAULT outranks even a new path: a default on a NEW optional field is still
                # applied to objects, so it is a VALUE arriving, not merely a shape.
                sev = "DEFAULT"
            elif not parent_existed:
                # The path did not exist, so NOTHING about it can affect a stored object --
                # whatever the keyword. The first draft only reached NEW-SHAPE through the
                # tightening branch, so a bound on a new path fell through to RELAXED: nothing
                # had been relaxed, and the label was simply untrue. It sat in the safe bucket
                # either way, which is why it survived -- a wrong label that happens not to
                # change the verdict is still a wrong label, and this tool's whole job is to
                # say which bucket a change is in.
                sev = "NEW-SHAPE"
            elif k in TIGHTENING or (k in LOOSENING and n.get(k) is not None and
                                     o.get(k) is not None and n[k] < o[k]):
                sev = "REACHES-EXISTING"
            elif (k in LOOSENING and o.get(k) is None and n.get(k) is not None
                  and (_cel_near(new, path) or _cel_near(old, path))):
                # A BOUND NEWLY APPEARING ON A PRE-EXISTING NODE THAT CARRIES A CEL RULE.
                # Reported by oasgen-provider 2026-11-14 from a MEASURED case, and my first
                # draft called this RELAXED -- the exact wrong answer. Adding maxItems to a
                # previously unbounded array can make the apiserver's cost estimator START
                # costing a rule it had SKIPPED, turning an O(n^2) rule into a CRD GENERATION
                # failure. The bound looks like a tightening of the array; the breakage lands
                # on the rule, and the CRD then does not exist at all rather than merely
                # rejecting writes.
                # Restricted to parent_existed: a BRAND-NEW array arriving with both a bound
                # and a rule is costed correctly from the start -- there is no previously
                # skipped rule to enable. Without that restriction this would fire on every
                # ordinary additive release (authn 0.47.0's new audiences[] carries both).
                # This tool CANNOT decide it -- cost estimation is the apiserver's. So it
                # reports that the check is REQUIRED and exits non-zero, rather than printing
                # a note nobody has to act on. A condition only a reader can evaluate, with
                # nothing forcing them to, is a check that cannot fail.
                sev = "COST-RECHECK"
            else:
                sev = "RELAXED"
            findings.append((sev, path, k, o.get(k), n.get(k), parent_existed))
    return findings


def report(findings, old, new):
    order = {"REACHES-EXISTING": 0, "COST-RECHECK": 1, "DEFAULT": 2, "NEW-SHAPE": 3,
             "RELAXED": 4}
    findings = sorted(findings, key=lambda f: (order.get(f[0], 9), f[1], f[2]))
    co = sum(1 for v in old.values() if v)
    cn = sum(1 for v in new.values() if v)
    print("  schema paths  old: %d (%d constrained)   new: %d (%d constrained)"
          % (len(old), co, len(new), cn))
    for sev, path, k, before, after, existed in findings:
        crd, bare = path.split("/", 1)
        print("  %-17s %s" % (sev, bare))
        print("      crd=%s  keyword=%s" % (crd, k))
        print("      %r -> %r   parent path existed before: %s" % (before, after, existed))
    cost = [f for f in findings if f[0] == "COST-RECHECK"]
    if cost:
        print()
        print("  %d bound(s) newly added to a PRE-EXISTING node that carries a CEL rule." % len(cost))
        print("  That can make the apiserver START costing a rule it previously SKIPPED, so an")
        print("  O(n^2) rule becomes a CRD GENERATION failure and the CRD does not exist at all.")
        print("  THIS TOOL CANNOT DECIDE IT -- cost estimation is the apiserver's. Render the CRD at")
        print("  the new version and confirm generation succeeds, then record that in the pin comment.")
    worst = [f for f in findings if f[0] == "REACHES-EXISTING"]
    print()
    if worst:
        print("  %d tightening(s) REACH A SHAPE THAT ALREADY EXISTED. Each one makes stored objects"
              % len(worst))
        print("  unupdatable at their next write unless every existing object already satisfies it.")
        print("  That is a REFUSAL unless you have counted the existing objects on every cluster.")
    else:
        print("  no tightening reaches a pre-existing shape.")
    return 1 if (worst or cost) else 0


# ---------------------------------------------------------------------------------------------
# Self-test. Synthetic, offline, and the first case is the one authn asked for.
# ---------------------------------------------------------------------------------------------
def _crd(spec_props, version="v1"):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": "things.example.com"},
            "spec": {"group": "example.com", "names": {"kind": "Thing"},
                     "versions": [{"name": version, "served": True, "schema": {
                         "openAPIV3Schema": {"type": "object", "properties": {
                             "spec": {"type": "object", "properties": spec_props}}}}}]}}


def self_test():
    ok = True

    def run(a, b):
        fa, fb = facts_from_docs([a]), facts_from_docs([b])
        assert fa is not None and fb is not None
        return compare(fa, fb), fa, fb

    def sev_at(findings, needle, kw):
        hits = [f for f in findings if needle in f[1] and f[2] == kw]
        return hits[0][0] if hits else None

    LIST_OF_OBJ = {"items_list": {"type": "array", "items": {
        "type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}}}

    # 1. THE CASE authn ASKED FOR: an EXISTING array-of-objects gains a `required` on its items.
    before = _crd(LIST_OF_OBJ)
    after = json.loads(json.dumps(before))
    itm = after["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["items_list"]["items"]
    itm["required"] = ["a"]
    f, fa, fb = run(before, after)
    got = sev_at(f, "items_list[]", "required")
    good = got == "REACHES-EXISTING"
    print("%s  required added to items of an EXISTING array -> %s (want REACHES-EXISTING)"
          % ("ok  " if good else "FAIL", got))
    if not good:
        ok = False
    # non-vacuity: the walker must actually see the items path
    seen = any(p.endswith("items_list[]") for p in fb)
    print("%s  the walker emits the array-item path at all: %s" % ("ok  " if seen else "FAIL", seen))
    if not seen:
        ok = False

    # 2. The same `required`, but the whole array is NEW -> safe.
    before2 = _crd({"other": {"type": "string"}})
    after2 = _crd(json.loads(json.dumps(LIST_OF_OBJ)))
    after2["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["items_list"]["items"]["required"] = ["a"]
    f2, _, _ = run(before2, after2)
    got2 = sev_at(f2, "items_list[]", "required")
    good = got2 == "NEW-SHAPE"
    print("%s  required added to items of a NEW array -> %s (want NEW-SHAPE)"
          % ("ok  " if good else "FAIL", got2))
    if not good:
        ok = False

    # 3. A CEL rule on an EXISTING scalar -> refusal class.
    b3 = _crd({"f": {"type": "string"}})
    a3 = json.loads(json.dumps(b3))
    a3["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["f"]["x-kubernetes-validations"] = [{"rule": "self != ''"}]
    f3, _, _ = run(b3, a3)
    got3 = sev_at(f3, ".spec.f", "cel")
    good = got3 == "REACHES-EXISTING"
    print("%s  CEL added to an EXISTING field -> %s (want REACHES-EXISTING)"
          % ("ok  " if good else "FAIL", got3))
    if not good:
        ok = False

    # 4. A CEL rule on a NEW scalar -> safe. This is authn 0.47.0's actual shape.
    b4 = _crd({"old": {"type": "string"}})
    a4 = _crd({"old": {"type": "string"},
               "newf": {"type": "string", "x-kubernetes-validations": [{"rule": "self != ''"}]}})
    f4, _, _ = run(b4, a4)
    got4 = sev_at(f4, ".spec.newf", "cel")
    good = got4 == "NEW-SHAPE"
    print("%s  CEL added to a NEW field -> %s (want NEW-SHAPE)" % ("ok  " if good else "FAIL", got4))
    if not good:
        ok = False

    # 5. A `default` is reported wherever it appears -- it is a value, not a constraint.
    b5 = _crd({"f": {"type": "integer"}})
    a5 = _crd({"f": {"type": "integer", "default": 4}})
    f5, _, _ = run(b5, a5)
    got5 = sev_at(f5, ".spec.f", "default")
    good = got5 == "DEFAULT"
    print("%s  a `default` added -> %s (want DEFAULT)" % ("ok  " if good else "FAIL", got5))
    if not good:
        ok = False

    # 6. CONTROL: identical inputs must yield NOTHING. Without this every case above could be
    #    passing because the differ reports everything it sees.
    f6, fa6, _ = run(b5, json.loads(json.dumps(b5)))
    good = not f6 and len(fa6) > 0
    print("%s  identical inputs -> %d finding(s) (want 0), with %d schema path(s) seen (want >0, "
          "or the zero findings prove nothing)" % ("ok  " if good else "FAIL", len(f6), len(fa6)))
    if not good:
        ok = False

    # 7. A tightened bound on an existing field is a tightening; a loosened one is RELAXED.
    b7 = _crd({"f": {"type": "string", "maxLength": 100}})
    a7 = _crd({"f": {"type": "string", "maxLength": 10}})
    f7, _, _ = run(b7, a7)
    got7 = sev_at(f7, ".spec.f", "maxLength")
    a7b = _crd({"f": {"type": "string", "maxLength": 1000}})
    f7b, _, _ = run(b7, a7b)
    got7b = sev_at(f7b, ".spec.f", "maxLength")
    good = got7 == "REACHES-EXISTING" and got7b == "RELAXED"
    print("%s  maxLength 100->10 = %s, 100->1000 = %s (want REACHES-EXISTING, RELAXED)"
          % ("ok  " if good else "FAIL", got7, got7b))
    if not good:
        ok = False

    # ---- COST-RECHECK: oasgen-provider's measured case, which my first draft called RELAXED ----
    RULE = [{"rule": "self.all(x, self.exists_one(y, y == x))"}]
    arr = {"type": "array", "items": {"type": "string"}}
    withrule = dict(arr, **{"x-kubernetes-validations": RULE})
    cases = (
        ("an EXISTING array WITH a CEL rule gains maxItems", _crd({"arr": withrule}),
         _crd({"arr": dict(withrule, maxItems=64)}), "COST-RECHECK"),
        ("an EXISTING array with NO rule gains maxItems", _crd({"arr": arr}),
         _crd({"arr": dict(arr, maxItems=64)}), "RELAXED"),
        ("a NEW array arriving with a rule AND a bound", _crd({"other": {"type": "string"}}),
         _crd({"other": {"type": "string"}, "arr": dict(withrule, maxItems=64)}), "NEW-SHAPE"),
    )
    for label, a, b, want in cases:
        f, _, _ = run(a, b)
        got = sev_at(f, ".spec.arr", "maxItems")
        good = got == want
        print("%s  %s -> %s (want %s)" % ("ok  " if good else "FAIL", label, got, want))
        if not good:
            ok = False

    print("\nself-test: %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--chart", help="chart name; pulls --old and --new from --repo")
    ap.add_argument("--repo", default="oci://ghcr.io/krateo-platformops/charts")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--dirs", nargs=2, metavar=("OLD_DIR", "NEW_DIR"))
    a = ap.parse_args()
    if a.self_test:
        return 0 if self_test() else 1
    if a.dirs:
        od, nd = a.dirs
        work = None
    elif a.chart and a.old and a.new:
        work = tempfile.mkdtemp(prefix="crd-tightening-")
        for v in (a.old, a.new):
            d = os.path.join(work, v)
            os.makedirs(d)
            r = subprocess.run(["helm", "pull", "%s/%s" % (a.repo, a.chart),
                                "--version", v, "--untar"], cwd=d,
                               capture_output=True, text=True)
            if r.returncode != 0:
                print("::error::could not pull %s %s: %s" % (a.chart, v, (r.stderr or "")[:200]))
                return 2
        od = os.path.join(work, a.old, a.chart)
        nd = os.path.join(work, a.new, a.chart)
    else:
        ap.error("need --self-test, or --dirs OLD NEW, or --chart with --old and --new")
    try:
        do, dn = render(od), render(nd)
        if do is None or dn is None:
            return 2
        fo, fn = facts_from_docs(do), facts_from_docs(dn)
        if not fo or not fn:
            print("::error::one side yielded NO schema paths at all (old=%d new=%d). Refusing to "
                  "report a comparison whose corpus may be empty -- 'no findings' and 'nothing "
                  "was read' are the same output otherwise." % (len(fo), len(fn)))
            return 2
        return report(compare(fo, fn), fo, fn)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
