#!/usr/bin/env python3
"""Exercise hack/chart-root-schema-gate.py's verdict rules against fixtures, with no registry.

WHY THIS SUITE IS MOSTLY ABOUT FALSE POSITIVES. The gate's first two drafts were both too broad,
and each time the thing that caught it was running against a deliberately MIXED set of real
charts rather than only the one that failed:

  draft 1  refused any closed root            -> refused `frontend` 1.8.15, which is LIVE and
                                                 Ready on 057 (closed root, but declares global)
  draft 2  refused any root `required:`       -> refused agentgateway / cert-manager-issuers /
                                                 external-dns, whose required keys are all
                                                 satisfied by their own values.yaml defaults,
                                                 because helm merges defaults BEFORE validating

A gate that refuses working charts is worse than no gate: it teaches an operator to skip it. So
every rule here is asserted in BOTH directions -- the defect is caught AND the legitimate shape
next to it is passed.

It imports the real module rather than reimplementing the rules.
"""
import importlib.util
import pathlib
import sys

HACK = pathlib.Path(__file__).resolve().parents[2] / "hack" / "chart-root-schema-gate.py"
spec = importlib.util.spec_from_file_location("gate", HACK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# (label, schema, defaults, expected_ok)
CASES = [
    # the global conjunction, both directions
    ("closed root, no global -> REFUSE",
     {"additionalProperties": False, "properties": {}}, {}, False),
    ("closed root, declares global -> ok (frontend 1.8.15's real shape)",
     {"additionalProperties": False, "properties": {"global": {"type": "object"}}}, {}, True),
    ("open root, no global -> ok (builders-page's real shape)",
     {"additionalProperties": True, "properties": {}}, {}, True),

    # root required, both directions
    ("root required not defaulted -> REFUSE",
     {"required": ["clickhouse"], "properties": {}}, {}, False),
    ("root required defaulted by the chart -> ok (agentgateway 0.10.0's real shape)",
     {"required": ["gatewayClassName", "controllerName", "gateway"], "properties": {}},
     {"gatewayClassName": "x", "controllerName": "y", "gateway": {}}, True),
    ("root required PARTLY defaulted -> REFUSE, naming only the unmet key",
     {"required": ["acme", "internalCA"], "properties": {}}, {"acme": {}}, False),

    # shapes that must never be flagged
    ("closed SUB-object -> ok (rdc, image: legitimate)",
     {"properties": {"rdc": {"additionalProperties": False}}}, {}, True),
    ("empty required -> ok", {"required": [], "properties": {}}, {}, True),
    ("no schema at all -> ok", None, {}, True),
    ("unspecified additionalProperties -> ok", {"properties": {}}, {}, True),

    # both defects at once must still refuse
    ("closed+no-global AND unmet required -> REFUSE",
     {"additionalProperties": False, "required": ["seal"], "properties": {}}, {}, False),
]


def main():
    failures = []
    for label, schema, defaults, want in CASES:
        ok, reason = gate.verdict(schema, defaults)
        if ok != want:
            failures.append(f"{label}: ok={ok}, wanted {want}  [{reason}]")
        elif not ok and "required" in label and "PARTLY" in label and "acme" in reason:
            failures.append(f"{label}: reason names a key that WAS defaulted: {reason}")

    # The suite must exercise both verdicts, or it proves only one half.
    verdicts = {c[3] for c in CASES}
    if verdicts != {True, False}:
        failures.append(f"cases reach only {verdicts} -- one verdict is untested")

    # The module's own self-test must pass, since the gate's claim rests on it.
    rc = gate.main(["--self-test"])
    if rc != 0:
        failures.append(f"--self-test returned {rc}")

    if failures:
        print("check-chart-root-schema-gate: FAILED")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"check-chart-root-schema-gate: OK — {len(CASES)} cases, both verdicts reached, "
          f"self-test green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
