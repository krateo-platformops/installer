#!/usr/bin/env python3
"""CI guard (installer#523): turning a feature OFF must remove exactly that feature's components
from the render, and nothing else.

check-feature-keys.py asserts the STATIC mapping (pins' `feature:` values exist in the schema).
Nothing asserted the DYNAMIC one: that flipping a flag actually changes what the chart declares.
That gap is why #523 exists — five components on krateo-057 are gated on features that have been
unset at every retained revision, so the live cluster cannot answer the question either.

WHAT THE OTHER LAYER COVERS, so this one is not mistaken for the whole proof:
plumbing#52/#54 proves helm DELETES what a chart stopped declaring, per-PR against a real cluster.
This proves the chart STOPS DECLARING it. Neither implies the other and both are needed.

LIMITATION, stated rather than discovered: `helm template` DISABLES `lookup`, so every
lookup-gated object renders empty. compositions.yaml gates on inst.crdExists (a lookup), so
Compositions NEVER appear here and this guard covers the CompositionDefinition layer only. A CD is
what owns a component's generated CRD and its per-version controller, so it is the layer where a
stranded object does the damage — but this is not a Composition-level assertion and must not be
quoted as one.

NON-VACUITY: every feature that gates components must have at least one of them present in the
baseline, and turning it off must remove at least one object. A guard that inspects nothing passes
identically to one that found nothing wrong, so both directions are asserted per feature.
"""
import json, os, subprocess, sys, tempfile, shutil, collections
import yaml

CHART = "chart"

# COMPANION OBJECTS — hand-written templates that belong to a component but are not NAMED after one.
# Every such object must be declared here against the component it serves, because that relationship
# is otherwise invisible: the template names a feature literally and nothing ties it to the pin.
#
# THIS MAP IS THE #526 DETECTOR. hyperdx-admin-creds was gated on `specialistAgents` while its
# component alert-provider had been moved to `portal` by #482, so a portal-only install deployed
# alert-provider with no credentials and wedged for 43 minutes. With the component declared here,
# BOTH directions fail: disabling `portal` removes alert-provider without its companion, and
# disabling `specialistAgents` removes a companion whose component is still rendered.
#
# An undeclared hand-written object fails this guard on purpose. Declare it rather than loosening
# the rule — the declaration is the only place this coupling is written down.
COMPANIONS = {
    ("Secret", "hyperdx-admin-creds"): "alert-provider",
    ("Secret", "authn-jwt-signing-key"): "authn",
    ("ConfigMap", "krateo-installed-versions"): "repo-mcp-server",
}

# Objects that belong to the umbrella itself rather than to any component, so no feature gates them.
UMBRELLA = {("Job", "installer-ordered-teardown")}
KUBE_VERSION = "1.36.0"  # the chart declares kubeVersion >= 1.36.0-0; helm's default 1.34 fails

def render(chart_dir, features):
    args = ["helm", "template", "ci", chart_dir, "--kube-version", KUBE_VERSION]
    for k, v in sorted(features.items()):
        args += ["--set", f"features.{k}={'true' if v else 'false'}"]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"::error::render failed for features={features}")
        print(p.stderr.strip()[:2000])
        sys.exit(1)
    ids = set()
    for d in yaml.safe_load_all(p.stdout):
        if not isinstance(d, dict) or "kind" not in d:
            continue
        m = d.get("metadata") or {}
        ids.add((d["kind"], m.get("namespace") or "-", m.get("name")))
    return ids

