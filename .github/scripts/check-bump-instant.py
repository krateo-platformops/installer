#!/usr/bin/env python3
"""CI guard: pin what the emission decision does AT A VERSION TRANSITION — the one state none of
this repo's other gates exercises.

THIS NOW PINS THE FIX, NOT THE DEFECT. It began life as a characterization test of open defect
installer#477, asserting the buggy behaviour so the fix could not land unnoticed. #477 was fixed by
#665 (the gate) and #667 (the four other call sites), so on 2026-11-05 this gate did exactly what it
was built to do: it failed, and its own message said that failing was the intended outcome. This is
the rewrite it asked for. It asserts the FIXED shape positively, and a self-test proves it would
catch the defect's return.

WHY THIS GATE WAS MISSING, measured rather than assumed. Of this repo's 18 gate scripts, exactly
one mentions a version transition at all, and none drives the emission decision with a cluster
state from mid-bump. Every gate renders or reasons about a SINGLE version — a steady state. The
defect lives in the instant between a CompositionDefinition advancing and crdgen creating the CRD
version it implies, and nothing looked there.

THE FIX, which is what this gate now asserts. compositions.yaml no longer decides anything about
versions: it reads ONE decision, `inst.emitDecision`, which resolves the whole gate including the
feature-OFF drain. Inside it, `inst.emitTag` answers "which apiVersion do we emit at" with a
three-way resolution (target served -> target; target unserved but the live instance exists at a
served version -> THAT version; neither -> withhold), and the escape is `inst.compositionExistsAny`,
which is version-AGNOSTIC: an existence check must be permissive, because its job is to STOP a
deletion. `requiresApiGroups` remains ANDed OUTSIDE the escape, deliberately and with its reason in
the helper: a missing API group is not a flap, and emitting anyway buys an endless 500.

THE MECHANISM IT REPLACED (#477). compositions.yaml:124 used to be:

    and (eq $crd "true") (eq $req "true") (or (eq $deps "true") (eq $exists "true"))

`$crd` is an OUTER conjunct and `inst.crdExists` demands a CRD *serving the component's new
version*. At the bump instant no such version is served — the bumped CompositionDefinition is what
causes crdgen to create it. So `$crd` is false, the `and` short-circuits, and **`$exists` is never
consulted**. `$exists` is the clause added on 2026-07-08 specifically to stop a live composition
being pruned when a gate goes transiently false; it cannot do that here, because it sits inside the
conjunct that fails.

THE SAME CONSTRUCTION DESTROYED 34 LIVE COMPOSITION CRs IN A SIBLING CHART ON 2026-11-04.
krateo-blueprints' openstack umbrella has `$crd` outside the escape too; a 0.3.32 -> 0.3.33 bump of
a0-openstack-d4 pruned every component CR on the first render, leaving 27 helm releases with no
composition CR driving them and three components split across two releases. Filed there as #166.
Their `$exists` is MORE permissive than ours — version-agnostic where inst.compositionExists is
version-keyed — and it made no difference, because the outer conjunct fails first.

WHAT IS NOT KNOWN, and this gate does not pretend otherwise: whether a real installer roll reaches
this path. Many single-component pin bumps on krateo-demo and krateo-057 on 2026-11-04 produced
zero prunes, zero new sibling releases, and 0 of 257 compositions lacking a release-name label. So
either the path is not reached on those rolls or something upstream prevents it, and this gate
cannot tell which — it exercises the helper, not the live sequence.

WHAT A GREEN RUN DOES NOT COVER: it never calls `lookup`, so it says nothing about the CRD list the
chart actually receives at runtime, and nothing about ordering between the CD write and crdgen.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HELPERS = Path("chart/templates/_helpers.tpl")
GATE = Path("chart/templates/compositions.yaml")

KIND, OLD, NEW = "Portal", "1.8.89", "1.8.90"


def crd(kind, vers):
    return {"spec": {"group": "composition.krateo.io", "names": {"kind": kind},
                     "versions": [{"name": v, "served": True} for v in vers]}}


def vtag(v):
    return "v" + v.replace(".", "-")


#  (label, allCRDs, expected crdExists)
CASES = [
    ("THE BUMP INSTANT: CRD serves only the OLD version", [crd(KIND, [vtag(OLD), "vacuum"])], ""),
    ("after crdgen catches up: both versions served",     [crd(KIND, [vtag(OLD), vtag(NEW), "vacuum"])], "true"),
    ("steady state: only the NEW version served",         [crd(KIND, [vtag(NEW), "vacuum"])], "true"),
    ("CRD list EMPTY (helm template / lookup disabled)",  [], ""),
    ("NEW version present but served=false",
     [{"spec": {"group": "composition.krateo.io", "names": {"kind": KIND},
                "versions": [{"name": vtag(NEW), "served": False},
                             {"name": vtag(OLD), "served": True}]}}], ""),
]

PROBE = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\ndata:\n"
    "{{- range $i, $c := .Values.cases }}\n"
    '  case{{ $i }}: {{ (include "inst.crdExists" (list $c.kind $c.version $c.allCRDs)) | quote }}\n'
    "{{- end }}\n"
)


def render(helpers_text):
    work = Path(tempfile.mkdtemp(prefix="bump-instant-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        (chart / "templates" / "_helpers.tpl").write_text(helpers_text)
        (chart / "templates" / "probe.yaml").write_text(PROBE)
        (chart / "values.yaml").write_text("cases: []\n")
        vals = work / "cases.yaml"
        vals.write_text(yaml.safe_dump({"cases": [
            {"kind": KIND, "version": NEW, "allCRDs": a} for _l, a, _e in CASES]},
            default_flow_style=False))
        p = subprocess.run(["helm", "template", "probe", str(chart), "-f", str(vals)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return {"__error__": p.stderr.strip()[:1200]}
        doc = next(d for d in yaml.safe_load_all(p.stdout)
                   if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        return doc.get("data") or {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# The structural half. The mechanism is the SHAPE of the decision, which no behavioural probe sees
# because `inst.emitTag`'s live-instance arm calls `lookup` and `helm template` disables lookup.
#
# ASSERTED POSITIVELY — as the lines that must be PRESENT — not as the absence of the defect's old
# text. An absence-check passes for any phrasing its author did not anticipate;
# openstack-as-a-service hit exactly that and replaced the same shape. The one absence check here is
# narrow and paired with a positive one: the defect was a $crd OUTER conjunct, so its return is
# detectable as that specific construction reappearing.
DEFECT = '(eq $crd "true")'

REQUIRED = [
    (GATE, 'include "inst.emitDecision"',
     "compositions.yaml reads ONE decision instead of rebuilding the gate"),
    (HELPERS, 'define "inst.emitDecision"',
     "the single decision exists as a helper"),
    (HELPERS, 'include "inst.emitTag"',
     "emitDecision resolves the version through inst.emitTag"),
    (HELPERS, 'or (eq $deps "true") (eq $exists "true")',
     "the ($deps or $exists) escape is REACHABLE — not behind an outer version conjunct"),
    (HELPERS, 'include "inst.compositionExistsAny"',
     "the escape is version-AGNOSTIC (any served tag), so a mid-migration instance still counts"),
]


def structural(gate_src, helpers_src, report=False):
    """Returns a list of failures. Pure, so the self-test can drive it on mutated sources."""
    out = []
    src = {GATE: gate_src, HELPERS: helpers_src}
    for f, needle, why in REQUIRED:
        if needle in src[f]:
            if report:
                print(f"  PASS  {f.name}: {why}")
        else:
            out.append(f"{f.name} no longer contains {needle!r} — {why}. "
                       "If this changed deliberately, the bump-instant behaviour must be "
                       "re-measured before this gate can speak for it.")
    if DEFECT in gate_src:
        out.append(f"compositions.yaml contains {DEFECT!r} again — installer#477's outer version "
                   "conjunct is back, which makes the ($deps or $exists) escape unreachable at the "
                   "bump instant and prunes the live composition being upgraded.")
    elif report:
        print(f"  PASS  compositions.yaml has no {DEFECT} outer conjunct (installer#477 stays fixed)")
    return out


def selftest():
    """Prove the structural check DETECTS something. A gate that has only ever been shown to pass
    is indistinguishable from one that cannot fail."""
    gate_src, helpers_src = GATE.read_text(), HELPERS.read_text()
    if structural(gate_src, helpers_src):
        print("::error::self-test precondition failed — the gate does not pass on HEAD")
        return 1
    probes = [
        ("reintroducing #477's outer $crd conjunct",
         gate_src + '\n{{- if and (eq $crd "true") (eq $req "true") }}{{- end -}}\n', helpers_src),
        ("removing the single decision from compositions.yaml",
         gate_src.replace('include "inst.emitDecision"', 'include "inst.somethingElse"'), helpers_src),
        ("making the escape version-KEYED again",
         gate_src, helpers_src.replace('include "inst.compositionExistsAny"',
                                       'include "inst.compositionExistsAtTag"')),
        ("putting the escape behind an outer conjunct",
         gate_src, helpers_src.replace('or (eq $deps "true") (eq $exists "true")',
                                       'and (eq $deps "true") (eq $exists "true")')),
    ]
    bad = 0
    for label, g, h in probes:
        found = structural(g, h)
        print(f"  {'ok  ' if found else 'FAIL'} self-test: {label} -> "
              f"{'detected' if found else 'NOT DETECTED'}")
        if not found:
            bad += 1
    if bad:
        print(f"::error::self-test: {bad} mutation(s) went undetected, so this gate cannot "
              "vouch for the shape it claims to pin")
    return 1 if bad else 0


def main() -> int:
    for f in (HELPERS, GATE):
        if not f.is_file():
            print(f"::error::{f} not found — run from the repo root")
            return 1

    data = render(HELPERS.read_text())
    if "__error__" in data:
        print("::error::probe render failed — the gate cannot vouch for a helper it could not run")
        print(data["__error__"])
        return 1

    failures = []
    for i, (label, _a, want) in enumerate(CASES):
        got = data.get(f"case{i}")
        ok = got == want
        verdict = "EMITTED " if got == "true" else "WITHHELD"
        print(f"  {'PASS' if ok else 'FAIL'}  {label}\n          crdExists={got!r} -> {verdict}"
              + ("" if ok else f"   <-- WANT {want!r}"))
        if not ok:
            failures.append(f"{label}: want {want!r}, got {got!r}")

    # The structural half: the mechanism is the POSITION of $crd, which no behavioural probe sees.
    # Asserted POSITIVELY — as the line that must be present — rather than as the absence of the
    # defect's old text, because an absence-check passes for any phrasing its author did not
    # anticipate. (openstack-as-a-service hit exactly that and replaced the same shape.)
    gate_src = GATE.read_text()
    helpers_src = HELPERS.read_text()
    failures += structural(gate_src, helpers_src, report=True)

    if failures:
        print("::error::bump-instant gate: the emission decision at a version transition is not "
              "what this gate recorded. This gate is a CHARACTERIZATION of open defect #477:")
        for f in failures:
            print(f"  {f}")
        return 1

    if selftest():
        return 1

    print(f"\nbump-instant gate: OK — {len(CASES)} behavioural cases, {len(REQUIRED)} structural "
          f"assertions, 1 defect-absence check, 4 self-test mutations all detected.")
    print("THIS PINS THE FIX (installer#477, fixed by #665 and #667): compositions.yaml reads one "
          "decision, the version is resolved inside inst.emitTag, and the ($deps or $exists) escape "
          "is reachable and version-agnostic.")
    print("WHAT A GREEN RUN STILL DOES NOT COVER: inst.emitTag's live-instance arm calls lookup, "
          "which helm template disables, so no case here drives the bump instant with a real "
          "cluster state. That arm is covered by check-ready-gate.py's faked-cluster suite, and by "
          "nothing in this file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
