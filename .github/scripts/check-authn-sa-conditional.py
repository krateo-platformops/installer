#!/usr/bin/env python3
"""CI guard: componentValues.authn.serviceAccount's conditional, and the CEL-rule count it implies.

WHAT IT GUARDS. `if create is false then name is required (minLength 1)`, mirrored from authn's own
values.schema.json (#170). authn's chart has enforced it with a template `fail` since 0.53.0 and
declared it in its schema at 0.56.0. Without the mirror the installer ACCEPTS the bad combination
at the CR and it fails later at `helm upgrade` -- measured: the installer schema reported 0
violations for `{create: false}` while authn 0.56.0's own schema reported 1.

WHY IT NEEDS A GUARD AT ALL, rather than being self-evident in the diff. crdgen COMPILES this
if/then into an `x-kubernetes-validations` CEL rule. Measured by authn at plumbing v1.100.0 -- the
version core-provider 2.13.117 carries -- and at v1.104.0, identical output, 0 warnings:

    !(has(self.create) && has(self.create) && self.create == false)
      || (has(self.name) && has(self.name) && size(self.name) >= 1)

A CEL rule binds at ADMISSION. An existing object that violates a new rule is not flagged or
deleted -- it becomes silently UNUPDATABLE, and the failure surfaces on whatever next writes it,
usually a controller, far from the cause. That is why alert-provider 0.2.64 -> 0.2.65 looked
cosmetic to two instruments and was not: key sets 36 -> 36, description bytes 931 -> 931, CEL rules
15 -> 17.

So this file asserts BOTH halves:
  1. the conditional is present and behaves (six values, three of which must be refused);
  2. the installer schema compiles to exactly ONE such conditional, so a SECOND one cannot be added
     without this count changing and someone deciding to change it.

Assertion 2 is the one that earns the file. A guard on "the rule exists" passes just as happily
when a second, unreviewed admission constraint appears beside it.

WHAT THIS CANNOT DO. It never runs crdgen, so it cannot prove the emitted CEL text or its estimated
cost. Those were measured out of band: authn ran crdgen at both plumbing versions, and a server
dry-run against krateo-057 accepted the rule on its real path while a control placing the same rule
under two unbounded arrays was refused at factor 1.153434x. check-cel-cost-shape.py covers the cost
SHAPE in CI -- and is deliberately silent here, because it warns only for a keyword under an
unbounded collection and this one has no array above it. That silence and the apiserver's
acceptance are the same property measured two ways.
"""
import json
import pathlib
import sys

SCHEMA = pathlib.Path("chart/values.schema.json")
PATH = ("properties", "componentValues", "properties", "authn", "properties", "serviceAccount")

# Exactly the values authn measured against crdgen's emitted CEL rule, so the JSON-Schema
# semantics here and the admission semantics there are asserted to agree.
CASES = [
    ({"create": False}, False, "no name at all"),
    ({"create": False, "name": ""}, False, "empty name -- the minLength half, which plumbing <v1.95.0 dropped"),
    ({"create": False, "name": "x"}, True, "a name is given"),
    ({}, True, "CONTROL: nothing set, the 057 and demo state"),
    ({"create": True}, True, "CONTROL: create true needs no name"),
    ({"create": True, "name": ""}, True, "CONTROL: the rule must not fire when create is true"),
]
EXPECTED_CONDITIONALS = 1  # componentValues.authn.serviceAccount. Changing this is a DECISION.


def node_at(schema, path):
    n = schema
    for p in path:
        if not isinstance(n, dict) or p not in n:
            return None
        n = n[p]
    return n


SCALAR_TYPES = {"string", "integer", "number", "boolean"}


def count_conditionals(schema):
    """Every if/then pair in the schema -- each compiles to one CEL rule on the Installer CRD."""
    found = []

    def walk(n, p):
        if isinstance(n, dict):
            if "if" in n and "then" in n:
                found.append(".".join(x for x in p if x not in ("properties", "items")) or "<root>")
            for k, v in n.items():
                walk(v, p + [k])
        elif isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, p + [str(i)])

    walk(schema, [])
    return sorted(found)


