#!/usr/bin/env python3
"""CI guard: a JSON Schema keyword that crdgen must COMPILE INTO CEL, sitting under an unbounded
array or string, is refused -- because the apiserver's cost estimator multiplies by every unbounded
collection above the rule and refuses the WHOLE generated CRD.

WHY THIS EXISTS, measured rather than imagined. installer 0.3.507 and 0.3.508 CANNOT INSTALL on a
fresh cluster. snowplow measured 0.3.508 on 2 of 2 fresh GKE 1.36 clusters, 2026-11-05 11:41Z:
`helm install` ends with `job installer-self-await-crd failed: DeadlineExceeded`, because the
generated Installer CRD is refused:

    componentValues.identity-lifecycle.identityResources.items.resources.items
      .x-kubernetes-validations[0].rule: Forbidden: estimated rule cost exceeds
      budget by factor of 1.468006x

The schema contained ZERO hand-written CEL. What it had was a JSON Schema `not`:

    {"type": "string", "minLength": 1, "not": {"enum": ["*", "secrets"]}}

CRD structural schemas have no `not`, so crdgen compiles it to CEL. `identityResources` had no
maxItems, `resources` had no maxItems, the string had no maxLength -- so the estimator assumed
worst case. Two clusters were spent discovering a property of the schema that is checkable here.

THE BLAST RADIUS IS WHY THIS IS A GATE AND NOT A FIX. componentValues mirrors are DERIVED from each
component chart's schema, so a `not` that ANY component author writes under an unbounded array
propagates into the Installer CRD automatically and breaks every fresh install. core-provider's
plumbing#81 emits `not: {enum}` natively (no CEL, no budget) and closes the class properly; this
gate is the installer-side guard that does not depend on which core-provider is pinned.

WHAT IS AN ERROR vs A WARNING, and the split is evidence-based rather than cautious.
  ERROR  -- `not` (and a `const` nested inside one). This is the keyword PROVEN to compile to a
            cost-estimated CEL rule, by two independent reproductions of factor 1.468006x: a server
            dry-run reconstruction on krateo-057, and core-provider's own differential over 1125
            schemas which lists identity-lifecycle among 12 failures under BOTH plumbing v1.21.0
            and v1.25.0.
  WARN   -- anyOf / oneOf / allOf / patternProperties / propertyNames / uniqueItems / if-then-else
            / dependencies / multipleOf. Same SHAPE, but NOT proven costly: when 0.3.508 failed,
            the apiserver reported `1 structural error(s)` while FOUR of these were also present
            and unbounded (two anyOf under krateo-observability's mongodb statefulSet, two
            uniqueItems under access-review's rbac). Exactly one error with five candidates present
            is evidence that only `not` was over budget. Erroring on them would fail a chart that
            demonstrably installs, and a gate that fires on working code is a gate someone
            switches off.

WHAT A GREEN RUN DOES NOT COVER: it reasons about the INPUT schema, not crdgen's output. It cannot
compute the apiserver's cost estimate (that is server-side -- `kubectl create --dry-run=server`
answers it) and it does not know which keywords a future crdgen will compile. It checks the shape
that is known to break, not every shape that could.
"""
import json
import sys
from pathlib import Path

SCHEMA = Path("chart/values.schema.json")

# core-provider ANSWERED THIS FROM CODE on 2026-11-08 — plumbing crdgen v1.32.0,
# transpile_keywords.go emitNot, since feffae6 / plumbing v1.26.1, which core-provider 2.13.76
# includes. It replaces the premise this gate was built on:
#
#   * `not` is NEVER compiled to CEL.
#   * `not: {enum}` and `not: {const}` are emitted NATIVELY — no CEL, no cost budget. That is why
#     the live Installer CRD at v0-3-542 carries 2 structural `not` and ZERO CEL rules, which is
#     what I measured before asking.
#   * ANY OTHER scalar `not`, `not: {pattern}` included, is DROPPED, with the warning
#     "not (non-trivial) unsupported -> dropped", and surfaced on the definition's SchemaEnforced
#     condition.
#   * The 0.3.508 refusal was the OLD translation of a forbidden-values `not` into
#     `!(self in [...])` over two unbounded arrays. feffae6 removed that translation.
#
# So THIS GATE WAS TESTING FOR A HAZARD THAT NO LONGER EXISTS, and refusing safe changes: it
# blocked #748's `not: {pattern}` sampling guard for an unbounded cost that crdgen never incurs.
# The real hazard is the opposite one — a `not` crdgen silently DROPS, so a constraint the author
# believes they wrote is absent from the CRD and enforced only when helm validates values.
#
# NOT AN ERROR, deliberately. A dropped `not` is still enforced where the installer actually
# validates: `helm template`/`helm install` reads values.schema.json. For the installer that is its
# own render; for a component it is cdc's helm install, so a bad value fails the RECONCILE instead
# of being refused by kubectl. Later, but still a refusal. The warning exists so nobody believes a
# dropped constraint is an admission-time one.
NOT_EMITTED_NATIVELY = ("enum", "const")
SAME_SHAPE_UNPROVEN = ("oneOf", "anyOf", "allOf", "dependencies", "dependentSchemas",
                       "dependentRequired", "patternProperties", "propertyNames",
                       "if", "then", "else", "uniqueItems", "multipleOf")
