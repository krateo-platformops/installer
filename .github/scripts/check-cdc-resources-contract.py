#!/usr/bin/env python3
"""CI guard: a per-Kind cdc `resources` pin must reach a field core-provider actually reads, and
must carry the whole contract.

WHY THIS EXISTS. The installer rendered this block at the CompositionDefinition's spec.resources
for as long as the feature existed. The CD CRD declares spec as
{apiRef, chart, controller, deploy, statusDataTemplate, upgradePolicy} -- there is no
spec.resources -- so the apiserver PRUNED every value while `kubectl` reported success. Measured on
krateo-057 2026-11-15: 0 of 73 live CDs carried spec.resources, and portal's controller ran
resources byte-identical to a component with no pin at all, having carried a 512Mi pin since #65
("so it stops OOMKilling on cold reconcile"). The OOMKills stopped because the chart's DEFAULT
limit was already 512Mi, so the intervention looked like it had worked. A pruned field and a
redundant field are indistinguishable from the outcome -- which is why this is a gate and not a
code review note.

THREE RULES, each with a falsifier below:

  1. SPEC FIELD EXISTS. Every top-level spec key the template emits must be one the CRD declares.
     This is the general form of the bug and it covers fields nobody has invented yet.

  2. THE PATH IS spec.controller.resources. Asserted by name as well as by rule 1, because
     `controller` existing is not the same as `resources` being nested inside it: a render at
     spec.controller would also satisfy rule 1 while still being wrong.

  3. THE CONTRACT IS WHOLE. spec.controller.resources REPLACES core-provider's cdc.resources
     rather than merging (cdcvalues.go:34, assets/cdc/deployment.yaml:149), so a pin that omits a
     field silently drops a default. Required: requests.cpu, requests.memory, limits.memory.
     FORBIDDEN: limits.cpu -- core-provider's values.yaml:416 marks the omission deliberate,
     measured at ~99.8% of CFS periods throttled during the cold-start burst.

WHAT THIS DOES NOT COVER: whether a chosen memory value is RIGHT for a controller. That needs its
working-set history, which lives in observability's data, not here. This gate only ensures the
value reaches the controller and carries the whole block.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

import yaml

CHART = "chart"
PINS = "chart/files/component-pins.min.yaml"
DEFS = "chart/templates/definitions.yaml"

# Recorded from the LIVE CRD on krateo-057, 2026-11-15, served version v1alpha1:
#   kubectl get crd compositiondefinitions.core.krateo.io -o json
# Kept as a constant because this gate must run offline, in a PR, with no cluster. If
# core-provider adds a spec field, this list is what needs updating -- and the failure is loud.
CRD_SPEC_FIELDS = {"apiRef", "chart", "controller", "deploy", "statusDataTemplate", "upgradePolicy"}
REQUIRED = {("requests", "cpu"), ("requests", "memory"), ("limits", "memory")}
FORBIDDEN = {("limits", "cpu")}

VALUES = "chart/values.yaml"

# ---- the POOL path, a SECOND resources contract with DIFFERENT merge semantics ----
#
# core-provider's pool step 5 adds core-provider.cdc.pool.default.{resources,workers} and
# core-provider.cdc.pool.overrides.<poolName>.{resources,workers}, sizing the Deployment
# `cdc-pool-<name>`. core-provider stated the semantics on 2026-11-17, and they are NOT the same
# as the dedicated-controller path above:
#
#   cdc.pool.default.resources            REPLACES cdc.resources. A pool never inherits the
#                                         per-controller default, because a pool is not a
#                                         controller-sized process. So the DEFAULT must be whole,
#                                         exactly like a spec.controller.resources pin.
#   cdc.pool.overrides.<name>.resources   MERGES over cdc.pool.default.resources, deep, key by
#                                         key. So an OVERRIDE may be partial: omitting
#                                         limits.memory keeps the default's, it does not drop it.
#                                         Same for workers.
#
# limits.cpu is forbidden on BOTH, for the reason in rule 3: a cpu limit throttled the engine in
# ~99.8% of CFS periods on cold start, and core-provider omits it deliberately (values.yaml:416).
#
# WHY THIS GATE EXISTS BEFORE THE KEYS DO. The block is not in chart/values.yaml yet -- pool step
# 5 has not merged. A rule with no population passes vacuously, which reads exactly like a clean
# result, so the falsifiers below plant each violation and assert it is caught. The rule is inert
# today and ARMED today, and those are different things.
#
# AND A NAME COLLISION, recorded because it is the kind I keep getting wrong:
# cdc.pool.*.workers is the pool's TOTAL across its members. spec.controller.workers is PER
# member and is capped by it. Same word, different denominator. The cap is not enforced yet --
# it needs an unstructured-runtime change (core-provider#561), which also blocks pool step 6.
POOL_DEFAULT_REQUIRED = {("requests", "cpu"), ("requests", "memory"), ("limits", "memory")}
POOL_FORBIDDEN = {("limits", "cpu")}


def _res_pairs(res):
    """The (section, key) pairs a resources block sets, e.g. {("requests","cpu")}."""
    if not isinstance(res, dict):
        return None
    return {(k, kk) for k, v in res.items() if isinstance(v, dict) for kk in v}


def pool_violations(pool):
    """The pool contract, as a pure function so the falsifiers need no chart.

    Returns a list of human-readable violations. An absent or empty block is NOT a violation --
    it is the state before pool step 5 merges -- and the caller reports the population size so a
    vacuous pass cannot be mistaken for a checked one.
    """
    out = []
    if not isinstance(pool, dict):
        return out

    default = (pool.get("default") or {})
    dres = default.get("resources")
    if dres is not None:
        pairs = _res_pairs(dres)
        if pairs is None:
            out.append("cdc.pool.default.resources is not a mapping")
        else:
            missing = POOL_DEFAULT_REQUIRED - pairs
            if missing:
                out.append(
                    "cdc.pool.default.resources is missing %s -- it REPLACES cdc.resources for a "
                    "pool's Deployment, so an omitted field is a DROPPED default, not an "
                    "inherited one" % sorted(".".join(m) for m in missing))
            bad = POOL_FORBIDDEN & pairs
            if bad:
                out.append(
                    "cdc.pool.default.resources sets %s -- a cpu limit throttled the engine in "
                    "~99.8%% of CFS periods on cold start; core-provider omits it deliberately"
                    % sorted(".".join(b) for b in bad))

    for pname, ov in sorted((pool.get("overrides") or {}).items()):
        ores = (ov or {}).get("resources")
        if ores is None:
            continue
        pairs = _res_pairs(ores)
        if pairs is None:
            out.append("cdc.pool.overrides.%s.resources is not a mapping" % pname)
            continue
        # PARTIAL IS LEGAL HERE. An override merges over the default key by key, so the
        # completeness rule deliberately does NOT apply -- asserting it would reject the exact
        # shape core-provider shipped (an override setting only requests.memory).
        bad = POOL_FORBIDDEN & pairs
        if bad:
            out.append(
                "cdc.pool.overrides.%s.resources sets %s -- forbidden on every path, including an "
                "override that merges over a compliant default"
                % (pname, sorted(".".join(b) for b in bad)))
    return out


def die(msg):
    print(f"::error::check-cdc-resources-contract: {msg}")
    sys.exit(1)


def render_cds(pins_text=None, defs_text=None):
    """Render the chart and return every CompositionDefinition it emits, as dicts.

    Writes into a COPY so a mutant can be rendered without touching the tree.
    """
    d = tempfile.mkdtemp()
    dst = os.path.join(d, "chart")
    subprocess.run(["cp", "-R", CHART, dst], check=True, capture_output=True)
    cy = os.path.join(dst, "Chart.yaml")
    spec = yaml.safe_load(open(cy, encoding="utf-8").read().replace("CHART_VERSION", "9.9.9"))
    spec.pop("dependencies", None)
    spec["version"] = "9.9.9"
    open(cy, "w", encoding="utf-8").write(yaml.safe_dump(spec, default_flow_style=False))
    if pins_text is not None:
        open(os.path.join(dst, "files", os.path.basename(PINS)), "w", encoding="utf-8").write(pins_text)
    if defs_text is not None:
        open(os.path.join(dst, "templates", "definitions.yaml"), "w", encoding="utf-8").write(defs_text)
    r = subprocess.run(
        ["helm", "template", "t", dst, "--kube-version", "1.36.0",
         "--set", "features.coreAgents=true", "--set", "features.specialistAgents=true",
         "--set", "features.portal=true", "--set", "features.agentGateway=true"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr.strip()[:400]
    out = []
    for doc in yaml.safe_load_all(r.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "CompositionDefinition":
            out.append(doc)
    return out, None


def main():
    if not os.path.exists(DEFS):
        die(f"{DEFS} is missing")

    pins = yaml.safe_load(open(PINS, encoding="utf-8"))
    comps = pins["components"] if isinstance(pins, dict) else pins
    pinned = {c["name"]: c["resources"] for c in comps if c.get("resources")}

    # ---- RULE 3, on the pins themselves: the contract is whole ----
    bad = []
    for name, res in sorted(pinned.items()):
        have = {(k, kk) for k, v in res.items() if isinstance(v, dict) for kk in v}
        missing = REQUIRED - have
        present_forbidden = FORBIDDEN & have
        if missing:
            bad.append(f"{name}: missing {sorted('.'.join(m) for m in missing)} -- "
                       f"spec.controller.resources REPLACES cdc.resources, so an omitted field is "
                       f"a DROPPED default, not an inherited one")
        if present_forbidden:
            bad.append(f"{name}: sets {sorted('.'.join(f) for f in present_forbidden)} -- a cpu "
                       f"limit throttled the engine in ~99.8% of CFS periods on cold start; "
                       f"core-provider omits it deliberately")
    if bad:
        die("pin contract violated:\n  " + "\n  ".join(bad))
    print(f"  ok   {len(pinned)} pin(s) carry the whole contract "
          f"(requests.cpu+memory, limits.memory, no cpu limit): {sorted(pinned)}")

    # ---- CONTROL: the render must actually produce CDs, or every rule below is vacuous ----
    cds, err = render_cds()
    if cds is None:
        die(f"the chart did not render, so nothing below was checked: {err}")
    if not cds:
        die("the render produced ZERO CompositionDefinitions. Every assertion below would pass "
            "on an empty set, which reads exactly like a clean result.")
    print(f"  ok   control: the render produced {len(cds)} CompositionDefinition(s)")

    # ---- RULE 1: every spec field emitted is one the CRD declares ----
    emitted = set()
    for cd in cds:
        emitted |= set((cd.get("spec") or {}).keys())
    undeclared = emitted - CRD_SPEC_FIELDS
    if undeclared:
        die(f"the chart emits CD spec field(s) the CRD does not declare: {sorted(undeclared)}.\n"
            f"  The apiserver PRUNES an undeclared field while the write reports success, so the "
            f"value silently never reaches core-provider.\n"
            f"  CRD declares: {sorted(CRD_SPEC_FIELDS)}\n"
            f"  If core-provider has ADDED a field, update CRD_SPEC_FIELDS in this gate.")
    print(f"  ok   every emitted spec field is declared by the CRD: {sorted(emitted)}")

    # ---- RULE 2: the path is spec.controller.resources, by name ----
    with_res = {cd["metadata"]["name"]: cd for cd in cds
                if ((cd.get("spec") or {}).get("controller") or {}).get("resources")}
    at_root = sorted(cd["metadata"]["name"] for cd in cds if "resources" in (cd.get("spec") or {}))
    if at_root:
        die(f"{len(at_root)} CD(s) render resources at spec.resources, which the CRD does not "
            f"declare: {at_root[:8]}")
    expect = set(pinned)
    got = set(with_res)
    if got != expect:
        die(f"pins set resources for {sorted(expect)} but the render puts "
            f"spec.controller.resources on {sorted(got)}.\n"
            f"  missing from the render: {sorted(expect - got)}\n"
            f"  unexpected in the render: {sorted(got - expect)}")
    print(f"  ok   all {len(got)} pinned value(s) land at spec.controller.resources")

    # the rendered values must equal the pinned ones, not merely exist
    for name, cd in sorted(with_res.items()):
        rendered = cd["spec"]["controller"]["resources"]
        if rendered != pinned[name]:
            die(f"{name}: pinned {json.dumps(pinned[name])} but rendered "
                f"{json.dumps(rendered)}")
    print(f"  ok   each rendered block equals its pinned value")

    # ---- FALSIFIER: a template that renders at spec.resources MUST be refused ----
    defs = open(DEFS, encoding="utf-8").read()
    mutant = defs.replace("  controller:\n    resources:\n      {{- toYaml . | nindent 6 }}",
                          "  resources:\n    {{- toYaml . | nindent 4 }}")
    if mutant == defs:
        die("FALSIFIER could not be built: the expected controller/resources render is not in "
            f"{DEFS}, so this gate cannot prove it detects the original bug.")
    mcds, merr = render_cds(defs_text=mutant)
    if mcds is None:
        print(f"  ok   FALSIFIER: the spec.resources mutant does not even render ({merr[:60]})")
    else:
        mroot = [cd["metadata"]["name"] for cd in mcds if "resources" in (cd.get("spec") or {})]
        if not mroot:
            die("FALSIFIER FAILED: a template rendering at spec.resources produced no CD with "
                "spec.resources, so rule 2 cannot fail and proves nothing.")
        print(f"  ok   FALSIFIER: the spec.resources mutant emits it on {len(mroot)} CD(s), "
              f"which rule 1 and rule 2 both refuse")

    # ---- FALSIFIER: a pin with a cpu limit MUST be refused ----
    ptext = open(PINS, encoding="utf-8").read()
    pm = yaml.safe_load(ptext)
    pl = pm["components"] if isinstance(pm, dict) else pm
    hit = False
    for c in pl:
        if c.get("resources"):
            c["resources"].setdefault("limits", {})["cpu"] = "500m"
            hit = True
            break
    if not hit:
        die("FALSIFIER could not be built: no pin carries resources, so the contract rule has no "
            "population and its pass is vacuous.")
    mhave = {(k, kk) for k, v in c["resources"].items() if isinstance(v, dict) for kk in v}
    if not (FORBIDDEN & mhave):
        die("FALSIFIER FAILED: a planted cpu limit was not seen by the contract check.")
    print(f"  ok   FALSIFIER: a planted limits.cpu on {c['name']} is caught by the contract rule")

    # ---- RULE 4: the POOL path, whose default must be whole and whose override may be partial ----
    pool = {}
    if os.path.exists(VALUES):
        vals = yaml.safe_load(open(VALUES, encoding="utf-8")) or {}
        pool = (((vals.get("core-provider") or {}).get("cdc") or {}).get("pool") or {})
    pv = pool_violations(pool)
    if pv:
        die("pool contract violated:\n  " + "\n  ".join(pv))
    n_pool = (1 if (pool.get("default") or {}).get("resources") is not None else 0) \
             + sum(1 for o in (pool.get("overrides") or {}).values()
                   if (o or {}).get("resources") is not None)
    if n_pool:
        print(f"  ok   {n_pool} pool resources block(s) satisfy the contract "
              f"(default whole, overrides may be partial, no cpu limit anywhere)")
    else:
        print("  ok   0 pool resources block(s) present -- pool step 5 has not merged, so RULE 4 "
              "has no population. The falsifiers below prove it is armed anyway.")

    # ---- FALSIFIER: a pool DEFAULT missing limits.memory MUST be refused ----
    probe = {"default": {"resources": {"requests": {"cpu": "100m", "memory": "660Mi"}}}}
    if not pool_violations(probe):
        die("FALSIFIER FAILED: a pool default with no limits.memory was accepted, so the "
            "completeness half of rule 4 proves nothing.")
    print("  ok   FALSIFIER: a pool default missing limits.memory is refused")

    # ---- FALSIFIER: limits.cpu MUST be refused on the default AND on an override ----
    whole = {"requests": {"cpu": "100m", "memory": "660Mi"}, "limits": {"memory": "1Gi"}}
    if not pool_violations({"default": {"resources": dict(whole, limits={"memory": "1Gi",
                                                                         "cpu": "500m"})}}):
        die("FALSIFIER FAILED: a planted limits.cpu on the pool default was not caught.")
    if not pool_violations({"default": {"resources": whole},
                            "overrides": {"p": {"resources": {"limits": {"cpu": "500m"}}}}}):
        die("FALSIFIER FAILED: a planted limits.cpu on a pool override was not caught -- an "
            "override merges over a compliant default, so this is the path most likely to "
            "reintroduce the throttle unnoticed.")
    print("  ok   FALSIFIER: a planted limits.cpu is refused on the pool default AND on an override")

    # ---- CONTROL: core-provider's own shipped shapes MUST pass, or rule 4 is too strict ----
    # The default Diego picked, and the partial override they gave as its example.
    shipped = {"default": {"resources": whole, "workers": 10},
               "overrides": {"example": {"resources": {"requests": {"memory": "900Mi"}}}}}
    strict = pool_violations(shipped)
    if strict:
        die("CONTROL FAILED: rule 4 rejects the shape core-provider is shipping, so it would "
            "block pool step 5 rather than guard it: %s" % strict)
    print("  ok   CONTROL: core-provider's shipped default and its partial override both pass")

    print(f"check-cdc-resources-contract: OK — {len(pinned)} pin(s), {len(cds)} CD(s) rendered, "
          f"{n_pool} pool block(s), 4 rules + 4 falsifiers + 1 control")
    return 0


if __name__ == "__main__":
    sys.exit(main())