def count_cel_sources(schema):
    """EVERY construct crdgen compiles into x-kubernetes-validations, not just if/then.

    Counting if/then alone was this gate's blind spot: the claim "exactly one CEL rule on the
    Installer CRD" happened to be true, but a `dependentRequired` or a bounded non-scalar
    `uniqueItems` could be added and the if/then count would not move. A gate whose subject is
    narrower than the claim it implies passes while the claim becomes false.

    The four sources, from plumbing's crdgen at main (read by authn, not measured -- they had no
    build during marketing's hold, and the measurement is noted below):

        1. if/then/else                  emitIfThenElse, transpile_keywords.go:340
        2. dependentRequired             emitDependentRequired, transpile_keywords.go:141
        3. uniqueItems: true             transpile.go:815-831, and ONLY sometimes:
                                           scalar items        -> x-kubernetes-list-type: set (no CEL)
                                           non-scalar + maxItems -> CEL
                                           non-scalar, no maxItems -> emits NOTHING AT ALL
        4. author-written x-kubernetes-validations   passthroughExtensions, :111

    `enum` inside an array item is NOT a source: `items` is transpiled recursively
    (transpile.go:394-395) and copyEnum (:639) puts the values on the typed child node, so the
    apiserver enforces it natively with no CEL and no CEL cost. On an UNTYPED node an enum is
    dropped with a ConstraintDropped warning -- never turned into CEL. `not`, `const`, `pattern`
    and the numeric bounds are all native too.

    TO SETTLE THIS BY MEASUREMENT rather than by crdgen's source: generate the CRD and run
      yq '.. | select(has("x-kubernetes-validations")) | path'
    with the known if/then pair as the positive control -- it MUST appear, or the probe is wrong.

    Returns {source: [paths]}. Native-but-not-CEL constraints (enums, required, patterns, bounds)
    are deliberately NOT counted: they bind at admission too, but this function's subject is CEL.
    """
    out = {"if_then": [], "dependentRequired": [], "uniqueItems_cel": [], "passthrough": []}
    inert = []

    def label(p):
        return ".".join(x for x in p if x not in ("properties", "items")) or "<root>"

    def walk(n, p):
        if isinstance(n, dict):
            if "if" in n and "then" in n:
                out["if_then"].append(label(p))
            if "dependentRequired" in n:
                out["dependentRequired"].append(label(p))
            if "x-kubernetes-validations" in n:
                out["passthrough"].append(label(p))
            if n.get("uniqueItems") is True:
                it = n.get("items") or {}
                if it.get("type") in SCALAR_TYPES:
                    pass  # becomes x-kubernetes-list-type: set, enforced natively
                elif "maxItems" in n:
                    out["uniqueItems_cel"].append(label(p))
                else:
                    inert.append(label(p))
            for k, v in n.items():
                walk(v, p + [k])
        elif isinstance(n, list):
            for i, v in enumerate(n):
                walk(v, p + [str(i)])

    walk(schema, [])
    for k in out:
        out[k] = sorted(out[k])
    out["_inert_uniqueItems"] = sorted(inert)
    return out


def main() -> int:
    if not SCHEMA.is_file():
        print("::error::%s not found -- run from the repo root" % SCHEMA)
        return 1
    try:
        import jsonschema
    except ImportError:
        print("::error::the `jsonschema` package is required (pip install jsonschema). This guard "
              "cannot validate without it, and skipping would make 'no violations' meaningless.")
        return 1

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    sa = node_at(schema, PATH)
    if sa is None:
        print("::error::componentValues.authn.serviceAccount is missing from the schema")
        return 1

    bad = 0

    # --- 1. the conditional is declared, with the shape authn specified
    if "if" not in sa or "then" not in sa:
        print("::error::componentValues.authn.serviceAccount has no if/then. authn's chart refuses "
              "{create: false} with no name since 0.53.0; without the mirror the installer accepts "
              "it at the CR and it fails later at helm upgrade.")
        return 1
    want_if = {"properties": {"create": {"const": False}}, "required": ["create"]}
    if sa["if"] != want_if:
        print("::error::the `if` clause changed. Expected %s, found %s. It mirrors authn's own "
              "values.schema.json (#170); change it there first."
              % (json.dumps(want_if, sort_keys=True), json.dumps(sa["if"], sort_keys=True)))
        bad += 1
    then = sa["then"]
    if then.get("required") != ["name"] or (then.get("properties") or {}).get("name", {}).get("minLength") != 1:
        print("::error::the `then` clause must require `name` with minLength 1. Found required=%r, "
              "name=%s. The minLength half is what plumbing <v1.95.0 silently dropped, so an empty "
              "name would be accepted without it."
              % (then.get("required"), json.dumps((then.get("properties") or {}).get("name"))))
        bad += 1

    # --- 2. it BEHAVES, on exactly the values authn measured against the emitted CEL rule
    v = jsonschema.Draft202012Validator(schema)
    for value, want_ok, label in CASES:
        errs = list(v.iter_errors({"componentValues": {"authn": {"serviceAccount": value}}}))
        got_ok = not errs
        ok = got_ok == want_ok
        bad += 0 if ok else 1
        print("  %-4s %-34s %s  (%s)%s"
              % ("ok" if ok else "FAIL", json.dumps(value),
                 "accepted" if got_ok else "refused", label,
                 "" if ok else "  <-- expected %s" % ("accepted" if want_ok else "refused")))

    # --- 3. the CEL-rule count. A SECOND conditional means a second admission constraint.
    conds = count_conditionals(schema)
    print("  if/then pairs in the schema: %d -> %s" % (len(conds), conds))

    # --- 3b. EVERY other construct that compiles to CEL, because counting if/then alone would
    # let a dependentRequired or a bounded non-scalar uniqueItems add an admission constraint
    # without moving the number this gate asserts on.
    src = count_cel_sources(schema)
    extra = {k: v for k, v in src.items()
             if k not in ("if_then", "_inert_uniqueItems") and v}
    total = sum(len(v) for k, v in src.items() if k != "_inert_uniqueItems")
    print("  CEL-emitting constructs: if_then=%d dependentRequired=%d uniqueItems_cel=%d "
          "passthrough=%d  -> %d CEL rule(s) expected on the CRD"
          % (len(src["if_then"]), len(src["dependentRequired"]),
             len(src["uniqueItems_cel"]), len(src["passthrough"]), total))
    if extra:
        print("::error::%d CEL rule(s) come from a construct other than the counted if/then pair: "
              "%s. Each binds at ADMISSION exactly as an if/then does -- an existing CR that "
              "violates a new one becomes silently UNUPDATABLE. If intended, say so here "
              "deliberately and check no live CR violates it first."
              % (total - len(src["if_then"]), extra))
        bad += 1
    if src["_inert_uniqueItems"]:
        print("::error::%d `uniqueItems: true` site(s) have NON-SCALAR items and no `maxItems`, so "
              "crdgen emits NOTHING for them and the uniqueness is NOT enforced anywhere: %s. "
              "Add maxItems (which makes it a CEL rule) or drop the uniqueItems, but do not leave "
              "a declared constraint that silently does nothing."
              % (len(src["_inert_uniqueItems"]), src["_inert_uniqueItems"]))
        bad += 1
    if len(conds) != EXPECTED_CONDITIONALS:
        print("::error::expected exactly %d if/then pair(s), found %d: %s. Each compiles to one "
              "x-kubernetes-validations CEL rule on the Installer CRD, and a CEL rule binds at "
              "ADMISSION -- an existing CR that violates a new one becomes silently UNUPDATABLE. "
              "If this addition is intended, raise EXPECTED_CONDITIONALS deliberately, name the "
              "rule in the tag's notice, and check no live CR violates it first."
              % (EXPECTED_CONDITIONALS, len(conds), conds))
        bad += 1

    if bad:
        return 1
    print("check-authn-sa-conditional: OK -- the conditional is declared, behaves on %d value(s), "
          "and the schema carries exactly %d if/then pair(s)" % (len(CASES), len(conds)))
    return 0


