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

COMPILED_TO_CEL = ("not",)
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
    errors, warnings = [], []
    def walk(node, path):
        if isinstance(node, dict):
            for kw in COMPILED_TO_CEL:
                if kw in node:
                    ub = unbounded_ancestors(root, path)
                    if ub:
                        errors.append((kw, path, ub))
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
    return errors, warnings


def pretty(path):
    return ".".join(p for p in path if p not in ("properties", "items")) or "<root>"


def selftest(root):
    """A gate only ever shown to pass cannot be distinguished from one that cannot fail.
    Re-introduce the exact 0.3.508 defect and require detection."""
    import copy
    probe = copy.deepcopy(root)
    node = probe["properties"]["componentValues"]["properties"].get("identity-lifecycle")
    if not node:
        print("::error::self-test needs componentValues.identity-lifecycle")
        return 1
    ir = node["properties"]["identityResources"]
    ir.pop("maxItems", None)
    ir["items"]["properties"]["resources"].pop("maxItems", None)
    ir["items"]["properties"]["resources"]["items"].pop("maxLength", None)
    errs, _ = scan(probe)
    hit = [e for e in errs if "identityResources" in pretty(e[1])]
    print("  %s self-test: stripping the 0.3.508 bounds -> %s"
          % ("ok  " if hit else "FAIL", "detected" if hit else "NOT DETECTED"))
    if not hit:
        print("::error::self-test: the gate did not detect the exact defect it exists for")
        return 1
    # and the inverse: HEAD itself must be clean, or the self-test above proves nothing
    errs2, _ = scan(root)
    print("  %s self-test: HEAD as committed -> %d error(s)"
          % ("ok  " if not errs2 else "FAIL", len(errs2)))
    return 0 if not errs2 else 1


def main() -> int:
    if not SCHEMA.is_file():
        print(f"::error::{SCHEMA} not found -- run from the repo root")
        return 1
    root = json.loads(SCHEMA.read_text())
    errors, warnings = scan(root)

    for kw, path, ub in warnings:
        print(f"  WARN  `{kw}` at {pretty(path)}")
        for p, why in ub[:2]:
            print(f"          unbounded above it: {pretty(p.split('.'))} ({why})")
    if warnings:
        print(f"  {len(warnings)} warning(s): same shape as the 0.3.508 defect, but NOT proven to "
              f"compile to a cost-estimated rule -- when 0.3.508 failed the apiserver reported "
              f"ONE structural error with these also present and unbounded.")

    for kw, path, ub in errors:
        print(f"  FAIL  `{kw}` at {pretty(path)}")
        for p, why in ub:
            print(f"          unbounded above it: {pretty(p.split('.'))} ({why})")

    if errors:
        print("::error::cel-cost-shape: a keyword crdgen compiles into CEL sits under an unbounded "
              "array or string. The apiserver's cost estimator multiplies by every unbounded "
              "collection above the rule and refuses the WHOLE generated CRD -- which is why "
              "0.3.507 and 0.3.508 cannot install on a fresh cluster. Add maxItems/maxLength, or "
              "remove the keyword. Verify with: kubectl create --dry-run=server -f <generated crd>")
        return 1

    if selftest(root):
        return 1
    print(f"\ncel-cost-shape: OK -- 0 error(s), {len(warnings)} warning(s), self-test detected the "
          f"0.3.508 defect when re-introduced.")
    print("WHAT THIS DOES NOT COVER: it reads the INPUT schema, not crdgen's output, and cannot "
          "compute the apiserver's cost estimate -- only a server dry-run of the generated CRD can.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
