#!/usr/bin/env python3
"""Refuse a chart/tests fixture that serves a composition CRD tag at a version the component is not pinned to.

WHY THIS EXISTS. chart/tests/snowplow_seedauthn_test.yaml names the snowplow CRD's served tag
(`v1-12-NN`), and inst.crdExists only renders the Snowplow composition when a SERVED tag matches
the pinned version. So every snowplow pin bump silently turns that suite red unless somebody
remembers to edit two lines by hand. That was remembered by hand for 1.12.70, 1.12.74, 1.12.76 and
1.12.77, and the fifth bump (1.12.78) needed it again. Relying on someone remembering is the defect.

THE RULE. For each fixture under chart/tests/: find the CustomResourceDefinition objects in the
composition.krateo.io group, map each to its pinned component, and refuse a `served: true` version
whose tag (v1-12-77) is a DIFFERENT version than the component's pin (1.12.78).

MAPPING IS BY spec.names.kind, never by guessing the plural (`snowplows.composition.krateo.io` for
`snowplow` is not mechanical): the fixture's own `names.kind` is matched against the pins' `kind`.
A composition-group CRD that cannot be mapped (no names.kind, no pin with that kind, or two) is a
REFUSAL, not a skip. CRDs in other groups (gateway, cert-manager, upstream operators) are not
component CRDs; they are counted as foreign, not silently dropped.

THE SUBTLE CASE: A DEPENDENCY'S STALE TAG IS NOT A FINDING. A dependency is resolved by ITS OWN
served tag, not the dependent's pin, so snowplow_seedauthn_test.yaml deliberately serves
SnowplowCrds at v1-12-67 and Authn at v0-46-0; sweeping those would silently stop testing what the
dep gate does. A CRD is therefore exempt when its component is a (transitive) dep of ANOTHER
component whose CRD is in the same fixture file. Exempt tags are counted and printed.

Fixtures that are stale on purpose (a written-at-version control, a version-override control) are
listed in .github/test-fixture-deliberately-stale.yaml. Each entry is checked: one that no longer
matches a stale served tag is refused, so the list cannot rot.

DENOMINATOR. Prints "N fixture(s) scanned, M CRD object(s), K served tag(s) checked" and REFUSES
when K is zero: a gate that checked nothing and says OK is the vacuous pass this repo is
eliminating. An unreadable or unparseable fixture FAILS CLOSED.

Exit 0 clean, 1 findings or refusals.
"""
import glob
import os
import re
import sys
import tempfile

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PINS = os.path.join(ROOT, "chart", "files", "component-pins.yaml")
TESTS = os.path.join(ROOT, "chart", "tests")
STALE = os.path.join(ROOT, ".github", "test-fixture-deliberately-stale.yaml")
GROUP = "composition.krateo.io"
TAG_RE = re.compile(r"^v(\d+)-(\d+)-(\d+)$")
VER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def walk_crds(o):
    if isinstance(o, dict):
        if o.get("kind") == "CustomResourceDefinition":
            yield o
        for v in o.values():
            yield from walk_crds(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk_crds(v)


def load_pins(path):
    with open(path) as f:
        d = yaml.safe_load(f)
    comps = d["components"]
    by_kind = {}
    for p in comps:
        if p.get("kind"):
            by_kind.setdefault(p["kind"], []).append(p["name"])
    return {p["name"]: p for p in comps}, by_kind


def transitive_deps(pins, name):
    seen, stack = set(), list(pins[name].get("deps") or [])
    while stack:
        d = stack.pop()
        if d in seen or d not in pins:
            continue
        seen.add(d)
        stack.extend(pins[d].get("deps") or [])
    return seen


def check_docs(fname, docs, pins, by_kind, allow):
    """Return (stats, findings, refusals) for one fixture's parsed documents.

    findings: sorted list of (fixture, crd, tag, tag_version, pinned_version)
    refusals: list of strings
    """
    st = {"crds": 0, "checked": 0, "exempt_dep": 0, "allowed": 0, "foreign": 0}
    findings, refusals, used = [], [], set()
    crds = [c for d in docs for c in walk_crds(d)]
    # one entry per distinct CRD name: the same object is repeated across test cases
    seen, uniq = set(), []
    for c in crds:
        key = yaml.safe_dump(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    st["crds"] = len(uniq)
    mapped = []  # (crd, component)
    for c in uniq:
        name = (c.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name:
            refusals.append("%s: a CustomResourceDefinition has no metadata.name" % fname)
            continue
        if not name.endswith("." + GROUP):
            st["foreign"] += 1
            continue
        kind = ((c.get("spec") or {}).get("names") or {}).get("kind")
        owners = by_kind.get(kind, []) if kind else []
        if len(owners) != 1:
            refusals.append(
                "%s: cannot map CRD %s (spec.names.kind=%r) to exactly one pinned component (candidates: %s)"
                % (fname, name, kind, owners or "none")
            )
            continue
        mapped.append((c, name, owners[0]))
    present = {comp for _, _, comp in mapped}
    for c, name, comp in mapped:
        pin = pins[comp].get("version")
        pm = VER_RE.match(str(pin))
        if not pm:
            refusals.append("%s: pin of %s is %r, not x.y.z" % (fname, comp, pin))
            continue
        is_dep = any(comp in transitive_deps(pins, o) for o in present if o != comp)
        for v in (c.get("spec") or {}).get("versions") or []:
            if not isinstance(v, dict) or v.get("served") is not True:
                continue
            tag = v.get("name")
            tm = TAG_RE.match(str(tag))
            if not tm:
                refusals.append("%s: %s serves tag %r, which is not vX-Y-Z" % (fname, name, tag))
                continue
            if tm.groups() == pm.groups():
                st["checked"] += 1
                continue
            if is_dep:
                st["exempt_dep"] += 1
                continue
            if (fname, name, tag) in allow:
                st["allowed"] += 1
                used.add((fname, name, tag))
                continue
            st["checked"] += 1
            findings.append((fname, name, tag, ".".join(tm.groups()), pin))
    return st, sorted(set(findings)), refusals, used


def load_allow(path):
    with open(path) as f:
        d = yaml.safe_load(f)
    out = set()
    for e in d["entries"]:
        out.add((e["fixture"], e["crd"], e["tag"]))
    return out


def run(tests_dir, pins_path, stale_path):
    pins, by_kind = load_pins(pins_path)
    allow = load_allow(stale_path)
    files = sorted(glob.glob(os.path.join(tests_dir, "*.yaml")))
    tot = {"fixtures": 0, "no_crd": 0, "crds": 0, "checked": 0, "exempt_dep": 0, "allowed": 0, "foreign": 0}
    findings, refusals, used = [], [], set()
    if not files:
        refusals.append("no fixtures found under %s" % tests_dir)
    for f in files:
        base = os.path.basename(f)
        tot["fixtures"] += 1
        try:
            with open(f) as fh:
                docs = [d for d in yaml.safe_load_all(fh)]
        except Exception as e:  # unreadable or unparseable: FAIL CLOSED
            refusals.append("%s: unreadable or unparseable fixture: %s" % (base, str(e).splitlines()[0]))
            continue
        st, fi, rf, us = check_docs(base, docs, pins, by_kind, allow)
        if st["crds"] == 0:
            tot["no_crd"] += 1
        for k in ("crds", "checked", "exempt_dep", "allowed", "foreign"):
            tot[k] += st[k]
        findings += fi
        refusals += rf
        used |= us
    for e in sorted(allow - used):
        if any(os.path.basename(f) == e[0] for f in files):
            refusals.append("allowlist entry %s is not a stale served tag in that fixture any more; remove it" % (e,))
        else:
            refusals.append("allowlist entry %s names a fixture that does not exist; remove it" % (e,))
    if tot["checked"] == 0:
        refusals.append("K=0: no served tag was checked against a pin, so an OK would be vacuous")
    return tot, findings, refusals


def report(tot, findings, refusals):
    print(
        "%d fixture(s) scanned (%d with no CRD object), %d CRD object(s), %d served tag(s) checked "
        "[%d dependency tag(s) exempt, %d allowlisted, %d foreign-group CRD(s) not component CRDs]"
        % (tot["fixtures"], tot["no_crd"], tot["crds"], tot["checked"], tot["exempt_dep"], tot["allowed"], tot["foreign"])
    )
    for fx, crd, tag, tv, pv in findings:
        print(
            "FAIL %s: %s serves %s (= %s) but the component is pinned at %s. Update the fixture to the pin's tag."
            % (fx, crd, tag, tv, pv)
        )
    for r in refusals:
        print("REFUSE %s" % r)
    return 1 if findings or refusals else 0


def selftest():
    pins = {
        "snowplow": {"name": "snowplow", "kind": "Snowplow", "version": "1.12.78", "deps": ["snowplow-crd", "authn"]},
        "snowplow-crd": {"name": "snowplow-crd", "kind": "SnowplowCrds", "version": "1.12.78"},
        "authn": {"name": "authn", "kind": "Authn", "version": "0.56.0", "deps": ["authn-crd"]},
        "authn-crd": {"name": "authn-crd", "kind": "AuthnCrds", "version": "0.56.0"},
    }
    by_kind = {"Snowplow": ["snowplow"], "SnowplowCrds": ["snowplow-crd"], "Authn": ["authn"], "AuthnCrds": ["authn-crd"]}

    def crd(plural, kind, tag, served=True, group=GROUP):
        return {
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "%s.%s" % (plural, group)},
            "spec": {"names": {"kind": kind}, "versions": [{"name": tag, "served": served}]},
        }

    def doc(*crds):
        return {"tests": [{"kubernetesProvider": {"objects": list(crds)}}]}

    fails = []

    def expect(label, got, want):
        if got != want:
            fails.append("%s: got %r want %r" % (label, got, want))

    # 1. matching served tag passes
    st, f, r, _ = check_docs("m.yaml", [doc(crd("snowplows", "Snowplow", "v1-12-78"))], pins, by_kind, set())
    expect("match findings", (f, r, st["checked"]), ([], [], 1))
    # 2. stale served tag FAILS
    st, f, r, _ = check_docs("s.yaml", [doc(crd("snowplows", "Snowplow", "v1-12-77"))], pins, by_kind, set())
    expect("stale", (f, r), ([("s.yaml", "snowplows.composition.krateo.io", "v1-12-77", "1.12.77", "1.12.78")], []))
    # 2b. an unserved stale tag is not a finding (and is not checked)
    st, f, r, _ = check_docs("u.yaml", [doc(crd("snowplows", "Snowplow", "v1-12-77", served=False))], pins, by_kind, set())
    expect("unserved", (f, r, st["checked"]), ([], [], 0))
    # 3. deliberately stale DEPENDENCIES must not be flagged (the real snowplow_seedauthn shape)
    d3 = doc(
        crd("snowplows", "Snowplow", "v1-12-78"),
        crd("snowplowcrdses", "SnowplowCrds", "v1-12-67"),
        crd("authns", "Authn", "v0-46-0"),
    )
    st, f, r, _ = check_docs("d.yaml", [d3], pins, by_kind, set())
    expect("dep exempt", (f, r, st["checked"], st["exempt_dep"]), ([], [], 1, 2))
    # 3b. the same dep CRDs WITHOUT their dependent present are roots and ARE checked
    st, f, r, _ = check_docs("d2.yaml", [doc(crd("authns", "Authn", "v0-46-0"))], pins, by_kind, set())
    expect("dep without dependent", (len(f), r), (1, []))
    # 4. unmappable composition CRD is a REFUSAL, not a skip
    st, f, r, _ = check_docs("x.yaml", [doc(crd("mysteries", "Mystery", "v1-0-0"))], pins, by_kind, set())
    expect("unmappable", (f, len(r)), ([], 1))
    nokind = crd("mysteries", "Mystery", "v1-0-0")
    del nokind["spec"]["names"]
    st, f, r, _ = check_docs("x2.yaml", [doc(nokind)], pins, by_kind, set())
    expect("no kind", (f, len(r)), ([], 1))
    # 5. no CRD objects at all: passes, and is counted by run()
    st, f, r, _ = check_docs("n.yaml", [{"tests": []}], pins, by_kind, set())
    expect("no crd", (st["crds"], f, r), (0, [], []))
    # foreign-group CRD is counted, not mapped, not refused
    st, f, r, _ = check_docs("g.yaml", [doc(crd("httproutes", "HTTPRoute", "v1", group="gateway.networking.k8s.io"))], pins, by_kind, set())
    expect("foreign", (st["foreign"], f, r), (1, [], []))
    # 6. WHOLE finding set of one fixture: stale root + stale deps + a second stale root tag
    multi = crd("snowplows", "Snowplow", "v1-12-77")
    multi["spec"]["versions"].append({"name": "v1-12-70", "served": True})
    multi["spec"]["versions"].append({"name": "v1-12-78", "served": True})
    d6 = doc(multi, crd("snowplowcrdses", "SnowplowCrds", "v1-12-67"), crd("authns", "Authn", "v0-46-0"))
    st, f, r, _ = check_docs("w.yaml", [d6], pins, by_kind, set())
    expect(
        "whole set",
        (f, r, st["checked"], st["exempt_dep"], st["crds"]),
        (
            [
                ("w.yaml", "snowplows.composition.krateo.io", "v1-12-70", "1.12.70", "1.12.78"),
                ("w.yaml", "snowplows.composition.krateo.io", "v1-12-77", "1.12.77", "1.12.78"),
            ],
            [],
            3,
            2,
            3,
        ),
    )
    # 7. allowlist: honoured when stale, and an entry that matches nothing is refused by run()
    allow = {("a.yaml", "snowplows.composition.krateo.io", "v1-12-77")}
    st, f, r, used = check_docs("a.yaml", [doc(crd("snowplows", "Snowplow", "v1-12-77"))], pins, by_kind, allow)
    expect("allowlisted", (f, r, st["allowed"], used), ([], [], 1, allow))

    # 8. run(): denominator, no-CRD counting, fail-closed on unparseable, K=0, dead allowlist entry
    with tempfile.TemporaryDirectory() as td:
        tdir = os.path.join(td, "tests")
        os.makedirs(tdir)
        pp = os.path.join(td, "pins.yaml")
        sp = os.path.join(td, "stale.yaml")
        yaml.safe_dump({"components": list(pins.values())}, open(pp, "w"))
        yaml.safe_dump({"entries": []}, open(sp, "w"))

        def put(n, text):
            with open(os.path.join(tdir, n), "w") as fh:
                fh.write(text)

        put("good.yaml", yaml.safe_dump(doc(crd("snowplows", "Snowplow", "v1-12-78"))))
        put("empty.yaml", "tests: []\n")
        tot, f, r = run(tdir, pp, sp)
        expect("run ok", (tot["fixtures"], tot["no_crd"], tot["crds"], tot["checked"], f, r), (2, 1, 1, 1, [], []))
        put("bad.yaml", "tests: [unclosed\n  - : :\n")
        tot, f, r = run(tdir, pp, sp)
        expect("unparseable fails closed", (len(r), f), (1, []))
        os.remove(os.path.join(tdir, "bad.yaml"))
        os.remove(os.path.join(tdir, "good.yaml"))
        tot, f, r = run(tdir, pp, sp)
        expect("K=0 refused", (tot["checked"], any(x.startswith("K=0") for x in r)), (0, True))
        put("good.yaml", yaml.safe_dump(doc(crd("snowplows", "Snowplow", "v1-12-78"))))
        yaml.safe_dump({"entries": [{"fixture": "good.yaml", "crd": "snowplows.composition.krateo.io", "tag": "v1-12-77"}]}, open(sp, "w"))
        tot, f, r = run(tdir, pp, sp)
        expect("dead allowlist entry", len(r), 1)

    if fails:
        for x in fails:
            print("SELF-TEST FAIL " + x)
        return 1
    print("self-test OK: match, stale, unserved, dep-exempt, dep-without-dependent, unmappable, no-kind, no-CRD, foreign, whole-set, allowlist, denominator, fail-closed, K=0")
    return 0


def main():
    if "--self-test" in sys.argv:
        return selftest()
    try:
        tot, findings, refusals = run(TESTS, PINS, STALE)
    except Exception as e:
        print("REFUSE cannot run: %s: %s" % (type(e).__name__, e))
        return 1
    return report(tot, findings, refusals)


if __name__ == "__main__":
    sys.exit(main())