def main():
    schema = json.load(open(f"{CHART}/values.schema.json"))
    declared = sorted(schema["properties"]["features"].get("properties", {}))
    pins = yaml.safe_load(open(f"{CHART}/files/component-pins.yaml"))["components"]

    by_feature = collections.defaultdict(set)
    for c in pins:
        if isinstance(c, dict) and c.get("feature"):
            by_feature[c["feature"]].add(str(c.get("name")))

    gating = sorted(f for f in by_feature if f in declared)
    if not gating:
        print("::error::no declared feature gates any component pin — this guard would inspect nothing")
        sys.exit(1)

    # Stage a copy: Chart.yaml ships version/appVersion as the CHART_VERSION placeholder, which
    # `helm dependency build` rejects ("chart.metadata.version \"CHART_VERSION\" is invalid").
    work = tempfile.mkdtemp(prefix="feature-render-")
    try:
        staged = os.path.join(work, "installer")
        shutil.copytree(CHART, staged)
        cy = os.path.join(staged, "Chart.yaml")
        txt = open(cy).read()
        txt = txt.replace("version: CHART_VERSION", "version: 0.0.0-ci")
        txt = txt.replace("appVersion: CHART_VERSION", "appVersion: 0.0.0-ci")
        open(cy, "w").write(txt)
        dep = subprocess.run(["helm", "dependency", "build", staged], capture_output=True, text=True)
        if dep.returncode != 0:
            print("::error::helm dependency build failed")
            print(dep.stderr.strip()[:2000])
            sys.exit(1)

        all_on = {f: True for f in declared}
        base = render(staged, all_on)
        print(f"baseline: {len(base)} objects with all {len(declared)} features on")
        if len(base) < 10:
            print("::error::baseline render produced too few objects — this comparison means nothing")
            sys.exit(1)

        failures = []
        for feat in gating:
            comps = by_feature[feat]
            # Non-vacuity A: this feature's components must actually be in the baseline, or
            # turning the feature off proves nothing about them.
            present = {n for (_k, _ns, n) in base if n in comps}
            if not present:
                failures.append(
                    f"{feat}: none of its {len(comps)} components appear in the baseline render "
                    f"({sorted(comps)}) — the toggle cannot be proven either way")
                continue

            off = render(staged, {**all_on, feat: False})
            removed = base - off
            added = off - base

            # Nothing may APPEAR when a feature is turned off.
            if added:
                failures.append(f"{feat}: turning it OFF ADDED objects: {sorted(added)}")

            # Non-vacuity B: something must go.
            if not removed:
                failures.append(
                    f"{feat}: turning it OFF removed NOTHING, but {sorted(present)} "
                    f"is gated on it and rendered in the baseline")
                continue

            # Every removal must be attributable to one of this feature's components, either by
            # being named after it or by being a declared companion of it.
            stray, undeclared = [], []
            for (k, ns, n) in sorted(removed):
                if n in comps:
                    continue
                owner = COMPANIONS.get((k, n))
                if owner is None:
                    if (k, n) in UMBRELLA:
                        stray.append((k, ns, n))  # umbrella objects must never be feature-gated
                    else:
                        undeclared.append((k, ns, n))
                elif owner not in comps:
                    stray.append((k, ns, n))
            if undeclared:
                failures.append(
                    f"{feat}: removed hand-written object(s) not declared in COMPANIONS: "
                    f"{undeclared} — add each one against the component it serves")
            if stray:
                failures.append(
                    f"{feat}: turning it OFF removed objects NOT gated on it: {stray}")

            # THE #526 ASSERTION, the other direction: if a component goes, its declared
            # companions must go with it. A companion left behind is a component deployed
            # without the object it cannot start without.
            orphaned = sorted(
                (k, n) for (k, n), owner in COMPANIONS.items()
                if owner in present and (k, "krateo-system", n) not in removed
                and any(n2 == n for (_k2, _ns2, n2) in base)
            )
            if orphaned:
                failures.append(
                    f"{feat}: component(s) removed but their companion object(s) SURVIVED: "
                    f"{orphaned} — the companion's gate does not follow its component "
                    f"(this is the installer#526 defect)")

            print(f"  {feat:18s} off -> -{len(removed):2d} objects, all attributable")

        if failures:
            print("::error::feature-render: turning a feature off does not cleanly remove its "
                  "components (installer#523):")
            for f in failures:
                print(f"  {f}")
            sys.exit(1)

        print(f"feature-render: OK — {len(gating)} feature(s) gate components "
              f"({', '.join(gating)}); each removes only its own objects when disabled")
    finally:
        shutil.rmtree(work, ignore_errors=True)

if __name__ == "__main__":
    main()
