#!/usr/bin/env python3
"""CI guard (installer#145): every distinct `feature:` value a component pin gates on in
component-pins.yaml must exist as a property under `features.properties` in values.schema.json.

Each pin's `feature:` names the spec.features.<key> flag that turns that component on. self-bootstrap
resolves the pin list against the CR's spec.features to decide which Compositions to emit, and that
features object is the crdgen-compiled `features.properties` block of the CRD (additionalProperties
governs whether an unknown key is even settable). A pin that gates on a feature the schema never
declares can therefore NEVER be enabled: the flag is not a real CR field, so the component silently
stays off on every install with no error anywhere — the chart still renders, lints and publishes.

This is the features-object analogue of check-pin-fields.py (which guards the components[] schema).
It only asserts pins ⊆ schema features; the schema may declare extra features no pin gates on yet
(e.g. coreProvider), which is fine.

We parse the pins with yaml.safe_load and read the `feature:` key off each mapping so prose in
comments (e.g. "NOT a dedicated feature: a new installer feature ...") can never leak a phantom key.
"""
import sys, json, yaml

schema = json.load(open("chart/values.schema.json"))
features = schema["properties"]["features"]
declared = set(features.get("properties", {}))

pins = yaml.safe_load(open("chart/files/component-pins.yaml"))["components"]

used = {}  # feature -> first pin name that uses it
for c in pins:
    if not isinstance(c, dict):
        continue
    f = c.get("feature")
    if f is not None:
        used.setdefault(f, c.get("name"))

orphans = sorted((f, used[f]) for f in used if f not in declared)
if orphans:
    print("::error::pin feature(s) missing from values.schema.json features.properties — "
          "the flag is not a real CR field, so the component can never be enabled (installer#145):")
    for f, name in orphans:
        print(f"  feature: {f}  (first used by {name})")
    print(f"declared features: {sorted(declared)}")
    sys.exit(1)
print(f"feature-keys: OK — all {len(used)} distinct pin features "
      f"({', '.join(sorted(used))}) are declared in values.schema.json")
