#!/usr/bin/env python3
"""CI guard: exercise inst.withheldReason — the explanation a withheld component gives for its
own absence.

WHY IT EXISTS (#614). compositions.yaml withholds a component silently: every
CompositionDefinition reports True/True, the composition CR is simply never created, and there is
no event, no failed resource and no degraded condition anywhere. An operator who enables the
observability tier without `features.ingress` gets an install that looks entirely healthy and
emits nothing.

AND THE EXPLANATION SITS TWO LEVELS FROM THE THING THEY ENABLED. `krateo-observability` declares
no unsatisfied API group of its own — it is withheld because `clickhouse-operator` is, and
`clickhouse-operator`'s declaration is the only place the real condition is recorded, on a
component the operator had no reason to look at. A prerequisite discoverable only by walking the
dependency graph by hand is, in practice, not declared at all.

MEASURED AS A REGRESSION, not a standing gap: 0.3.477 has no requiresApiGroups and installed the
whole tier on a fresh cluster with no cert-manager; 0.3.478 introduced them (#594) and a fresh
install stops at 33 of 40 declared CRs. The snowplow bench bracketed it from outside — 0.3.468 at
40/40, 0.3.506 at 33/40.

WHAT THIS ASSERTS. It renders the REAL _helpers.tpl against synthetic CRD lists, so no cluster and
no timing are involved:

  1. a component 2, 3 and 4 levels above the cause NAMES the cause and the group
  2. the component that owns the unmet group says so directly
  3. CONTROL: with the group served, nothing blames an unmet group
  4. no message ends in a dangling separator (an empty list rendered into prose)
  5. a MUTANT with the transitive arm removed is CAUGHT

Assertion 3 keys on the BLAME ("does not serve"), not on the topic ("API group"). An earlier
version of this test keyed on the topic and failed the control, because the correct message for a
satisfied component says "its declared API groups are present" — the check matched prose ABOUT
groups rather than a blame OF groups, which is the same defect as a gate satisfied by the comment
above the thing it checks.

WHAT A GREEN RUN DOES NOT COVER: it exercises the reporter, not the gate. compositions.yaml's own
emission decision is asserted elsewhere; a reporter that agrees with a wrong gate would pass here.
It also never calls `lookup`, so it cannot show that the CRD list the chart threads in at runtime
is the one it would see.
"""
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HELPERS = Path("chart/templates/_helpers.tpl")
BLAME = "does not serve"

COMPONENTS = [
    {"name": "clickhouse-operator", "kind": "ClickhouseOperator", "version": "0.1.0",
     "deps": [], "requiresApiGroups": ["cert-manager.io"]},
    {"name": "mongodb-operator", "kind": "MongodbOperator", "version": "0.1.0",
     "deps": [], "requiresApiGroups": ["cert-manager.io"]},
    {"name": "krateo-observability", "kind": "KrateoObservability", "version": "0.1.23",
     "deps": ["clickhouse-operator", "mongodb-operator"]},
    {"name": "otel-collector-deployment", "kind": "OtelCollectorDeployment", "version": "0.3.11",
     "deps": ["krateo-observability"]},
    {"name": "otel-collector-daemonset", "kind": "OtelCollectorDaemonset", "version": "0.1.16",
     "deps": ["otel-collector-deployment"]},
]
BYNAME = {c["name"]: c for c in COMPONENTS}


def crd(group, kind="X", vers=("v1",)):
    return {"spec": {"group": group, "names": {"kind": kind},
                     "versions": [{"name": v, "served": True} for v in vers]}}


def own_crds():
    return [crd("composition.krateo.io", c["kind"],
                ["v" + str(c["version"]).replace(".", "-")]) for c in COMPONENTS]


NO_CM = own_crds()
WITH_CM = own_crds() + [crd("cert-manager.io", "Certificate")]

#  (label, target, allCRDs, must_name_cause, is_control)
CASES = [
    ("#614: krateo-observability, 2 levels above the cause", "krateo-observability",  NO_CM,   True,  False),
    ("the component that OWNS the unmet group",              "clickhouse-operator",   NO_CM,   False, False),
    ("3 levels up: otel-collector-deployment",               "otel-collector-deployment", NO_CM, True, False),
    ("4 levels up: otel-collector-daemonset",                "otel-collector-daemonset",  NO_CM, True, False),
    ("CONTROL: group served -> must not blame a group",      "krateo-observability",  WITH_CM, False, True),
    ("CONTROL: the operator itself, group served",           "clickhouse-operator",   WITH_CM, False, True),
]

