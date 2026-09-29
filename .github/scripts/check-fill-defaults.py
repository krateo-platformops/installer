#!/usr/bin/env python3
"""CI guard: no componentValues leaf may carry a schema `default` if compositions.yaml also writes
that same path with inst.fillPath.

inst.fillPath is fill-if-ABSENT: it writes only when the key is missing, so an override in
componentValues wins. That is the intended contract. But core-provider applies THIS schema's defaults
into the Installer CR spec before the platform render reads them, so a `default` on such a leaf is not
documentation — it pre-occupies the key with a value nobody typed, and the fill silently no-ops. The
declared behaviour and the shipped behaviour then disagree with nothing logged anywhere.

This shipped on componentValues.frontend.agentgateway.enabled (`default: false`). compositions.yaml
fills it with true when features.agentGateway is on, the default blocked it, and the frontend chart
appends /api/a2a/<ns>/autopilot ONLY when it is true — so every install with the gateway on served the
bare gateway origin as AUTOPILOT_API_BASE_URL, the Autopilot rail POSTed to / and every turn failed
with 405, for every user. The chart rendered, linted and published throughout. It survived from at
least 0.3.280 because an existing Composition CR kept a stale `true` from before the default existed;
only a pin bump, which re-renders that CR, exposed it.

inst.forcePath is NOT affected and is not flagged: it overwrites unconditionally, so a default beneath
it is inert (componentValues.agentgateway-policies.cors.enabled is deliberately exactly that, and its
schema defaults for allowOrigins/allowMethods are load-bearing).

Companion to check-feature-keys.py / check-pin-fields.py: same class of defect, where a schema that
reads as a type declaration is in fact a live value.
"""
import sys, re, json, collections

schema = json.load(open("chart/values.schema.json"))
tmpl = open("chart/templates/compositions.yaml").read()

# Paths written fill-if-absent. Matched per-call so a forcePath call cannot be mistaken for a fill:
# the helper name and its "path" (list ...) argument are read from the SAME include expression.
fill_paths = set()
for m in re.finditer(r'include\s+"inst\.(fillPath|forcePath)"[^\n]*?"path"\s+\(list\s+((?:"[^"]+"\s*)+)\)', tmpl):
    if m.group(1) != "fillPath":
        continue
    fill_paths.add(tuple(re.findall(r'"([^"]+)"', m.group(2))))

if not fill_paths:                      # the regex going stale must fail loudly, not pass vacuously
    sys.exit("check-fill-defaults: found no inst.fillPath calls in compositions.yaml — the matcher is "
             "broken, or the helper was renamed. Refusing to pass without checking anything.")

defaults = {}
def walk(node, path):
    if not isinstance(node, dict):
        return
    if "default" in node and node.get("type") in ("boolean", "string", "integer", "number"):
        defaults[tuple(path)] = node["default"]
    for key, sub in (node.get("properties") or {}).items():
        walk(sub, path + [key])

for comp, sub in schema["properties"]["componentValues"]["properties"].items():
    walk(sub, [comp])

bad = []
for dpath, dval in sorted(defaults.items()):
    if tuple(dpath[1:]) in fill_paths:
        bad.append((dpath[0], ".".join(dpath[1:]), dval))

if bad:
    print("componentValues leaves carrying a schema default that inst.fillPath also writes:\n")
    for comp, leaf, dval in bad:
        print(f"  componentValues.{comp}.{leaf}  default={dval!r}")
    sys.exit(
        "\nEach of these silently disables its own fill-if-absent write: core-provider applies the "
        "default into the Installer CR spec, so the key is never absent and inst.fillPath never runs. "
        "Remove the `default` and state the intent in `description` instead — the fill is what sets "
        "the value. If the write is meant to be unconditional, use inst.forcePath, which a default "
        "cannot block."
    )

print(f"check-fill-defaults: OK — {len(fill_paths)} fill-if-absent paths, "
      f"{len(defaults)} defaulted componentValues leaves, no collisions")
