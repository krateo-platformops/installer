#!/usr/bin/env python3
"""CI guard: exercise inst.readyFromConditions — the ONE place this chart decides whether a
dependency counts as ready.

WHY IT NEEDS A TEST AT ALL. `inst.ready` has to `lookup` the live object, and `helm template`
DISABLES lookup, so for as long as the decision lived inside `inst.ready` it could not be
exercised without a cluster. It was a single line reading `status == "True"` and nothing ever
checked it — which is how it went unexamined until core-provider#165 asked what it does with
`Unknown`. The decision is now split into inst.readyFromConditions, which takes a conditions
list and nothing else, so it is testable; this is that test.

WHAT IT MUST DISCRIMINATE, and the middle two are the point:

    Ready=True                            -> ready
    Ready=Unknown reason=ZeroEvidence     -> ready      the cdc evaluated NONE of the children
    Ready=Unknown reason=Creating         -> NOT ready   a genuinely converging dep MUST block
    Ready=False  reason=Creating          -> NOT ready   core-provider#230's new emitter: a
                                                         composition whose render shrank
    Ready=False  reason=Unavailable       -> NOT ready
    Ready=False                           -> NOT ready

Accepting bare `Unknown` would turn every mid-creation dependency into "ready" and let
dependants render against children that do not exist yet. Rejecting ZeroEvidence would gate
the 8 components that dep on model-configs, permanently, since zero-evidence is a steady
state. Both failure directions are live, which is why both arms are asserted.

It renders the REAL _helpers.tpl in a throwaway chart rather than reimplementing the logic:
a test that restates the rule in Python would agree with itself.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HELPERS = Path("chart/templates/_helpers.tpl")

CASES = [
    ("Ready=True",                        [{"type": "Ready", "status": "True"}],                                  "true"),
    ("Ready=True + noise",                [{"type": "Synced", "status": "False"}, {"type": "Ready", "status": "True"}], "true"),
    ("Unknown/ZeroEvidence",              [{"type": "Ready", "status": "Unknown", "reason": "ZeroEvidence"}],      "true"),
    ("Unknown/Creating must BLOCK",       [{"type": "Ready", "status": "Unknown", "reason": "Creating"}],          ""),
    ("Unknown with NO reason must BLOCK", [{"type": "Ready", "status": "Unknown"}],                                ""),
    ("Ready=False",                       [{"type": "Ready", "status": "False"}],                                 ""),
    # core-provider#230 ships a NEW emitter of Ready=False: a composition whose render produced
    # FEWER children than it was managing now reports condition.Creating(), which is
    # Type=Ready / Status=False / Reason=Creating (unstructured-runtime v1.6.0), rather than
    # Available. The bare Ready=False case above already covers the BRANCH, so these two add no
    # new code path — they pin the CONTRACT, named for the states that will actually arrive.
    # Why that is worth two lines: #230's whole purpose is that a dep mid-shrink must not be
    # treated as converged, and the tempting "fix" when rolls start converging more slowly is to
    # accept False/Creating here as "it is only converging". That would restore exactly the false
    # convergence #230 removes, and without these cases nothing would fail.
    ("False/Creating must BLOCK",         [{"type": "Ready", "status": "False", "reason": "Creating"}],           ""),
    ("False/Unavailable must BLOCK",      [{"type": "Ready", "status": "False", "reason": "Unavailable"}],        ""),
    ("no Ready condition at all",         [{"type": "Synced", "status": "True"}],                                 ""),
    ("no conditions",                     [],                                                                     ""),
    # A non-Ready condition carrying the magic reason must not satisfy the gate.
    ("Synced=Unknown/ZeroEvidence only",  [{"type": "Synced", "status": "Unknown", "reason": "ZeroEvidence"}],     ""),
]


def main() -> int:
    if not HELPERS.is_file():
        print(f"::error::{HELPERS} not found — run from the repo root")
        return 1

    work = Path(tempfile.mkdtemp(prefix="ready-gate-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        # The real helpers, verbatim. Any drift between the shipped rule and the tested rule
        # is impossible by construction.
        shutil.copy(HELPERS, chart / "templates" / "_helpers.tpl")
        (chart / "templates" / "probe.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\ndata:\n'
            '{{- range $i, $c := .Values.cases }}\n'
            '  case{{ $i }}: {{ (include "inst.readyFromConditions" (list $c.conditions)) | quote }}\n'
            '{{- end }}\n')
        (chart / "values.yaml").write_text("cases: []\n")

        vals = work / "cases.yaml"
        import yaml
        vals.write_text(yaml.safe_dump(
            {"cases": [{"conditions": c} for _, c, _ in CASES]}, default_flow_style=False))

        p = subprocess.run(
            ["helm", "template", "probe", str(chart), "-f", str(vals)],
            capture_output=True, text=True)
        if p.returncode != 0:
            print("::error::render failed")
            print(p.stderr.strip()[:1500])
            return 1

        doc = next(d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        data = doc.get("data") or {}

        failures = []
        for i, (name, _conds, want) in enumerate(CASES):
            got = data.get(f"case{i}")
            if got is None:
                failures.append(f"{name}: no output rendered (helper missing?)")
                continue
            verdict = "READY    " if got == "true" else "NOT READY"
            print(f"  {verdict}  {name}" + ("" if got == want else f"   <-- WANT {want!r}, GOT {got!r}"))
            if got != want:
                failures.append(f"{name}: want {want!r}, got {got!r}")

        if failures:
            print("::error::ready-gate: inst.readyFromConditions does not discriminate as required "
                  "(core-provider#165):")
            for f in failures:
                print(f"  {f}")
            return 1
        print(f"ready-gate: OK — all {len(CASES)} cases discriminate, "
              f"incl. Unknown/ZeroEvidence READY and Unknown/Creating BLOCKED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
