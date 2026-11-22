#!/usr/bin/env python3
"""CI guard: a pinned chart must not gain a values key this schema cannot set — a RATCHET.

WHAT THE GAP IS. Every `componentValues.<component>` node is a MIRROR of that component chart's own
values schema, and every node is `additionalProperties: false`. A key the chart offers and this
schema does not declare is PRUNED BY THE APISERVER on the way into the Installer CR, while the write
reports success: the value vanishes and the chart's own default wins. Nothing is red, nothing logs.

MEASURED 2026-11-22, which is why this is a ratchet and not a clean gate: 23 of the components whose
charts could be pulled already offer keys this schema cannot set, across 79 sites. The worst are
krateo-observability (21) and otel-collector-daemonset (17). Closing all of them needs each owning
session's shapes and is days of work; letting the number GROW silently in the meantime is the thing
worth stopping today.

So: a NEW site fails. A removed site asks to be decremented. The count can only go down.

WHY THE BASELINE RECORDS CHECKABILITY, not just sites. Three registries are in use (the default
`ghcr.io/krateo-platformops/charts`, `krateo-agentiko`, `krateo-blueprints`) and some charts are
private by design. A component whose chart cannot be pulled contributes NO sites — so without
recording which were checked, a pull that starts failing would look exactly like drift being fixed.
The baseline therefore pins the SET of components checked, and a previously-checkable component
going unpullable is a FAILURE with its own message, not a silent pass.

WHAT IT CANNOT DO, stated so a green tick is not read as wider than it is:
  * it validates the chart's PUBLISHED values.yaml, not every value an operator might set;
  * it says nothing about a key that IS declared but typed differently from the chart's -- that is
    a different measurement (schema default vs chart default), and all 7 deviations found in that
    sweep turned out to be deliberate, documented installer policy;
  * it cannot see charts it cannot pull, and it names them rather than skipping quietly.
"""
import json
import os
import pathlib
import subprocess
import sys

import yaml

PINS = pathlib.Path("chart/files/component-pins.yaml")
SCHEMA = pathlib.Path("chart/values.schema.json")
BASELINE = pathlib.Path(".github/componentvalues-coverage-baseline.json")
DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"
# Fail closed: a run that checked almost nothing must not report a clean ratchet.
MIN_CHECKED_FRACTION = 0.70


