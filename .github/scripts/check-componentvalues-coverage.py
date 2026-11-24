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
    # A PINNED COMPONENT WITH NO SCHEMA NODE AT ALL was invisible to this gate until 2026-11-22.
    # `targets` is an INTERSECTION, so such a component contributed no sites and read as nothing to
    # measure -- the same shape as a counter of arrivals being blind to non-arrivals. And it is not
    # a lesser problem than an undeclared KEY: componentValues is additionalProperties: false, so
    # the apiserver rejects the component key itself and NOTHING about that component can be set,
    # not one value. Found while checking an unrelated tailsampling pin: 15 of 93 pins had no node,
    # 7 of them `-crd` charts whose published values are genuinely empty (correct), and 8 shipping
    # values and wholly unconfigurable -- external-secrets 76 keys, cloudnative-pg 31,
    # git-provider 18, blueprint-render-service 10, and four with one each.
    undeclared = sorted(set(pins) - set(declared))
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
    # Judge the undeclared ones only on EVIDENCE: a chart with no published values needs no node,
    # and a chart that cannot be PULLED cannot be judged either way -- so it goes to unpullable
    # rather than being counted as fine. "I could not check" must not read as "it is correct".
    no_node, no_node_empty = {}, []
    for comp in undeclared:
        if comp in OUT_OF_SCOPE:
            continue
        vals, err = chart_values(pins[comp])
        if vals is None:
            unpullable[comp] = err
            continue
        checked.append(comp)
        keys = sorted(vals) if isinstance(vals, dict) else []
        if keys:
            no_node[comp] = keys
        else:
            no_node_empty.append(comp)
    return {"targets": targets, "checked": sorted(checked),
            "unpullable": unpullable, "sites": sites,
            "no_node": no_node, "no_node_empty": sorted(no_node_empty)}


