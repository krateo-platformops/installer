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
# The exemption fixtures inject their OWN entry rather than naming a real one. MISSING_SCHEMA_EXEMPT
# is deliberately empty in the gate, and a suite that asserted behaviour through a production entry
# would have broken the moment that entry was removed -- which is a test depending on configuration
# rather than on the code it covers.
EXEMPT_PROBE = "exempt-probe-not-a-real-component"
gate.MISSING_SCHEMA_EXEMPT[EXEMPT_PROBE] = "injected by this suite; not a real exemption"


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
    # A chart with NO root values.schema.json cannot be registered: crdgen builds the composition
    # CRD from it. This suite asserted the OPPOSITE until 2026-11-17 -- "no schema at all -> ok"
    # was a deliberate pass fixture -- which is why nothing refused the kyverno 3.9.2 pin at pin
    # time and the failure surfaced only as a Synced=False CD on a cluster.
    ("no schema at all -> REFUSE (crdgen has nothing to build a CRD from)",
     None, {}, False, set()),
    ("no schema but on the exempt list -> ok, as a TRACKED gap",
     None, {}, True, set(), EXEMPT_PROBE),
    # The exemption must not outlive its reason. If an exempt chart grows a root schema, the gate
    # refuses until the entry is removed -- otherwise a carve-out becomes permanent in silence.
    ("exempt component that NOW has a schema -> REFUSE (stale exemption)",
     {"properties": {"global": {}}}, {}, False, set(), EXEMPT_PROBE),
    ("unspecified additionalProperties -> ok", {"properties": {}}, {}, True, set()),

    # both defects at once must still refuse
    ("closed+no-global AND unmet required -> REFUSE",
     {"additionalProperties": False, "required": ["seal"], "properties": {}}, {}, False, None),
]



# ---------------------------------------------------------------------------------------------
# dependency_problems(): the class verdict() CANNOT see.
#
# helm validates the parent's schema against COALESCED values, and coalesceDeps always creates a
# key per dependency holding that dependency's defaults. So a closed root missing a dependency key
# passes verdict() -- which reasons about the schema alone -- and then fails every helm install.
# Found by core-provider on 2026-11-17 while reviewing the kyverno wrapper's schema, where my
# closed root declared `global` only and would have been refused on every install and upgrade.
#
# (label, root schema, dependencies, expected ok, substring the reason must contain)
DEP_CASES = [
    ("open root, dependency undeclared -> ok (nothing is forced)",
     {"properties": {}}, [{"name": "kyverno"}], True, "not closed"),
    ("unset additionalProperties counts as open -> ok",
     {"properties": {"global": {}}}, [{"name": "kyverno"}], True, "not closed"),
    ("closed root declaring the dependency -> ok",
     {"additionalProperties": False, "properties": {"global": {}, "kyverno": {}}},
     [{"name": "kyverno"}], True, "declares all 1"),
    ("closed root, NO dependencies at all -> ok",
     {"additionalProperties": False, "properties": {"global": {}}}, [], True, "no dependencies"),

    # the defect this check exists for
    ("closed root missing an UNCONDITIONAL dependency -> REFUSE, every install",
     {"additionalProperties": False, "properties": {"global": {}}},
     [{"name": "kyverno"}], False, "ALWAYS coalesces"),

    # the predicate must be WEAKER for a conditional dependency: helm processes dependencies
    # before building render values, so a disabled one may never be coalesced. Claiming "every
    # install" there would be a claim this function cannot support.
    ("closed root missing a CONDITIONAL dependency -> REFUSE, but only when the condition is true",
     {"additionalProperties": False, "properties": {"global": {}}},
     [{"name": "reports-server", "condition": "reports-server.enabled"}], False,
     "WHEN THE CONDITION IS TRUE"),
    ("a conditional miss must NOT be reported as failing every install",
     {"additionalProperties": False, "properties": {"global": {}}},
     [{"name": "x", "condition": "x.enabled"}], False, "costs nothing"),

    # aliasing renames the subchart BEFORE coalescing, so the alias is the key that matters
    ("closed root declaring the ALIAS -> ok",
     {"additionalProperties": False, "properties": {"global": {}, "pg": {}}},
     [{"name": "cloudnative-pg", "alias": "pg"}], True, "declares all 1"),
    ("closed root declaring the CHART NAME while the dep is ALIASED -> REFUSE",
     {"additionalProperties": False, "properties": {"global": {}, "cloudnative-pg": {}}},
     [{"name": "cloudnative-pg", "alias": "pg"}], False, "ALWAYS coalesces"),

    # CONTROL: the shape actually being published must pass, or the gate refuses our own fix
    ("CONTROL the kyverno wrapper as designed -> ok",
     {"additionalProperties": False,
      "properties": {"global": {"type": "object", "additionalProperties": True},
                     "kyverno": {"type": "object", "additionalProperties": True}}},
     [{"name": "kyverno", "version": "3.9.2"}], True, "declares all 1"),
]

def main():
    failures = []
    for case in CASES:
        label, schema, defaults, want, cv_props = case[:5]
        # 6th element, optional: the component name, which selects the missing-schema exemption.
        name = case[5] if len(case) > 5 else None
        ok, reason = gate.verdict(schema, defaults, cv_props, name)
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

    for label, sch, deps, want, must in DEP_CASES:
        ok, reason = gate.dependency_problems(sch, deps)
        if ok != want:
            failures.append(f"[dep] {label}: ok={ok}, wanted {want}  [{reason}]")
        elif must not in reason:
            failures.append(f"[dep] {label}: reason lacks {must!r}: {reason}")

    # A conditional miss must never borrow the unconditional claim. Asserted directly, because the
    # two differ only in wording and a copy-paste would make the gate overclaim.
    _, r_cond = gate.dependency_problems(
        {"additionalProperties": False, "properties": {}}, [{"name": "z", "condition": "z.on"}])
    if "ALWAYS coalesces" in r_cond:
        failures.append("[dep] a CONDITIONAL miss is reported as failing every install: " + r_cond)

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
    print(f"check-chart-root-schema-gate: OK — {len(CASES)} verdict + {len(DEP_CASES)} dependency "
          f"cases, both verdicts reached, "
          f"self-test green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