def self_test() -> int:
    """Mutate the schema in memory and require each mutation to be caught."""
    import copy
    try:
        import jsonschema
    except ImportError:
        print("  jsonschema missing -- cannot self-test")
        return 1
    base = json.loads(SCHEMA.read_text(encoding="utf-8"))
    bad = 0

    def behaves(schema):
        v = jsonschema.Draft202012Validator(schema)
        for value, want_ok, _ in CASES:
            errs = list(v.iter_errors({"componentValues": {"authn": {"serviceAccount": value}}}))
            if (not errs) != want_ok:
                return False
        return True

    # the real schema must pass both halves
    ok = behaves(base) and len(count_conditionals(base)) == EXPECTED_CONDITIONALS
    bad += 0 if ok else 1
    print("  %-4s the REAL schema passes" % ("ok" if ok else "FAIL"))

    # removing the conditional must be caught by the behaviour half
    m = copy.deepcopy(base)
    sa = node_at(m, PATH)
    sa.pop("if"), sa.pop("then")
    ok = not behaves(m)
    bad += 0 if ok else 1
    print("  %-4s removing the if/then is caught" % ("ok" if ok else "FAIL"))

    # dropping ONLY the minLength must be caught -- this is plumbing <v1.95.0's silent drop
    m = copy.deepcopy(base)
    node_at(m, PATH)["then"]["properties"]["name"].pop("minLength")
    ok = not behaves(m)
    bad += 0 if ok else 1
    print("  %-4s dropping only `minLength` is caught (the pre-v1.95.0 drop)" % ("ok" if ok else "FAIL"))

    # a SECOND conditional elsewhere must change the count
    m = copy.deepcopy(base)
    other = node_at(m, ("properties", "componentValues", "properties", "frontend",
                        "properties", "serviceAccount"))
    other["if"] = {"properties": {"create": {"const": False}}, "required": ["create"]}
    other["then"] = {"required": ["name"]}
    n = len(count_conditionals(m))
    ok = n == EXPECTED_CONDITIONALS + 1
    bad += 0 if ok else 1
    print("  %-4s a second if/then raises the count to %d" % ("ok" if ok else "FAIL", n))

    # and the count must NOT be satisfied by an `if` with no `then`, which emits nothing
    m = copy.deepcopy(base)
    node_at(m, ("properties", "componentValues", "properties", "frontend",
                "properties", "serviceAccount"))["if"] = {"required": ["create"]}
    ok = len(count_conditionals(m)) == EXPECTED_CONDITIONALS
    bad += 0 if ok else 1
    print("  %-4s a bare `if` with no `then` does NOT count" % ("ok" if ok else "FAIL"))

    print("self-test: 5 case(s), %d failure(s)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
