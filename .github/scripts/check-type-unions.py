#!/usr/bin/env python3
"""CI guard: no multi-member non-null JSON Schema `type` array in chart/values.schema.json.

WHY, measured on a live cluster rather than argued. plumbing/crdgen's `primaryType`
(crdgen/transpile.go:605) keeps the FIRST non-null member of a `type` array and discards the rest
with no diagnostic -- plumbing#77. `["T","null"]` is handled correctly: it sets `nullable: true`.
Any OTHER multi-member union silently loses members, and the Installer CRD is generated from this
file by that transpiler, so a union here becomes a narrower CRD than the schema promises and the
apiserver REJECTS values this chart's own schema accepts.

It had happened. On krateo-057, at tag 0.3.506:

    schema:    OASGEN_PROVIDER_MAX_RECONCILE_RATE  type: ["integer","string"]
    live CRD:  OASGEN_PROVIDER_MAX_RECONCILE_RATE  type: integer, no x-kubernetes-int-or-string

so `componentValues.oasgen-provider.env.OASGEN_PROVIDER_MAX_RECONCILE_RATE: "10"` -- a string,
which is how env values are usually written in a values file -- is refused by the apiserver.
The two controls in the same measurement, both of which came back CLEAN and are the reason this
gate forbids only the multi-member case:

    platformRoutes   ["array","null"]  -> type: array, nullable: true   both arms kept
    frontend.config  ["string","null"] -> x-kubernetes-preserve-unknown-fields: true

THE FIX THIS GATE DIRECTS YOU TO is the idiom already used 68 times in this schema and PROVEN to
survive the transpiler (68 of them are present in the live CRD with the marker intact):

    "anyOf": [{"type": "integer"}, {"type": "string"}], "x-kubernetes-int-or-string": true

WHAT A GREEN RUN DOES NOT COVER. It reads this file only. It does not run crdgen, does not compare
against a generated CRD, and so cannot see any OTHER shape the transpiler narrows. One such shape
was measured beside the above and is NOT a union: `krateo-autopilot.mcpServers` is a map of
objects, and crdgen emits it as an open object with x-kubernetes-preserve-unknown-fields, which
drops the two int-or-string nodes under `mcpServers.*.resources.{limits,requests}.cpu` along with
all other validation underneath. That is a LOOSENING, not a rejection, so it is not this gate's
subject -- but it means "no unions" is not the same claim as "the CRD matches the schema".
"""
import json
import sys
from pathlib import Path

SCHEMA = Path("chart/values.schema.json")


def unions(node, path, out):
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, list):
            members = [x for x in t if x != "null"]
            if len(members) > 1:
                out.append((path, t))
        for k, v in node.items():
            unions(v, f"{path}.{k}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            unions(v, f"{path}[{i}]", out)
    return out


def main() -> int:
    if not SCHEMA.is_file():
        print(f"::error::{SCHEMA} not found — run from the repo root")
        return 1
    doc = json.loads(SCHEMA.read_text())

    # Self-check FIRST: a gate that cannot fail proves nothing about the file it passed.
    probe = {"properties": {"x": {"type": ["integer", "string"]}}}
    if len(unions(probe, "", [])) != 1:
        print("::error::self-check FAILED — this gate did not flag a planted "
              'type: ["integer","string"]. It cannot vouch for the schema.')
        return 1
    for benign in ([ "string", "null"], ["array", "null"], "string"):
        if unions({"properties": {"x": {"type": benign}}}, "", []):
            print(f"::error::self-check FAILED — this gate flagged {benign!r}, which crdgen "
                  "handles correctly. A gate with false positives gets switched off.")
            return 1
    print("  PASS  self-check: a planted multi-member union is flagged; "
          '["T","null"] and a plain string are not')

    found = unions(doc, "", [])
    if found:
        print("::error::chart/values.schema.json declares a multi-member JSON Schema type union. "
              "plumbing/crdgen keeps only the FIRST member, with no diagnostic, so the generated "
              "Installer CRD is NARROWER than this schema and the apiserver rejects values the "
              "chart accepts (plumbing#77, measured live on 057 at 0.3.506):")
        for path, t in found:
            kept = next((x for x in t if x != "null"), "?")
            print(f"  {path}\n      declares {t} -> the CRD will say type: {kept!r}, "
                  f"and every other member is refused")
        print('  Use: "anyOf": [{"type": "integer"}, {"type": "string"}], '
              '"x-kubernetes-int-or-string": true  — 68 nodes in this schema already do, and all '
              "68 are present in the live CRD with the marker intact.")
        return 1

    ints = json.dumps(doc).count('"x-kubernetes-int-or-string": true')
    print(f"  PASS  no multi-member type union; {ints} node(s) use the int-or-string idiom instead")
    print("\ntype-unions gate: OK")
    print("NOT asserted: that the generated CRD matches this schema. This gate reads the schema "
          "only — it never runs crdgen, so any OTHER narrowing the transpiler performs is "
          "invisible to it (one such, mcpServers becoming an open object, is in the docstring).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
