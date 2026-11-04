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

    # Structural. The behavioural arms above prove inst.emitTagFrom resolves correctly. These
    # assert that exactly ONE place in the chart makes the emission decision, which is a different
    # claim and the one that actually broke: #665 fixed the live gate in compositions.yaml and left
    # withheld-components.yaml holding a SECOND copy of the gate expression keyed on the pinned
    # version, so at the bump instant the reporter called "withheld" the component the gate emits.
    # Pinning literal lines in compositions.yaml could not catch that -- those lines were correct.
    # What catches it is ownership: the sub-checks may appear in _helpers.tpl and nowhere else.
    helpers = HELPERS.read_text()
    src = GATE.read_text()

    if '{{- define "inst.emitDecision" -}}' in helpers:
        print("  PASS  the decision is defined in _helpers.tpl")
    else:
        failures.append("inst.emitDecision is not defined in _helpers.tpl. The emission decision "
                        "must live in exactly one place; without it each consumer derives its own.")

    dec = helpers[helpers.index('{{- define "inst.emitDecision" -}}'):]
    dec = dec[:dec.index('{{- define "', 10)] if '{{- define "' in dec[10:] else dec
    if 'include "inst.compositionExistsAny"' in dec:
        print("  PASS  the decision's own $exists escape is version-agnostic")
    else:
        failures.append("inst.emitDecision's $exists escape does not use inst.compositionExistsAny. "
                        "An escape that exists to STOP a prune must be permissive; keyed on the "
                        "emit tag it prunes the very instance it protects when the CRD serves both "
                        "the old and the new version and the instance has not migrated yet.")

    OWNED = ["inst.emitTag", "inst.depsReady", "inst.requiresApiGroups",
             "inst.compositionExistsAny", "inst.dependentsGone"]
    strays = {}
    for f in sorted(Path("chart/templates").glob("*.yaml")):
        body = f.read_text()
        hits = [h for h in OWNED if 'include "%s"' % h in body]
        if hits:
            strays[f.name] = hits
    if strays:
        for name, hits in strays.items():
            failures.append("%s calls %s directly. Every sub-check of the emission decision belongs "
                            "to inst.emitDecision; a second caller is a second gate, and two gates "
                            "cannot be kept in step by care -- that is how the reporter came to "
                            "contradict the gate it reports on." % (name, ", ".join(hits)))
    else:
        print("  PASS  no template file re-derives any sub-check of the decision (%d scanned)"
              % len(list(Path("chart/templates").glob("*.yaml"))))

    for needle, why in [
        ('{{- $emit := include "inst.emitDecision" (list $top $c $components $allCRDs) }}',
         "the gate reading the decision"),
        ("apiVersion: composition.krateo.io/{{ $emit }}",
         "the emitter reading the SAME value the gate read"),
    ]:
        if needle in src:
            print(f"  PASS  compositions.yaml has {why}")
        else:
            failures.append(f"compositions.yaml no longer has {why} ({needle!r}). The gate and the "
                            f"emitted apiVersion must be the one value, or they can disagree — #477.")

    readers = [f.name for f in sorted(Path("chart/templates").glob("*.yaml"))
               if 'include "inst.emitDecision"' in f.read_text()]
    if "withheld-components.yaml" in readers:
        print("  PASS  the withheld reporter reads the decision rather than a copy of it")
    else:
        failures.append("withheld-components.yaml does not call inst.emitDecision. It reports which "
                        "components were withheld, so if it restates the gate instead of reading it, "
                        "its report can contradict the render it describes.")

    # The helpers that look a composition up must resolve the tag through inst.emitTag, not build it
    # from a pinned version with inst.apiVersion. A cluster-free gate cannot show a lookup finds the
    # right object; it CAN show which version the lookup is keyed on, and that is where the defect
    # lives. Each of these three decides something destructive or reportable about a LIVE object:
    # dependentsGone releases a draining component for deletion, compositionExists keeps a
    # CompositionDefinition registered (and its generated CRD alive), withheldReason explains the
    # outcome to an operator.
    def body(name):
        i = helpers.index('{{- define "%s" -}}' % name)
        j = helpers.find('{{- define "', i + 10)
        return helpers[i:j if j != -1 else len(helpers)]

    for name in ["inst.dependentsGone", "inst.compositionExists", "inst.withheldReason"]:
        try:
            b = body(name)
        except ValueError:
            failures.append("%s is not defined in _helpers.tpl; this gate asserts how it resolves "
                            "the version and cannot do that if it is gone." % name)
            continue
        if 'include "inst.compositionExistsAny"' not in b:
            failures.append("%s does not ask inst.compositionExistsAny. It asks whether a LIVE "
                            "composition still exists, and that question must be version-AGNOSTIC: "
                            "keyed on any single tag -- the pin OR the tag we would emit at -- it "
                            "misses an instance that has not migrated yet while the CRD serves both "
                            "versions, and concludes a thing that exists is gone." % name)
        elif name != "inst.withheldReason" and ('include "inst.apiVersion"' in b
                                                or 'include "inst.emitTag"' in b):
            # Exempting the reporter is deliberate, not a loosening to make the gate pass.
            # inst.dependentsGone and inst.compositionExists answer ONLY "does it exist", so any
            # single derived version in them is keying that answer on one tag -- the defect.
            # inst.withheldReason answers two different questions and needs both helpers: emitTag
            # to say "no CRD serves version X at all", compositionExistsAny to say whether a live
            # instance could have kept it alive. Forbidding the pair there would force the reporter
            # back onto one question and reintroduce the disagreement it exists to expose.
            failures.append("%s derives a single version (inst.apiVersion or inst.emitTag) as well "
                            "as asking inst.compositionExistsAny. inst.emitTag PREFERS THE TARGET, "
                            "which is right for the emitted apiVersion and wrong for an existence "
                            "check; mixing them is how this defect recurred once already." % name)
        else:
            print("  PASS  %s asks existence version-agnostically (any served tag)" % name)

    if 'include "inst.crdExists" (list $c.kind $c.version' in helpers + src:
        failures.append("a gate helper derives emission from inst.crdExists at the PINNED version "
                        "again, which is the defect: at the bump instant it is false and the "
                        "$exists escape is never consulted.")
    else:
        print("  PASS  nothing derives emission from inst.crdExists at the pinned version")

    if failures:
        print("::error::emit-tag gate: the emission version is not resolved as required (#477). "
              "The failure this guards PRUNES LIVE COMPOSITIONS and reports nothing:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"\nemit-tag gate: OK — {len(CASES)} arms, the mutant is caught, the decision exists in "
          f"exactly one place, and every helper that asks about a live composition keys its lookup "
          f"on the resolved tag.")
    print("NOT asserted: inst.emitTag's own lookup. Nothing cluster-free can show it finds the "
          "instance, or looks in the right namespace.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