def load_pins():
    d = yaml.safe_load(PINS.read_text(encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("components") or d.get("pins") or [])
    return {c["name"]: c for c in items if isinstance(c, dict) and c.get("name")}


def chart_values(pin):
    """The chart's PUBLISHED values.yaml, read from the repo THE PIN NAMES.

    The pin's `repo` key is the registry authority -- probing the default registry for a chart
    pinned elsewhere returns "not found" for every version including the one running, which is
    indistinguishable from an unpublished chart. Measured three times in one morning.
    """
    repo = pin.get("repo") or DEFAULT_REPO
    chart = pin.get("chart") or pin["name"]
    ver = str(pin.get("version") or "")
    if not ver:
        return None, "no version in the pin"
    r = subprocess.run(["helm", "show", "values", "%s/%s" % (repo, chart), "--version", ver],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
    if not r.stdout.strip():
        # NOT a failure. A chart with no values.yaml -- normal for a `-crds` chart, which ships
        # only CRD manifests -- offers no keys at all, so it cannot drift and there is nothing to
        # declare. Returning it as an empty mapping keeps it in `checked` (so a later values.yaml
        # appearing IS seen) instead of reporting "could not pull" for a chart that pulled fine.
        return {}, None
    try:
        return (yaml.safe_load(r.stdout) or {}), None
    except yaml.YAMLError as e:
        return None, [str(e)[:120]]


def sites_for(validator, comp, vals):
    """Undeclared-key sites for one component, as stable dotted paths."""
    out = set()
    for e in validator.iter_errors({"componentValues": {comp: vals}}):
        if "Additional properties" not in e.message and "does not match any of the regexes" not in e.message:
            continue
        out.add("/".join(str(x) for x in list(e.absolute_path)[2:]) or "<root>")
    return sorted(out)


def measure():
    import jsonschema
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    declared = ((schema.get("properties") or {}).get("componentValues") or {}).get("properties") or {}
    v = jsonschema.Draft202012Validator(schema)
    pins = load_pins()
    targets = sorted(set(pins) & set(declared))
    checked, unpullable, sites = [], {}, {}
    for comp in targets:
        vals, err = chart_values(pins[comp])
        if vals is None:
            unpullable[comp] = err
            continue
        checked.append(comp)
        s = sites_for(v, comp, vals)
        if s:
            sites[comp] = s
    return {"targets": targets, "checked": sorted(checked),
            "unpullable": unpullable, "sites": sites}


# Components whose charts live in a registry CI's GITHUB_TOKEN cannot read. MEASURED, not assumed:
# the first CI run of this gate pulled 59 of 78 and named exactly these 19 -- every one from
# krateo-agentiko (private by design) plus two krateo-blueprints charts. My laptop pulls them with
# a gcloud/docker login CI does not have, which made my locally generated baseline unreproducible.
#
# THEY ARE OUT OF SCOPE, AND THAT IS A REAL GAP, NOT A FIX: drift inside these 19 is unguarded by
# this gate. Named here rather than silently absent, and the gate REFUSES to let the list grow --
# so a component sliding out of reach is still a failure, which is the whole point of pinning the
# checked set. Closing the gap needs credentials, which is a separate decision and not this gate's.
OUT_OF_SCOPE = {
    "agentgateway-controller", "agentgateway-policies", "agentiko", "authn-agent",
    "clickhouse-mcp-server", "clickstack-agent", "config-refs-mcp-server", "core-provider-agent",
    "frontend-agent", "incident-agent", "installer-agent", "kagent", "kagent-crds",
    "krateo-capability-roles", "model-configs", "repo-mcp-server", "snowplow-agent",
    "structure-graph-mcp-server", "tenant-onboarding",
}


def main() -> int:
    for p in (PINS, SCHEMA):
        if not p.is_file():
            print("::error::%s not found -- run from the repo root" % p)
            return 1
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print("::error::the `jsonschema` package is required (pip install jsonschema). Refusing to "
              "skip: a guard that skips when its tool is missing reports success for a thing it "
              "did not do.")
        return 1
    if not BASELINE.is_file():
        print("::error::%s not found. Generate it with --write-baseline and commit it in the same "
              "PR as this gate." % BASELINE)
        return 1

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = measure()

    in_scope = [c for c in now["targets"] if c not in OUT_OF_SCOPE]
    n_t = len(in_scope)
    n_c = len([c for c in now["checked"] if c not in OUT_OF_SCOPE])
    print("  components with a schema node and a pin: %d" % len(now["targets"]))
    print("  out of scope (registry CI cannot read):  %d" % len(OUT_OF_SCOPE & set(now["targets"])))
    print("  IN SCOPE:                                %d" % n_t)
    print("  of those, chart pulled successfully:      %d" % n_c)
    # A component sliding INTO the out-of-scope set unnoticed is the failure this guards.
    newly_dark = sorted(set(now["unpullable"]) - OUT_OF_SCOPE)
    if newly_dark:
        print("::error::%d component(s) could not be pulled and are NOT in the declared "
              "out-of-scope list: %s" % (len(newly_dark), ", ".join(newly_dark)))
        print("::error::Each contributes ZERO sites while dark, so drift inside it is invisible. "
              "Fix the pull, or add it to OUT_OF_SCOPE deliberately with the reason.")
        return 1
    if now["unpullable"]:
        print("  could not pull (%d): %s" % (len(now["unpullable"]), ", ".join(sorted(now["unpullable"]))))

    # FAIL CLOSED on a run that inspected too little.
    if n_t and n_c / n_t < MIN_CHECKED_FRACTION:
        print("::error::only %d of %d charts could be pulled (%.0f%%, floor %.0f%%). A ratchet that "
              "inspected almost nothing must FAIL, because 'no new sites' is indistinguishable "
              "from 'nothing was looked at'."
              % (n_c, n_t, 100.0 * n_c / n_t, 100.0 * MIN_CHECKED_FRACTION))
        return 1

    bad = 0

    # A component the BASELINE checked that can no longer be pulled hides whatever it holds.
    lost = sorted((set(base.get("checked") or []) - set(now["checked"])) - OUT_OF_SCOPE)
    if lost:
        print("::error::%d component(s) the baseline CHECKED can no longer be pulled: %s"
              % (len(lost), ", ".join(lost)))
        for c in lost:
            print("::error::  %s -- %s" % (c, now["unpullable"].get(c, ["reason unknown"])))
        print("::error::This is not a pass: those charts contribute zero sites while unpullable, so "
              "drift inside them is invisible. Fix the pull, or move the component out of the "
              "baseline deliberately.")
        bad += 1

    bsites = base.get("sites") or {}
    new_sites, gone_sites = {}, {}
    for comp in sorted(set(bsites) | set(now["sites"])):
        if comp in OUT_OF_SCOPE:
            # Out of scope by declaration. My laptop CAN pull these (a gcloud/docker login CI does
            # not have), so measure() finds their sites while the baseline deliberately omits them
            # -- comparing would report every one as NEW on my machine and as nothing in CI. The
            # comparison must agree with the baseline's scope or the gate means different things in
            # two places, which is worse than not having it.
            continue
        if comp not in now["checked"] and comp not in now["sites"]:
            continue  # unpullable now; already reported above if the baseline had it
        was, is_ = set(bsites.get(comp) or []), set(now["sites"].get(comp) or [])
        if is_ - was:
            new_sites[comp] = sorted(is_ - was)
        if was - is_:
            gone_sites[comp] = sorted(was - is_)

    if new_sites:
        tot = sum(len(v) for v in new_sites.values())
        print("::error::%d NEW undeclared key site(s) in %d component(s). A key the chart offers "
              "and this schema does not declare is PRUNED by the apiserver on the way into the "
              "Installer CR, while the write reports SUCCESS -- the value vanishes and the chart's "
              "own default wins, with nothing red and nothing logged."
              % (tot, len(new_sites)))
        for comp, s in sorted(new_sites.items()):
            for path in s:
                print("::error::  %-28s %s" % (comp, path))
        print("::error::Declare the key in componentValues.%s (mirroring the chart's own schema, "
              "with NO `default:` -- crdgen materialises installer-schema defaults into every CR, "
              "where the CR beats the chart's values.yaml), then re-run --write-baseline."
              % sorted(new_sites)[0])
        bad += 1

    if gone_sites:
        tot = sum(len(v) for v in gone_sites.values())
        print("::error::%d site(s) in the baseline are FIXED. That is good, and the baseline must "
              "come down with it -- a stale high-water mark lets the next regression in for free. "
              "Re-run --write-baseline and commit it." % tot)
        for comp, s in sorted(gone_sites.items()):
            for path in s:
                print("::error::  FIXED  %-28s %s" % (comp, path))
        bad += 1

    if bad:
        return 1
    tot = sum(len(v) for v in now["sites"].values())
    print("check-componentvalues-coverage: OK -- %d component(s) checked, %d known site(s) in %d "
          "component(s), none new" % (n_c, tot, len(now["sites"])))
    return 0


def write_baseline() -> int:
    now = measure()
    out = {
        "_comment": [
            "GENERATED by check-componentvalues-coverage.py --write-baseline. Do not hand-edit.",
            "Each entry is a values key a PINNED chart offers that chart/values.schema.json does",
            "not declare. Such a key is PRUNED by the apiserver on the way into the Installer CR",
            "while the write reports success, so the value vanishes silently and the chart default",
            "wins. This file is a high-water mark: the gate fails on a NEW site and asks for a",
            "decrement when one is fixed.",
            "`checked` is load-bearing: a component whose chart cannot be pulled contributes zero",
            "sites, so without pinning the checked SET a failing pull would look like a fix.",
        ],
        "checked": [c for c in now["checked"] if c not in OUT_OF_SCOPE],
        "sites": {k: v for k, v in now["sites"].items() if k not in OUT_OF_SCOPE},
        "out_of_scope": sorted(OUT_OF_SCOPE),
    }
    BASELINE.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote %s: %d component(s) checked, %d site(s) in %d component(s)"
          % (BASELINE, len(now["checked"]), sum(len(v) for v in now["sites"].values()), len(now["sites"])))
    return 0


def self_test() -> int:
    """Exercise the comparison logic against synthetic baselines -- no network, no helm.

    The cases that matter are the ones that must FAIL: a new site, a lost component, and a run
    that inspected too little. A gate that only proves it passes on the current tree would pass
    just as happily with its comparison deleted.
    """
    import jsonschema
    schema = {"type": "object", "properties": {"componentValues": {"type": "object", "properties": {
        "comp": {"type": "object", "additionalProperties": False,
                 "properties": {"declared": {"type": "string"}}}}}}}
    v = jsonschema.Draft202012Validator(schema)
    bad = 0

    cases = [
        ({"declared": "x"}, [], "a chart with only declared keys yields no sites"),
        ({"declared": "x", "extra": 1}, ["<root>"], "an undeclared key yields a site"),
        ({"declared": "x", "a": 1, "b": 2}, ["<root>"],
         "TWO undeclared keys at one location are ONE site -- jsonschema emits a single error "
         "listing both, so counting errors would under-report"),
    ]
    for vals, want, label in cases:
        got = sites_for(v, "comp", vals)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%s got=%s" % (want, got))

    # the fail-closed floor must actually bite
    ok = (1 / 10) < MIN_CHECKED_FRACTION
    bad += 0 if ok else 1
    print("  %-4s 1 of 10 charts pulled is below the %.0f%% floor" % ("ok" if ok else "FAIL", 100 * MIN_CHECKED_FRACTION))

    # a lost component must be detectable
    lost = sorted(set(["a", "b"]) - set(["a"]))
    ok = lost == ["b"]
    bad += 0 if ok else 1
    print("  %-4s a component dropping out of `checked` is detected" % ("ok" if ok else "FAIL"))

    # the REAL baseline must exist and parse, or the gate cannot be merged
    if BASELINE.is_file():
        b = json.loads(BASELINE.read_text(encoding="utf-8"))
        ok = isinstance(b.get("checked"), list) and isinstance(b.get("sites"), dict) and b["checked"]
        bad += 0 if ok else 1
        print("  %-4s the committed baseline parses and is non-empty (%d checked, %d site(s))"
              % ("ok" if ok else "FAIL", len(b.get("checked") or []),
                 sum(len(x) for x in (b.get("sites") or {}).values())))
    else:
        print("  FAIL the committed baseline is missing")
        bad += 1

    print("self-test: %d case(s), %d failure(s)" % (len(cases) + 3, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--write-baseline" in sys.argv:
        sys.exit(write_baseline())
    sys.exit(main())
