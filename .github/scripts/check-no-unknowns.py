#!/usr/bin/env python3
"""CI guard: chart/values.schema.json must never reintroduce a free-form / unknown-value escape.

The installer's values.schema.json is fed verbatim to core-provider's crdgen, which turns it into
the Installer CRD's OpenAPI v3 structural schema. Two constructs let arbitrary, untyped values through
and are FORBIDDEN here:

  * `additionalProperties: true`            — an open object accepting any key with any value. crdgen
                                              rewrites it to x-kubernetes-preserve-unknown-fields, so it
                                              is exactly as unbounded on the live CRD.
  * `x-kubernetes-preserve-unknown-fields`  — the k8s structural-schema escape hatch (any subtree).
  * AN IMPLICITLY-OPEN OBJECT              — `type: object` with NEITHER `properties` NOR
                                              `additionalProperties`. JSON Schema treats a missing
                                              `additionalProperties` as `true`, so this is exactly as
                                              unbounded as the first case, and crdgen emits
                                              x-kubernetes-preserve-unknown-fields for it. VERIFIED on
                                              krateo-057: the live Installer CRD's `core-provider` field
                                              reads type=object, properties=0,
                                              x-kubernetes-preserve-unknown-fields=True. The guard
                                              greps the two LITERAL forms and missed this shape
                                              entirely, so the docstring's old claim that "no free-form
                                              escapes remain anywhere" was false for 62 subtrees.

`componentValues` is a HAND-CURATED, deliberately fully-typed override surface (see its description):
every object is `additionalProperties: false` with enumerated, typed properties; open string maps use
`additionalProperties: {type: string}` (a typed value, allowed). Reintroducing any forbidden
construct silently reopens the surface — this guard fails the PR instead.

WHY THE IMPLICITLY-OPEN ONES ARE ALLOWLISTED RATHER THAN TYPED. Most are Kubernetes-native
structures — podSecurityContext, affinity, tolerations, probes, volumes, annotation maps. Mirroring
upstream's PodSpec surface here would be enormous and would DRIFT from the real API every k8s
release, which is a worse failure than an unbounded subtree an operator opted into. So the honest
position is: these holes are DECLARED, with a reason, and a NEW one fails the build. That converts
62 silent escapes into 62 reviewed ones, and it makes the docstring true. To expose a new override knob,
add it explicitly and typed; do not reach for a wildcard.

Structural (JSON-walk) check, not a text grep: prose in `description`/`title` that merely names these
constructs is ignored — only real schema keywords are inspected.
"""
import sys, json

SCHEMA = "chart/values.schema.json"

# Kubernetes-native structures whose full surface belongs to upstream, not to this schema. Matched
# on the LEAF NAME, so a new component reusing the same k8s structure is covered without an edit.
K8S_NATIVE_OPEN = {
    "affinity", "podSecurityContext", "securityContext", "containerSecurityContext",
    "livenessProbe", "readinessProbe", "startupProbe", "nodeSelector",
    "podAnnotations", "podLabels", "annotations", "labels",
}
# Open objects that are NOT k8s-native and are deliberate. Each needs a reason, because an entry
# here is a reviewed decision to accept an unbounded subtree on the live CRD.
DECLARED_OPEN = {
    "/properties/core-provider":
        "bootstrap subchart passthrough — core-provider's own values surface, not ours to enumerate",
    "/properties/core-provider-crds":
        "bootstrap subchart passthrough — the CRD chart's own values surface",
    "/properties/componentValues/properties/agentgateway-policies/properties/costCatalog/properties/overrides":
        "per-model price overrides; the KEYS are model names, so there is no fixed set to enumerate",
    "/properties/componentValues/properties/agentgateway-policies/properties/gateway/properties/parameters/properties/deployment":
        "agentgateway's own Deployment override block, owned by that chart",
    "/properties/componentValues/properties/agentgateway-policies/properties/gateway/properties/parameters/properties/service":
        "agentgateway's own Service override block, owned by that chart",
}
# An open object under one of these ARRAY paths is a k8s-native list element (volumes, tolerations,
# imagePullSecrets, env, listeners, tls): same reasoning as K8S_NATIVE_OPEN.
K8S_NATIVE_ARRAYS = {
    "volumes", "volumeMounts", "tolerations", "imagePullSecrets", "env", "listeners", "tls",
    "extraRequestGuards", "extraResponseGuards", "forwardHeaderMatches", "assumeRole", "resources",
}

violations = []  # (json-path, message)
undeclared_open = []

def walk(node, path):
    if isinstance(node, dict):
        # An object with neither `properties` nor `additionalProperties` is open by JSON Schema
        # default. Detect it, then decide: k8s-native or explicitly declared is fine, anything
        # else is a new hole and fails.
        if (node.get("type") == "object"
                and not node.get("properties")
                and "additionalProperties" not in node):
            # Paths are JSON-pointer style: /properties/componentValues/properties/foo/items.
            # So the identifying name is the last NON-structural segment — skipping `properties`
            # and `items`, which are keywords rather than field names. Getting this wrong is why
            # the first version of this check matched nothing: it split on "." against a
            # slash-separated path, so every allowlist entry silently missed.
            segs = [s for s in path.split("/") if s and s not in ("properties", "items")]
            leaf = segs[-1] if segs else ""
            if not (leaf in K8S_NATIVE_OPEN or leaf in K8S_NATIVE_ARRAYS
                    or path in DECLARED_OPEN):
                undeclared_open.append(path or "<root>")
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
    if undeclared_open:
        print("::error::check-no-unknowns: object(s) open by JSON Schema DEFAULT — `type: object` with "
              "neither `properties` nor `additionalProperties`. crdgen emits "
              "x-kubernetes-preserve-unknown-fields for these, so they are as unbounded on the live "
              "CRD as `additionalProperties: true`:")
        for p in undeclared_open:
            print(f"  {p}")
        print("Either type the subtree, or — if it is a Kubernetes-native structure or a deliberate "
              "passthrough — add it to K8S_NATIVE_OPEN / K8S_NATIVE_ARRAYS / DECLARED_OPEN with a "
              "reason. An entry there is a reviewed decision; silence is not.")
        return 1
    print(f"check-no-unknowns: OK — {SCHEMA} has no additionalProperties:true, no "
          f"preserve-unknown-fields, and no UNDECLARED implicitly-open object "
          f"({len(K8S_NATIVE_OPEN)} k8s-native names + {len(K8S_NATIVE_ARRAYS)} array names + "
          f"{len(DECLARED_OPEN)} declared exceptions are allowed by name)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