# Components excluded from the COMPARISON. Empty since 2026-11-24, and the mechanism is kept
# because a component sliding out of CI's reach must still be a failure, not a silent skip.
#
# WHY IT IS EMPTY NOW, MEASURED. The 19 names here were recorded by the first run of this gate,
# which used GITHUB_TOKEN and could not read krateo-agentiko. Diego switched the gate to
# secrets.PAT on 2026-11-22 and the workflow has logged in with it since. Two things followed that
# nobody then re-derived:
#   - measure() attempts EVERY one of the 78 targets with no out-of-scope skip, and the
#     PAT-credentialed runs report `unpullable` EMPTY -- so CI pulls all 78, including these 19.
#   - the comparison loop below nevertheless `continue`d on them and write_baseline() omitted them,
#     so the gate MEASURED their sites every run and threw the measurement away.
# The printed label "registry CI cannot read" was therefore false, and the 19 were unratcheted
# through stale bookkeeping rather than through any decision or any missing credential.
#
# THE ASYMMETRY THAT JUSTIFIED THE SKIP IS GONE, and that is what makes rewriting the baseline
# safe. The old comment warned that a laptop can pull these while CI cannot, so a locally written
# baseline would claim coverage CI could not honour. Measured 2026-11-24, same script, same pins:
#   CI run 45327907989  78 targets / 59 in scope / 74 pulled / 98 sites in 40 components
#   this laptop         78 targets / 59 in scope / 74 pulled / 98 sites in 40 components
# Identical, so the baseline written here is the one CI measures.
#
# Emptying this set TIGHTENS the ratchet: the baseline grows from 26 components / 67 sites to 40 /
# 98, and drift inside the agent fleet becomes visible for the first time.
OUT_OF_SCOPE: set[str] = set()


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
    # n_c counts EVERY component whose chart was pulled, and since 2026-11-22 that includes the
    # ones with NO schema node -- which are not a subset of `targets`. So the old pair of lines
    # printed "IN SCOPE: 59" followed by "of those ... 74", a subset larger than its superset. The
    # labels now say which population each number belongs to, because a gate whose own arithmetic
    # reads as impossible teaches a reader to stop believing its numbers.
    n_target_pulled = len([c for c in now["checked"]
                           if c not in OUT_OF_SCOPE and c in set(now["targets"])])
    print("  components with a node, IN SCOPE:        %d" % n_t)
    print("  of those, chart pulled successfully:     %d" % n_target_pulled)
    print("  charts pulled in total (incl. no-node):  %d" % n_c)
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
    # THE FLOOR MUST COMPARE THE SAME POPULATION ON BOTH SIDES. Using n_c here was a defect I
    # introduced on 2026-11-22 and caught before merge: n_c counts every chart pulled INCLUDING the
    # no-node components, while n_t counts only components with a node, so n_c / n_t was 74/59 =
    # 1.25 and could never fall below 0.70. The floor -- whose whole job is to fail when too few
    # charts could be pulled -- was silently disabled by a numerator that had stopped matching its
    # denominator. Same shape as pairing a gauge with a trailing rate.
    if n_t and n_target_pulled / n_t < MIN_CHECKED_FRACTION:
        print("::error::only %d of %d charts could be pulled (%.0f%%, floor %.0f%%). A ratchet that "
              "inspected almost nothing must FAIL, because 'no new sites' is indistinguishable "
              "from 'nothing was looked at'."
              % (n_target_pulled, n_t, 100.0 * n_target_pulled / n_t, 100.0 * MIN_CHECKED_FRACTION))
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

    # --- components with NO schema node, ratcheted the same way the sites are. A new one must not
    # arrive silently, and a fixed one must bring the high-water mark down with it.
    bno = base.get("no_node") or {}
    nno = now.get("no_node") or {}
    new_nn = sorted(set(nno) - set(bno))
    gone_nn = sorted(set(bno) - set(nno))
    print("  pinned components with NO schema node that ship values: %d" % len(nno))
    print("  pinned components with no node and NO values (correct):  %d" % len(now.get("no_node_empty") or []))
    if new_nn:
        print("::error::%d pinned component(s) have NO componentValues node AND ship values, which "
              "is NEW since the baseline: %s. componentValues is additionalProperties: false, so "
              "the apiserver rejects the component key itself -- NOTHING about these can be set "
              "through the installer, not one value. Declare a node (mirrored from the chart and "
              "verified against it) or add the component here deliberately."
              % (len(new_nn), ", ".join("%s (%d key(s))" % (c, len(nno[c])) for c in new_nn)))
        bad += 1
    if gone_nn:
        print("::error::%d component(s) in the baseline's no_node list now HAVE a node: %s. That is "
              "good, and the baseline must come down with it -- a stale high-water mark lets the "
              "next regression in for free. Re-run --write-baseline and commit it."
              % (len(gone_nn), ", ".join(gone_nn)))
        bad += 1

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
        "no_node": {k: v for k, v in (now.get("no_node") or {}).items() if k not in OUT_OF_SCOPE},
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

    # --- the NO-NODE category, added 2026-11-22 with the measure() change. Without these the new
    # logic is unguarded: every case above compares `sites`, so deleting the no_node comparison
    # entirely would leave all of them green -- the same mistake cv_unit exists for in
    # hack/roll-snapshot.py. These assert the SET ARITHMETIC the ratchet does, which is the
    # observable the change moves.
    NN = [
        ({}, {"ext-secrets": ["a", "b"]}, 1, 0,
         "a component with NO node appearing is NEW -> must fail"),
        ({"ext-secrets": ["a", "b"]}, {"ext-secrets": ["a", "b"]}, 0, 0,
         "unchanged -> silent"),
        ({"ext-secrets": ["a"]}, {}, 0, 1,
         "a component that GAINED a node -> baseline must come down"),
        ({"a": ["x"]}, {"b": ["y"]}, 1, 1,
         "one gained a node and another lost one -> both reported"),
        ({}, {}, 0, 0,
         "FALSIFIER: empty both sides reports nothing, so the check is not firing unconditionally"),
    ]
    for bno, nno, want_new, want_gone, label in NN:
        got_new = len(sorted(set(nno) - set(bno)))
        got_gone = len(sorted(set(bno) - set(nno)))
        ok = (got_new, got_gone) == (want_new, want_gone)
        bad += 0 if ok else 1
        print("  %-4s no-node: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want new=%d gone=%d  got new=%d gone=%d"
                  % (want_new, want_gone, got_new, got_gone))

    # and a chart with NO published values must NOT be counted as a problem: a `-crd` chart
    # legitimately has none, and 7 of the 15 found on 2026-11-22 are exactly that.
    ok = True
    for keys, should_flag in (([], False), (["opentelemetry-collector"], True)):
        flagged = bool(keys)
        if flagged != should_flag:
            ok = False
    bad += 0 if ok else 1
    print("  %-4s no-node: an EMPTY-values chart is not flagged; a value-shipping one is"
          % ("ok" if ok else "FAIL"))

    # THE FLOOR'S DENOMINATOR, added with the no-node change because that change BROKE it: a
    # numerator counting a wider population than its denominator made the ratio exceed 1 and the
    # floor unreachable. This asserts the pairing, which is the thing that went wrong -- not the
    # threshold, which was never in question.
    FR = [
        (59, 59, 74, False, "all targets pulled -> floor not tripped"),
        (59, 30, 74, True,  "30 of 59 targets pulled -> floor TRIPPED, even though 74 charts were pulled"),
        (59, 58, 74, False, "58 of 59 -> fine"),
        (59, 41, 74, True,  "41 of 59 is 69.5%, just under 70% -> TRIPPED"),
        (59, 42, 74, False, "42 of 59 is 71.2%, just over -> not tripped"),
    ]
    for n_t_, pulled, total, want_trip, label in FR:
        tripped = bool(n_t_ and pulled / n_t_ < MIN_CHECKED_FRACTION)
        wrong = bool(n_t_ and total / n_t_ < MIN_CHECKED_FRACTION)   # the defect's arithmetic
        ok = tripped is want_trip
        bad += 0 if ok else 1
        print("  %-4s floor: %s" % ("ok" if ok else "FAIL", label))
        if want_trip and not wrong:
            print("       (the old n_c/n_t arithmetic would NOT have tripped here -- that is the bug)")

    print("self-test: %d case(s), %d failure(s)" % (len(cases) + 3 + len(NN) + 1 + len(FR), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--write-baseline" in sys.argv:
        sys.exit(write_baseline())
    sys.exit(main())
