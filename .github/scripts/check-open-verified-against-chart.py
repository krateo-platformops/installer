#!/usr/bin/env python3
"""CI guard: every CHART-VERIFIED open node must still be open in the chart it is verified against.

WHY THIS EXISTS, as a companion to check-no-unknowns rather than part of it. That gate keeps
`componentValues` fully typed and permits an open node only by NAME, via K8S_NATIVE_OPEN,
K8S_NATIVE_ARRAYS or DECLARED_OPEN. A name list is the right answer for Kubernetes-native
structures -- mirroring upstream's PodSpec here would be enormous and would drift every k8s release.

It is the WRONG answer for a component's own values surface. Diego's decision (2026-11-22) is that
`additionalProperties: true` may be permitted at a node ONLY when the pinned chart's own schema is
open at that exact node, VERIFIED AGAINST THE PUBLISHED CHART RATHER THAN ASSERTED. A name list
cannot do that: it records what someone believed when they added the entry, and it keeps passing
after the chart closes the node -- at which point the installer accepts keys the chart will refuse,
and the failure lands at `helm upgrade` inside a cdc, far from the values file that caused it.

So this file does the verification the name list cannot:

    for each entry in VERIFIED_OPEN:
      resolve the component's pin (version AND repo -- the pin's `repo` key is the registry
        authority, and three registries are in use)
      pull that exact chart version's values.schema.json
      walk to the SAME relative path inside it
      require the chart to be open there, by the same definition crdgen uses:
        additionalProperties: true, OR an object with no `properties` and no
        `additionalProperties` (open by JSON Schema default -- crdgen emits
        x-kubernetes-preserve-unknown-fields for both, measured on 057's live CRD:
        88 preserve-unknown sites, 6 of them beside a `properties` map, against 976 closed nodes)

FAIL-CLOSED, in three distinct ways, because each failure mode would otherwise read as success:

  1. a chart that cannot be PULLED is a FAILURE, not a skip. "I could not check" and "it is fine"
     must not produce the same exit code.
  2. a chart that ships NO values.schema.json is a FAILURE for an entry naming it: openness cannot
     be verified against a schema that does not exist, so the entry has no basis.
  3. an entry whose path is absent from the installer schema is a FAILURE: a stale entry that
     verifies nothing is how a list silently stops covering what it claims.

WHAT IT DOES NOT DO. It does not verify K8S_NATIVE_OPEN or DECLARED_OPEN; those are deliberately
name-based and check-no-unknowns owns them. It does not check that the installer's node and the
chart's node have the same SHAPE -- only that the chart is open where the installer is open. A
chart being open does not make a key correct, and a schema match does not bound the SERVICE behind
it: a 1-character username passed a mirrored schema and Keycloak rejected it with a 400.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

SCHEMA = pathlib.Path("chart/values.schema.json")
PINS = pathlib.Path("chart/files/component-pins.yaml")
DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"

# Installer-schema paths permitted to be open BECAUSE THE PINNED CHART IS OPEN THERE. JSON-pointer
# style, exactly as check-no-unknowns prints them. Each is verified on every run; none is trusted.
#
# EMPTY ON PURPOSE TODAY. The mechanism lands before the entries, so the first entry is added with
# a working verifier rather than alongside an unproven one. observability's 29 class-B sites are the
# first intended users, and each arrives with its own verified row.
VERIFIED_OPEN: dict[str, str] = {}

MIN_PINS = 80


def load_pins():
    d = yaml.safe_load(PINS.read_text(encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("components") or d.get("pins") or [])
    return [c for c in items if isinstance(c, dict) and c.get("name")]


def component_of(path: str):
    """The componentValues component a path belongs to, or None."""
    m = re.match(r"^/properties/componentValues/properties/([^/]+)(/.*)?$", path)
    return (m.group(1), m.group(2) or "") if m else None


def node_at(schema, pointer: str):
    """Walk a JSON-pointer-ish path of /properties/<k> and /items segments."""
    cur = schema
    for seg in [s for s in pointer.split("/") if s]:
        if seg == "properties":
            continue
        if seg == "items":
            cur = cur.get("items")
        else:
            cur = (cur.get("properties") or {}).get(seg)
        if cur is None:
            return None
    return cur


def is_open(node) -> bool:
    """Open by the same definition crdgen acts on: explicit true, or object with neither."""
    if not isinstance(node, dict):
        return False
    if node.get("additionalProperties") is True:
        return True
    return (node.get("type") == "object"
            and not node.get("properties")
            and "additionalProperties" not in node)


def chart_schema(pin, workdir):
    """Pull the pinned chart and return (schema_or_None, error_or_None)."""
    base = (pin.get("repo") or DEFAULT_REPO).rstrip("/")
    ref = "%s/%s" % (base, pin.get("chart") or pin["name"])
    dest = pathlib.Path(workdir) / pin["name"]
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["helm", "pull", ref, "--version", str(pin["version"]),
                        "-d", str(dest), "--untar"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return None, "helm pull failed: %s" % (r.stderr or "").strip()[:160]
    f = next(dest.rglob("values.schema.json"), None)
    if f is None:
        return None, "the chart ships no values.schema.json, so openness cannot be verified"
    try:
        return json.loads(f.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, "its values.schema.json is not valid JSON: %s" % e
    

def main() -> int:
    for p in (SCHEMA, PINS):
        if not p.is_file():
            print("::error::%s not found -- run from the repo root" % p)
            return 1
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    pins = {c["name"]: c for c in load_pins()}
    print("  pins parsed: %d" % len(pins))
    if len(pins) < MIN_PINS:
        print("::error::parsed only %d pin(s), expected at least %d -- a gate that inspects "
              "nothing must FAIL." % (len(pins), MIN_PINS))
        return 1
    print("  VERIFIED_OPEN entries: %d" % len(VERIFIED_OPEN))
    if not VERIFIED_OPEN:
        print("check-open-verified-against-chart: OK -- no entries yet; the verifier is in place "
              "and its self-test proves it discriminates. The first entry is verified on the run "
              "that adds it.")
        return 0

    bad = 0
    with tempfile.TemporaryDirectory(prefix="verified-open-") as wd:
        cache = {}
        for path in sorted(VERIFIED_OPEN):
            mine = node_at(schema, path)
            if mine is None:
                print("::error::%s is not present in %s. A stale entry verifies nothing, which is "
                      "how a list silently stops covering what it claims." % (path, SCHEMA))
                bad += 1
                continue
            if not is_open(mine):
                print("::error::%s is NOT open in the installer schema, so it does not need an "
                      "entry here. Remove it rather than leaving a row that checks nothing." % path)
                bad += 1
                continue
            parsed = component_of(path)
            if parsed is None:
                print("::error::%s is not under componentValues/<component>, so no chart can be "
                      "resolved for it. Name-based exceptions belong in check-no-unknowns." % path)
                bad += 1
                continue
            comp, rel = parsed
            pin = pins.get(comp)
            if pin is None:
                print("::error::%s names component %r, which is not in the pins file." % (path, comp))
                bad += 1
                continue
            if comp not in cache:
                cache[comp] = chart_schema(pin, wd)
            cschema, err = cache[comp]
            if cschema is None:
                print("::error::%s: could not verify against %s %s -- %s. A chart that cannot be "
                      "read is a FAILURE, not a skip: 'I could not check' and 'it is fine' must "
                      "not share an exit code."
                      % (path, comp, pin.get("version"), err))
                bad += 1
                continue
            theirs = node_at(cschema, rel)
            if theirs is None:
                print("::error::%s: the path %r does not exist in %s %s's own schema, so the "
                      "chart is not open there -- it does not know the node at all."
                      % (path, rel, comp, pin.get("version")))
                bad += 1
                continue
            if not is_open(theirs):
                print("::error::%s: %s %s's own schema is CLOSED at %r (additionalProperties=%r, "
                      "properties=%s). The installer must not accept keys the chart will refuse: "
                      "the failure would land at helm upgrade inside a cdc, far from the values "
                      "file that caused it."
                      % (path, comp, pin.get("version"), rel,
                         theirs.get("additionalProperties"), bool(theirs.get("properties"))))
                bad += 1
                continue
            print("  ok  %-62s verified open in %s %s" % (path, comp, pin.get("version")))
    if bad:
        return 1
    print("check-open-verified-against-chart: OK -- %d entr%s verified against the pinned chart"
          % (len(VERIFIED_OPEN), "y" if len(VERIFIED_OPEN) == 1 else "ies"))
    return 0


def self_test() -> int:
    """The verifier must DISCRIMINATE. A checker that calls everything open is useless, and one
    that calls everything closed blocks every legitimate entry -- so both halves are asserted."""
    bad = 0
    OPEN = [
        ({"additionalProperties": True}, "explicit additionalProperties: true"),
        ({"type": "object"}, "bare {type: object} -- open by JSON Schema default"),
        ({"type": "object", "additionalProperties": True, "properties": {"a": {}}},
         "AP:true BESIDE properties -- open, and the case that matters most"),
    ]
    CLOSED = [
        ({"type": "object", "additionalProperties": False}, "explicitly closed"),
        ({"type": "object", "properties": {"a": {}}}, "properties with no AP -- CLOSED, not open"),
        ({"type": "object", "additionalProperties": {"type": "string"}}, "a TYPED map is not open"),
        ({"type": "string"}, "a scalar"),
        (None, "an absent node"),
        ("notadict", "a non-dict"),
    ]
    for node, label in OPEN:
        ok = is_open(node) is True
        bad += 0 if ok else 1
        print("  %-4s OPEN   %s" % ("ok" if ok else "FAIL", label))
    for node, label in CLOSED:
        ok = is_open(node) is False
        bad += 0 if ok else 1
        print("  %-4s CLOSED %s" % ("ok" if ok else "FAIL", label))

    # the JSON-Schema-default case is the subtle one: `properties` present with no
    # additionalProperties is CLOSED for crdgen, and reading it as open would permit an entry
    # for a node the chart actually bounds.
    ok = is_open({"type": "object", "properties": {}}) is True
    bad += 0 if ok else 1
    print("  %-4s an EMPTY properties map counts as open (nothing is named, so nothing is bounded)"
          % ("ok" if ok else "FAIL"))

    cases = [
        ("/properties/componentValues/properties/foo/properties/bar", ("foo", "/properties/bar")),
        ("/properties/componentValues/properties/foo", ("foo", "")),
        ("/properties/core-provider", None),
        ("/properties/componentValues", None),
        ("", None),
    ]
    for path, want in cases:
        got = component_of(path)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-4s component_of(%r) -> %r" % ("ok" if ok else "FAIL", path[:46], got))

    # node_at must follow items as well as properties, or every array entry would read as absent
    probe = {"properties": {"a": {"items": {"properties": {"b": {"type": "string"}}}}}}
    got = node_at(probe, "/properties/a/items/properties/b")
    ok = got == {"type": "string"}
    bad += 0 if ok else 1
    print("  %-4s node_at follows /items as well as /properties" % ("ok" if ok else "FAIL"))
    ok = node_at(probe, "/properties/a/properties/zz") is None
    bad += 0 if ok else 1
    print("  %-4s node_at returns None for an absent path (not a crash, not {})"
          % ("ok" if ok else "FAIL"))

    print("self-test: %d case(s), %d failure(s)"
          % (len(OPEN) + len(CLOSED) + 1 + len(cases) + 2, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
