#!/usr/bin/env python3
"""CI guard: every field used in component-pins.yaml must exist in values.schema.json's
`properties.components.items.properties`.

That schema is `additionalProperties: false` and feeds core-provider's crdgen verbatim, becoming the
Installer CRD's structural schema. self-bootstrap.yaml applies the FULLY RENDERED pin list as
spec.components[] on that CRD, so a pin field the schema does not declare is rejected by the
apiserver's strict decoding:

    unknown field "spec.components[5].internalUrls"

and installer-self-instance CrashLoopBackOffs on a clean install. The chart still renders, lints and
publishes cleanly, so nothing catches it before someone installs the release (internalUrls shipped
unschema'd in 0.3.59 and stayed broken through 0.3.70).
"""
import sys, json, yaml

schema = json.load(open("chart/values.schema.json"))
item = schema["properties"]["components"]["items"]
declared = set(item["properties"])
pins = yaml.safe_load(open("chart/files/component-pins.yaml"))["components"]

if item.get("additionalProperties") is not False:
    print("::error::components[].items is no longer additionalProperties:false — this guard assumes it")
    sys.exit(1)

bad = [(i, c.get("name"), k) for i, c in enumerate(pins) for k in c if k not in declared]
if bad:
    print("::error::pin field(s) missing from values.schema.json components[].items.properties — "
          "the apiserver will reject the self-instance CR with 'unknown field':")
    for i, name, k in bad:
        print(f"  spec.components[{i}].{k}  ({name})")
    sys.exit(1)
print(f"pin-fields: OK — all {len(pins)} pins use only schema-declared fields")
