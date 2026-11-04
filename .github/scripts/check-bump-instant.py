#!/usr/bin/env python3
"""CI guard: pin what the emission decision does AT A VERSION TRANSITION — the one state none of
this repo's other gates exercises.

THIS IS A CHARACTERIZATION TEST OF AN OPEN DEFECT (installer#477). It asserts the behaviour that
exists today, not the behaviour that should. **When #477 is fixed, this test MUST fail**, and its
failure message says so. That is the point: the mechanism is currently recorded only in an issue,
and an issue is not something CI reads.

WHY THIS GATE WAS MISSING, measured rather than assumed. Of this repo's 18 gate scripts, exactly
one mentions a version transition at all, and none drives the emission decision with a cluster
state from mid-bump. Every gate renders or reasons about a SINGLE version — a steady state. The
defect lives in the instant between a CompositionDefinition advancing and crdgen creating the CRD
version it implies, and nothing looked there.

THE MECHANISM (#477). compositions.yaml:124 is:

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
    needle = ('{{- if and (eq $crd "true") (eq $req "true") '
              '(or (eq $deps "true") (eq $exists "true")) }}')
    if needle in gate_src:
        print("  PASS  compositions.yaml still ANDs $crd outside the ($deps or $exists) escape")
        print("          so at the bump instant the escape is unreachable — installer#477")
    else:
        failures.append(
            "compositions.yaml's gate expression has CHANGED. If installer#477 was fixed, THIS "
            "TEST IS NOW WRONG and must be updated to assert the new behaviour — that is the "
            "intended outcome, not a problem. If it changed for another reason, the bump-instant "
            "behaviour needs re-measuring before this gate can speak for it.")

    if failures:
        print("::error::bump-instant gate: the emission decision at a version transition is not "
              "what this gate recorded. This gate is a CHARACTERIZATION of open defect #477:")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"\nbump-instant gate: OK — {len(CASES)} cases. At the bump instant inst.crdExists "
          f"returns '' and the composition is WITHHELD, with $exists unreachable.")
    print("THIS PINS A DEFECT, NOT A GUARANTEE (installer#477). When #477 is fixed this test must "
          "fail and be rewritten — it exists so that the fix cannot land unnoticed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
