#!/usr/bin/env python3
"""CI guard: chart/values.schema.json must never reintroduce a free-form / unknown-value escape.

The installer's values.schema.json is fed verbatim to core-provider's crdgen, which turns it into
the Installer CRD's OpenAPI v3 structural schema. Two constructs let arbitrary, untyped values through
and are FORBIDDEN here:

  * `additionalProperties: true`            — an open object accepting any key with any value. crdgen
                                              rewrites it to x-kubernetes-preserve-unknown-fields, so it
                                              is exactly as unbounded on the live CRD.
  * `x-kubernetes-preserve-unknown-fields`  — the k8s structural-schema escape hatch (any subtree).

`componentValues` is a HAND-CURATED, deliberately fully-typed override surface (see its description):
every object is `additionalProperties: false` with enumerated, typed properties; open string maps use
`additionalProperties: {type: string}` (a typed value, allowed). Reintroducing either forbidden
construct silently reopens the surface — this guard fails the PR instead. To expose a new override knob,
add it explicitly and typed; do not reach for a wildcard.

Structural (JSON-walk) check, not a text grep: prose in `description`/`title` that merely names these
constructs is ignored — only real schema keywords are inspected.
"""
import sys, json

SCHEMA = "chart/values.schema.json"

violations = []  # (json-path, message)

def walk(node, path):
    if isinstance(node, dict):
        if node.get("additionalProperties") is True:
            violations.append((path or "<root>", "additionalProperties: true (use additionalProperties: false + typed properties, or a typed map)"))
        for k, v in node.items():
            if k == "x-kubernetes-preserve-unknown-fields" and v is True:
                violations.append((path or "<root>", "x-kubernetes-preserve-unknown-fields: true (type the subtree explicitly)"))
            # `additionalProperties` is a keyword whose value is a schema (or bool) — recurse into it
            # under a readable path; every other key recurses normally.
            walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]")

def main():
    try:
        schema = json.load(open(SCHEMA))
    except FileNotFoundError:
        print(f"::error::{SCHEMA} not found (run from the installer repo root)")
        return 1
    except json.JSONDecodeError as e:
        print(f"::error::{SCHEMA} is not valid JSON: {e}")
        return 1

    walk(schema, "")

    if violations:
        print(f"::error::{SCHEMA} reintroduced {len(violations)} unknown-value escape(s) — the schema must stay fully typed:")
        for path, msg in violations:
            print(f"  {path}: {msg}")
        return 1
    print(f"check-no-unknowns: OK — {SCHEMA} is fully typed (no additionalProperties:true, no preserve-unknown-fields)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