PROBE = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\ndata:\n"
    "{{- range $i, $case := .Values.cases }}\n"
    '  case{{ $i }}: {{ (include "inst.withheldReason" '
    "(list $ $case.target $case.components $case.allCRDs)) | quote }}\n"
    "{{- end }}\n"
)


def render(helpers_text):
    work = Path(tempfile.mkdtemp(prefix="withheld-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        (chart / "templates" / "_helpers.tpl").write_text(helpers_text)
        (chart / "templates" / "probe.yaml").write_text(PROBE)
        # namespaces.krateo is required because inst.withheldReason now asks
        # inst.compositionExistsAny, which does a namespaced lookup. Without it the helper
        # panics on a nil map and this gate reports "probe render failed" -- which is how it
        # surfaced, rather than as a wrong reason. Under `helm template` lookup is DISABLED,
        # so the existence answer here is always "" (no live instance); these cases exercise
        # the reasons given when nothing can be kept alive, which is what they are about.
        (chart / "values.yaml").write_text("cases: []\nnamespaces:\n  krateo: krateo-system\n")
        vals = work / "cases.yaml"
        vals.write_text(yaml.safe_dump({"cases": [
            {"target": BYNAME[t], "components": copy.deepcopy(COMPONENTS), "allCRDs": a}
            for _l, t, a, _n, _c in CASES]}, default_flow_style=False))
        p = subprocess.run(["helm", "template", "probe", str(chart), "-f", str(vals)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return {"__error__": p.stderr.strip()[:1200]}
        doc = next(d for d in yaml.safe_load_all(p.stdout)
                   if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        return doc.get("data") or {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


TRANSITIVE_ARM = re.compile(
    r'\{\{- if \$blockers -\}\}\s*\{\{- printf "withheld: a dependency requires an API group '
    r'the cluster does not serve[^\n]*\n', re.S)


def main() -> int:
    if not HELPERS.is_file():
        print(f"::error::{HELPERS} not found — run from the repo root")
        return 1
    helpers = HELPERS.read_text()
    if 'define "inst.withheldReason"' not in helpers:
        print("::error::_helpers.tpl no longer defines inst.withheldReason — this gate is "
              "testing nothing. Point it at the new template, or if the reporter was removed, "
              "#614's silence is back and that is the regression.")
        return 1

    data = render(helpers)
    if "__error__" in data:
        print("::error::probe render failed — the gate cannot vouch for a helper it could not run")
        print(data["__error__"])
        return 1
    if len(data) != len(CASES):
        print(f"::error::expected {len(CASES)} cases, got {len(data)}")
        return 1

    failures = []
    for i, (label, _t, _a, must_name, is_control) in enumerate(CASES):
        v = data.get(f"case{i}", "")
        bad = []
        if must_name and "clickhouse-operator needs cert-manager.io" not in v:
            bad.append("does not name the real cause (dependency + group)")
        if is_control and BLAME in v:
            bad.append(f"blames an unmet group ({BLAME!r}) while the group IS served")
        if v.rstrip().endswith("--"):
            bad.append("ends in a dangling separator — an empty list rendered into prose")
        print(f"  {'FAIL' if bad else 'PASS'}  {label}\n          {v[:150]}")
        for b in bad:
            failures.append(f"{label}: {b}")

    mutant = TRANSITIVE_ARM.sub('{{- if false -}}{{- "" -}}\n', helpers, count=1)
    if mutant == helpers:
        failures.append("could not construct the mutant: the transitive arm no longer matches the "
                        "pattern this gate deletes, so the self-check cannot run and the gate is "
                        "unverified. Update TRANSITIVE_ARM.")
    else:
        mdata = render(mutant)
        if "__error__" in mdata:
            failures.append("the mutant did not render, so the self-check is inconclusive")
        elif "clickhouse-operator needs cert-manager.io" in mdata.get("case0", ""):
            failures.append("MUTANT NOT CAUGHT: with the transitive arm deleted, krateo-observability "
                            "still named the cause. This gate cannot detect the regression it exists for.")
        else:
            print("  PASS  self-check: a mutant with the transitive arm deleted IS caught")

    if failures:
        print("::error::withheld-reason gate: a withheld component does not explain its own absence "
              "(#614). The failure this guards is silent — True/True everywhere and no CR:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"\nwithheld-reason gate: OK — {len(CASES)} cases, the cause is named 2/3/4 levels up, "
          f"both controls hold, no dangling separators, and the mutant is caught.")
    print("NOT asserted: the emission gate itself. This exercises the REPORTER; a reporter that "
          "agreed with a wrong gate would pass here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
