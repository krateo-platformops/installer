#!/usr/bin/env python3
"""Exercise hack/chart-root-schema-gate.py's verdict rules against fixtures, with no registry.

WHY THIS SUITE IS MOSTLY ABOUT FALSE POSITIVES. The gate's first two drafts were both too broad,
and each time the thing that caught it was running against a deliberately MIXED set of real
charts rather than only the one that failed:

  draft 1  refused any closed root            -> refused `frontend` 1.8.15, which is LIVE and
                                                 Ready on 057 (closed root, but declares global)
  draft 2  refused any root `required:`       -> refused agentgateway / cert-manager-issuers /
                                                 external-dns. I recorded this as a FALSE
                                                 POSITIVE because their required keys are
                                                 satisfied by their own values.yaml defaults,
                                                 helm merging defaults BEFORE validating.

THAT RECORD WAS WRONG AND THIS SUITE ENCODED THE ERROR. The exemption holds for `helm install` and
fails for the path every pinned component takes. A pin is deployed as a COMPOSITION: crdgen builds
a CRD from the chart's values.schema.json -- `required:` included -- and the CR's spec is built from
the INSTALLER's componentValues. The chart's own values.yaml never participates.

Proven on the one instance that reached a cluster, installer#489: cert-manager-issuers 0.10.0 DOES
default `acme` in its values.yaml, and the composition was STILL rejected, because
componentValues.cert-manager-issuers declared `acme` as an object with no properties, so the
installer could only ever emit `{}`.

So draft 2's three refusals were TRUE positives. The reasoning was wrong, not the verdict -- and a
case in this very list asserted the wrong one as expected behaviour, naming agentgateway 0.10.0 as
its example. A test that encodes the mistake is the mistake's strongest defender.

A gate that refuses working charts is still worse than no gate: it teaches an operator to skip it.
So every rule here is asserted in BOTH directions -- the defect is caught AND the legitimate shape
next to it is passed. What changed is WHICH shape is legitimate: not "the chart defaults it" but
"the installer's componentValues declares it AND gives it a default".

Cases are (label, schema, defaults, expected_ok, cv_props); cv_props is the installer's
componentValues surface for that component, which is what verdict() now decides against.

It imports the real module rather than reimplementing the rules.
"""
import importlib.util
import pathlib
import sys

HACK = pathlib.Path(__file__).resolve().parents[2] / "hack" / "chart-root-schema-gate.py"
spec = importlib.util.spec_from_file_location("gate", HACK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

# (label, schema, defaults, expected_ok, cv_props)
CASES = [
    # the global conjunction, both directions
    ("closed root, no global -> REFUSE",
     {"additionalProperties": False, "properties": {}}, {}, False, set()),
    ("closed root, declares global -> ok (frontend 1.8.15's real shape)",
     {"additionalProperties": False, "properties": {"global": {"type": "object"}}}, {}, True, set()),
    ("open root, no global -> ok (builders-page's real shape)",
     {"additionalProperties": True, "properties": {}}, {}, True, set()),

    # root required: the verdict turns on componentValues, NOT on the chart's own defaults.
    ("root required, no componentValues block at all -> REFUSE",
     {"required": ["clickhouse"], "properties": {}}, {}, False, None),

    # THE CORRECTION. This case previously asserted `-> ok`, naming agentgateway 0.10.0. Its real
    # shape: the chart defaults all three in values.yaml AND the installer declares all three --
    # each with `default: null`. So an install setting no componentValues emits a CR missing them.
    ("root required, chart-defaulted but componentValues has NO default -> REFUSE "
     "(agentgateway 0.10.0's real shape)",
     {"required": ["gatewayClassName", "controllerName", "gateway"], "properties": {}},
     {"gatewayClassName": "x", "controllerName": "y", "gateway": {}}, False,
     {"gatewayClassName": {"default": False, "inexpressible": False},
      "controllerName": {"default": False, "inexpressible": False},
      "gateway": {"default": False, "inexpressible": False}}),

    # installer#489's shape: declared, but as objects with no properties, so only `{}` is emittable.
    ("root required, componentValues declares them as EMPTY objects -> REFUSE "
     "(cert-manager-issuers 0.10.0, live-verified)",
     {"required": ["acme", "internalCA"], "properties": {}},
     {"acme": {"enabled": False}, "internalCA": {"enabled": True}}, False,
     {"acme": {"default": False, "inexpressible": True},
      "internalCA": {"default": False, "inexpressible": True}}),

    # THE LEGITIMATE SHAPES NEXT TO THEM, which must pass or the gate refuses working charts.
    ("root required, componentValues declares it WITH a default -> ok",
     {"required": ["foo"], "properties": {}}, {}, True,
     {"foo": {"default": True, "inexpressible": False}}),
    ("root required, shorthand set form means expressible-and-defaulted -> ok",
     {"required": ["bar"], "properties": {}}, {}, True, {"bar"}),

    # shapes that must never be flagged
    ("closed SUB-object -> ok (rdc, image: legitimate)",
     {"properties": {"rdc": {"additionalProperties": False}}}, {}, True, set()),
    ("empty required -> ok", {"required": [], "properties": {}}, {}, True, set()),
    ("no schema at all -> ok", None, {}, True, set()),
    ("unspecified additionalProperties -> ok", {"properties": {}}, {}, True, set()),

    # both defects at once must still refuse
    ("closed+no-global AND unmet required -> REFUSE",
     {"additionalProperties": False, "required": ["seal"], "properties": {}}, {}, False, None),
]


def main():
    failures = []
    for label, schema, defaults, want, cv_props in CASES:
        ok, reason = gate.verdict(schema, defaults, cv_props)
        if ok != want:
            failures.append(f"{label}: ok={ok}, wanted {want}  [{reason}]")
        # The old suite asserted a partly-defaulted REFUSE must name ONLY the unmet key. That rested
        # on chart defaults deciding the verdict, which they do not. The reason must instead name
        # WHICH failure mode applies, because the three need different fixes: declare the key, give
        # the object properties, or give it a default.
        elif not ok and schema and (schema.get("required") or []):
            if not any(m in reason for m in ("does not declare", "EMPTY objects", "NO default",
                                             "declares NO componentValues")):
                failures.append(f"{label}: REFUSE reason names no failure mode: {reason}")

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