def unbounded_ancestors(root, path):
    """Every array without maxItems, or string without maxLength, at or above `path`."""
    node, cur, out = root, [], []
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return out
        cur.append(part)
        if node.get("type") == "array" and "maxItems" not in node:
            out.append((".".join(cur), "array without maxItems"))
        if node.get("type") == "string" and "maxLength" not in node:
            out.append((".".join(cur), "string without maxLength"))
    return out


def scan(root):
    errors, warnings, dropped = [], [], []
    def walk(node, path):
        if isinstance(node, dict):
            # `not` is never compiled to CEL (core-provider, from crdgen's emitNot). What matters
            # is whether crdgen EMITS it or DROPS it: enum/const go native, everything else is
            # dropped and the constraint is absent from the CRD.
            if "not" in node and isinstance(node["not"], dict):
                body = set(node["not"].keys())
                if not (body & set(NOT_EMITTED_NATIVELY)):
                    dropped.append((sorted(body) or ["<empty>"], path))
            for kw in SAME_SHAPE_UNPROVEN:
                if kw in node:
                    ub = unbounded_ancestors(root, path)
                    if ub:
                        warnings.append((kw, path, ub))
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [str(i)])
    walk(root, [])
    return errors, warnings, dropped


def pretty(path):
    return ".".join(p for p in path if p not in ("properties", "items")) or "<root>"


def selftest(root):
    """A gate only ever shown to pass cannot be distinguished from one that cannot fail.
    BOTH arms, because this rule has two: a droppable `not` must be detected, and a natively
    emitted one must NOT be."""
    import copy

    # ARM 1 -- a `not: {pattern}` must be reported as DROPPED.
    probe = copy.deepcopy(root)
    probe["properties"]["__selftest"] = {"type": "string", "not": {"pattern": "^x$"}}
    _, _, dropped = scan(probe)
    hit = [d for d in dropped if "__selftest" in pretty(d[1])]
    print("  %s self-test: a `not: {pattern}` -> %s"
          % ("ok  " if hit else "FAIL", "detected as dropped" if hit else "NOT DETECTED"))
    if not hit:
        print("::error::self-test: the gate did not detect a droppable `not`, so it cannot fail")
        return 1

    # ARM 2 -- a `not: {enum}` is emitted natively and must NOT be reported.
    probe2 = copy.deepcopy(root)
    probe2["properties"]["__selftest2"] = {"type": "string", "not": {"enum": ["*"]}}
    _, _, dropped2 = scan(probe2)
    false_pos = [d for d in dropped2 if "__selftest2" in pretty(d[1])]
    print("  %s self-test: a `not: {enum}` -> %s"
          % ("ok  " if not false_pos else "FAIL",
             "correctly NOT reported (crdgen emits it natively)" if not false_pos
             else "WRONGLY reported"))
    if false_pos:
        print("::error::self-test: the gate flagged a natively emitted `not`, so it over-reports")
        return 1
    return 0


def main() -> int:
    if not SCHEMA.is_file():
        print(f"::error::{SCHEMA} not found -- run from the repo root")
        return 1
    root = json.loads(SCHEMA.read_text())
    errors, warnings, dropped = scan(root)

    for body, path in dropped:
        print(f"  DROP  `not: {{{', '.join(body)}}}` at {pretty(path)}")
    if dropped:
        print(f"  {len(dropped)} `not` keyword(s) crdgen DROPS. core-provider's crdgen emits only "
              f"`not: {{enum}}` and `not: {{const}}` natively; anything else is dropped with "
              f"\"not (non-trivial) unsupported -> dropped\" and surfaced on the definition's "
              f"SchemaEnforced condition.")
        print("        NOT A FAULT, but know what it means: the constraint is ABSENT from the "
              "generated CRD, so kubectl will not refuse a bad value. It is enforced when helm "
              "validates values.schema.json -- this chart's own render for the installer, and "
              "cdc's helm install for a component, where a bad value fails the RECONCILE instead. "
              "Later, but still a refusal. Do not describe it as admission-time validation.")

    for kw, path, ub in warnings:
        print(f"  WARN  `{kw}` at {pretty(path)}")
        for p, why in ub[:2]:
            print(f"          unbounded above it: {pretty(p.split('.'))} ({why})")
    if warnings:
        print(f"  {len(warnings)} warning(s): these keywords MAY compile to CEL, where the "
              f"apiserver's cost estimator multiplies by every unbounded collection above the rule. "
              f"Unlike `not`, nobody has asked core-provider how crdgen treats them -- so this is an "
              f"open question, not a known defect.")

    for kw, path, ub in errors:
        print(f"  FAIL  `{kw}` at {pretty(path)}")
        for p, why in ub:
            print(f"          unbounded above it: {pretty(p.split('.'))} ({why})")
    if errors:
        print("::error::cel-cost-shape: a keyword proven to compile into CEL sits under an "
              "unbounded array or string. Add maxItems/maxLength, or remove the keyword.")
        return 1

    if selftest(root):
        return 1
    print(f"\ncel-cost-shape: OK -- 0 error(s), {len(dropped)} dropped `not`, "
          f"{len(warnings)} warning(s); self-test proved BOTH arms.")
    print("WHAT THIS DOES NOT COVER: it reads the INPUT schema, not crdgen's output. The "
          "authority on what is emitted is crdgen's emitNot (plumbing v1.32.0); the authority on "
          "cost is a server-side dry-run of the generated CRD.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
