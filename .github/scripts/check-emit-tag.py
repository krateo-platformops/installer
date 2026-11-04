#!/usr/bin/env python3
"""CI guard: exercise inst.emitTagFrom — the single resolution of which apiVersion a component is
emitted at, and the fix for installer#477.

THE DEFECT. The emission gate used to AND `inst.crdExists $c.version` OUTSIDE the `$exists` escape.
At the instant a component's pin bumps, the generated CRD does not yet serve the new version — the
bumped CompositionDefinition is what causes crdgen to create it — so the conjunct failed, `$exists`
was never consulted, the composition vanished from the manifest and helm PRUNED the live CR. The
escape added on 2026-07-08 to prevent exactly that could not help, because it sat inside the
conjunct that failed.

Not theorised: the identical construction in krateo-blueprints' openstack umbrella pruned 34 live
composition CRs on 2026-11-04, leaving 27 helm releases with nothing driving them. Their escape was
MORE permissive than ours — version-agnostic — and it made no difference, because the outer
conjunct fails first.

WHY THIS TESTS A PURE FUNCTION. The resolution's interesting arm needs a `lookup` to discover which
version the live instance is served at, and `helm template` DISABLES lookup — so while the decision
lived beside its own lookup, the only arm a test could reach was the one that does not matter. The
decision is split into inst.emitTagFrom, which takes the lookup's RESULT as an argument. Every arm
is then reachable with no cluster and no fake-cluster plugin. Same split as inst.jwtSigningKeyPEM.

WHAT A GREEN RUN DOES NOT COVER: inst.emitTag's own `lookup` — that it finds the instance at all,
and at the right namespace. Nothing cluster-free can show that. It is asserted structurally below
(the call exists, with the namespace from values) and behaviourally not at all.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HELPERS = Path("chart/templates/_helpers.tpl")
GATE = Path("chart/templates/compositions.yaml")

#  (label, want, served, existing, expected)
CASES = [
    ("steady state: the target version is served",        "v1-8-90", "v1-8-90 vacuum",   "",        "v1-8-90"),
    ("THE FIX: target NOT served, instance at the old",    "v1-8-90", "v1-8-88 vacuum",   "v1-8-88", "v1-8-88"),
    ("crdgen caught up: both served -> prefer the target", "v1-8-90", "v1-8-88 v1-8-90",  "v1-8-88", "v1-8-90"),
    ("first creation: target not served, NO instance",     "v1-8-90", "v1-8-88 vacuum",   "",        ""),
    ("the CRD serves nothing at all",                      "v1-8-90", "",                 "",        ""),
    ("instance at a tag that is NO LONGER served",         "v1-8-90", "v1-8-89 vacuum",   "v1-8-70", ""),
    ("lookup unavailable (helm template): withhold",       "v1-8-90", "v1-8-88 vacuum",   "",        ""),
]

PROBE = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\ndata:\n"
    "{{- range $i, $c := .Values.cases }}\n"
    '  case{{ $i }}: {{ (include "inst.emitTagFrom" (list $c.want $c.served $c.existing)) | quote }}\n'
    "{{- end }}\n"
)


def render(helpers_text):
    work = Path(tempfile.mkdtemp(prefix="emit-tag-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        (chart / "templates" / "_helpers.tpl").write_text(helpers_text)
        (chart / "templates" / "probe.yaml").write_text(PROBE)
        (chart / "values.yaml").write_text("cases: []\nnamespaces:\n  krateo: krateo-system\n")
        vals = work / "cases.yaml"
        vals.write_text(yaml.safe_dump(
            {"cases": [{"want": w, "served": s, "existing": e} for _l, w, s, e, _x in CASES],
             "namespaces": {"krateo": "krateo-system"}}, default_flow_style=False))
        p = subprocess.run(["helm", "template", "probe", str(chart), "-f", str(vals)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return {"__error__": p.stderr.strip()[:1200]}
        doc = next(d for d in yaml.safe_load_all(p.stdout)
                   if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        return doc.get("data") or {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


FIX_ARM = re.compile(
    r'\{\{- else if and \$existingTag \(has \$existingTag \$served\) -\}\}\{\{- \$existingTag -\}\}')


def main() -> int:
    for f in (HELPERS, GATE):
        if not f.is_file():
            print(f"::error::{f} not found — run from the repo root")
            return 1
    helpers = HELPERS.read_text()
    if 'define "inst.emitTagFrom"' not in helpers:
        print("::error::_helpers.tpl no longer defines inst.emitTagFrom. If the resolution moved "
              "back beside its own lookup, only the arm that does not matter is testable again "
              "and #477's fix is unverifiable.")
        return 1

    data = render(helpers)
    if "__error__" in data:
        print("::error::probe render failed — cannot vouch for a helper it could not run")
        print(data["__error__"])
        return 1

    failures = []
    for i, (label, _w, _s, _e, want) in enumerate(CASES):
        got = data.get(f"case{i}", "<MISSING>")
        ok = got == want
        print(f"  {'PASS' if ok else 'FAIL'}  {label}\n          -> {got!r}"
              + ("" if ok else f"   WANT {want!r}"))
        if not ok:
            failures.append(f"{label}: want {want!r}, got {got!r}")

    # Mutation: delete the fix's arm. The bump-instant case must stop preserving the live version.
    mutant = FIX_ARM.sub("{{- else if false -}}", helpers, count=1)
    if mutant == helpers:
        failures.append("could not construct the mutant: the fix's arm no longer matches the "
                        "pattern this gate deletes, so the self-check cannot run. Update FIX_ARM.")
    else:
        mdata = render(mutant)
        if "__error__" in mdata:
            failures.append("the mutant did not render; self-check inconclusive")
        elif mdata.get("case1") == "v1-8-88":
            failures.append("MUTANT NOT CAUGHT: with the preservation arm deleted, the bump-instant "
                            "case still returned the live version. This gate cannot detect the "
                            "regression it exists for.")
        else:
            print("  PASS  self-check: a mutant with the preservation arm deleted IS caught")

    # Structural: all three call sites must read ONE resolution. Asserted positively.
    src = GATE.read_text()
    for needle, why in [
        ('{{- $emit := include "inst.emitTag" (list $top $c.kind $c.name $c.version $allCRDs) }}',
         "the single resolution"),
        ('{{- $crd  := ternary "true" "" (ne $emit "") }}', "the gate reading it"),
        ('{{- $exists := include "inst.compositionExistsAtTag" (list $top $c.kind $c.name $emit) }}',
         "existence asked at the tag being emitted"),
        ("apiVersion: composition.krateo.io/{{ $emit }}", "the emitter reading it"),
    ]:
        if needle in src:
            print(f"  PASS  compositions.yaml has {why}")
        else:
            failures.append(f"compositions.yaml no longer has {why} ({needle!r}). If the version is "
                            f"derived at more than one call site again, they can disagree — which "
                            f"is #477.")
    if 'include "inst.crdExists" (list $c.kind $c.version' in src:
        failures.append("compositions.yaml derives the gate from inst.crdExists at the TARGET "
                        "version again, which is the defect: at the bump instant it is false and "
                        "the $exists escape is never consulted.")
    else:
        print("  PASS  the gate no longer asks inst.crdExists for the target version directly")

    if failures:
        print("::error::emit-tag gate: the emission version is not resolved as required (#477). "
              "The failure this guards PRUNES LIVE COMPOSITIONS and reports nothing:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"\nemit-tag gate: OK — {len(CASES)} arms, the mutant is caught, and all three call sites "
          f"read one resolution.")
    print("NOT asserted: inst.emitTag's own lookup. Nothing cluster-free can show it finds the "
          "instance, or looks in the right namespace.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
