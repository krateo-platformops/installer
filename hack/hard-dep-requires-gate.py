#!/usr/bin/env python3
"""CI guard: a component whose chart RENDERS a kind that another pinned component SHIPS must declare
that API GROUP in `requiresApiGroups:`, not merely name its provider in `deps:`.

WHY THIS EXISTS. installer 0.3.474 shipped builder-gate pinned `feature: portal` with
`deps: [portal, kagent]`. On krateo-demo (portal ON, coreAgents OFF) it was emitted anyway and
chart-inspector 500'd on every reconcile:

    no matches for kind "RemoteMCPServer" in version "kagent.dev/v1alpha2"
    ensure CRDs are installed first

`deps` could never have stopped it. inst.depsReady (_helpers.tpl) SKIPS a dep whose own feature is
disabled — the readiness check sits INSIDE `if inst.featureEnabled $dep.feature`. That is right for
an OPTIONAL dep but makes `deps` an ORDERING constraint and nothing more. `requires` is the
render-time gate; this guard asserts the two are not confused.

Measuring the pin file for that shape found SIX latent twins of the same defect, none of which had
broken yet because neither live cluster had the exposing feature combination. This guard is what
stops a seventh.

WHAT IT CHECKS, per pinned component:
  1. pull its chart, collect every `apiVersion:` GROUP its templates render
  2. pull each OTHER pinned component's chart, collect every CRD GROUP it SHIPS
  3. if A renders a group that B ships, and A.feature != B.feature, then B must be in A.requires

WITHHOLDS (exit 2) rather than passing when it cannot do that work — a chart it cannot pull, or
zero groups discovered fleet-wide — because a guard that inspected nothing passes identically to one
that found nothing wrong. --self-test proves it flags a known-bad shape.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
import yaml

PINS = "chart/files/component-pins.yaml"
DEFAULT_ORG = "krateo-platformops"
FALLBACK_ORGS = ["krateo-platformops", "krateo-agentiko"]
# groups every chart may reference without shipping them: core k8s and the installer's own layer
BUILTIN = {"", "v1", "apps", "batch", "rbac.authorization.k8s.io", "networking.k8s.io",
           "policy", "autoscaling", "apiextensions.k8s.io", "admissionregistration.k8s.io",
           "coordination.k8s.io", "monitoring.coreos.com", "gateway.networking.k8s.io"}

# COLUMN 0 ONLY, and this is the whole correctness of the guard. A helm template writes the objects
# it CREATES starting at column 0; an INDENTED `apiVersion:` is a field VALUE nested inside some
# other object — in practice a portal widget or RestAction naming a resource it QUERIES. An earlier
# draft matched `^\s*apiVersion:` and so flagged portal-agents, reviews-page and github-provider-kog
# as unable to render without kagent/oasgen CRDs. krateo-demo refutes that directly: it runs portal
# ON with coreAgents OFF and all three are Ready=Available. Acting on those findings would have been
# WORSE than the defect this guard exists for — a wrong `requires` SUPPRESSES a working component.
API_RE = re.compile(r"^apiVersion:\s*[\"']?([A-Za-z0-9./-]+)[\"']?\s*$", re.M)
CRD_GROUP_RE = re.compile(r"^\s*group:\s*[\"']?([A-Za-z0-9.-]+)[\"']?\s*$", re.M)


def group_of(api_version):
    return api_version.split("/")[0] if "/" in api_version else ""


def pull(name, ver, chart, repo, dest):
    """`repo` in the pin file is a FULL OCI base (oci://ghcr.io/<org>/charts), not a bare org — an
    earlier draft of this guard treated it as an org, built oci://ghcr.io/oci://.../charts/charts/x
    and WITHHELD on nine charts. The withhold is what surfaced the bug, which is the whole argument
    for withholding instead of skipping quietly."""
    bases = []
    if repo:
        bases.append(repo.rstrip("/") if repo.startswith("oci://") else f"oci://ghcr.io/{repo}/charts")
    bases += [f"oci://ghcr.io/{o}/charts" for o in FALLBACK_ORGS]
    for base in bases:
        out = os.path.join(dest, name)
        r = subprocess.run(
            ["helm", "pull", f"{base}/{chart}", "--version", str(ver),
             "-d", dest, "--untar", "--untardir", out],
            capture_output=True, text=True)
        if r.returncode == 0 and os.path.isdir(out):
            return out
    return None


def scan(chart_dir):
    """returns (groups_rendered, groups_shipped_as_crds)"""
    rendered, shipped = set(), set()
    for root, _dirs, files in os.walk(chart_dir):
        for f in files:
            if not f.endswith((".yaml", ".yml", ".tpl")):
                continue
            try:
                text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            is_crd_file = "CustomResourceDefinition" in text
            for av in API_RE.findall(text):
                g = group_of(av)
                if g and g not in BUILTIN and "{{" not in g:
                    rendered.add(g)
            if is_crd_file:
                for g in CRD_GROUP_RE.findall(text):
                    if g and g not in BUILTIN:
                        shipped.add(g)
    # a chart never "renders" what it itself ships
    return rendered - shipped, shipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--only", help="limit to one component (debugging)")
    a = ap.parse_args()

    pins = yaml.safe_load(open(PINS))["components"]
    by_name = {c["name"]: c for c in pins}

    tmp = tempfile.mkdtemp(prefix="hard-dep-gate-")
    try:
        rendered, shipped, failed = {}, {}, []
        for c in pins:
            if a.only and c["name"] != a.only:
                continue
            d = pull(c["name"], c["version"], c.get("chart", c["name"]), c.get("repo"), tmp)
            if d is None:
                failed.append(f"{c['name']}@{c['version']}")
                continue
            r, s = scan(d)
            rendered[c["name"]], shipped[c["name"]] = r, s

        if failed:
            print(f"::error::WITHHOLDING — could not pull {len(failed)} chart(s): "
                  + ", ".join(failed[:6]))
            print("  A guard that inspected nothing passes identically to one that found nothing.")
            return 2
        if not any(shipped.values()):
            print("::error::WITHHOLDING — no component was found to ship ANY CRD group. The scan "
                  "is not seeing CRDs, so every comparison below would be vacuously clean.")
            return 2

        findings = []
        for a_name, a_groups in rendered.items():
            ac = by_name[a_name]
            for b_name, b_groups in shipped.items():
                if b_name == a_name:
                    continue
                overlap = a_groups & b_groups
                if not overlap:
                    continue
                bc = by_name[b_name]
                if ac.get("feature") == bc.get("feature"):
                    continue  # same gate: cannot be on without the other
                declared = set(ac.get("requiresApiGroups") or [])
                missing = sorted(overlap - declared)
                if not missing:
                    continue  # every shared group is declared
                findings.append((a_name, ac.get("feature"), b_name, bc.get("feature"), missing))

        checked = sum(len(v) for v in rendered.values())
        print(f"  scanned {len(rendered)} charts; {checked} rendered non-builtin group refs; "
              f"{sum(1 for v in shipped.values() if v)} charts ship CRDs")
        if a.self_test:
            # the known-bad shape: strip a real `requires` and assert it is flagged
            probe = next((c["name"] for c in pins if c.get("requiresApiGroups")), None)
            if probe is None:
                print("::error::SELF-TEST WITHHELD — no pin declares `requiresApiGroups`, so there "
                      "is no known-good shape to break.")
                return 2
            saved = by_name[probe].pop("requiresApiGroups")
            refound = []
            for b_name, b_groups in shipped.items():
                if b_name == probe:
                    continue
                if rendered[probe] & b_groups and by_name[b_name].get("feature") != by_name[probe].get("feature"):
                    refound.append(b_name)
            by_name[probe]["requiresApiGroups"] = saved
            if not refound:
                print(f"::error::SELF-TEST FAILED — removing `requires` from {probe} produced no "
                      f"finding, so this guard would not have caught the 0.3.474 defect.")
                return 2
            print(f"  self-test OK — dropping `requires` from {probe} is detected "
                  f"(would demand: {', '.join(refound)})")

        if findings:
            print(f"::error::{len(findings)} component(s) render a kind another component ships, "
                  f"across a feature boundary, without declaring it in `requires`:")
            for an, af, bn, bf, groups in findings:
                print(f"  {an} (feature={af}) renders {', '.join(groups)} which {bn} "
                      f"(feature={bf}) ships")
                print(f"    -> with {af} ON and {bf} OFF, chart-inspector cannot render {an} and "
                      f"the composition 500s forever. Add these to its pin's "
                      f"`requiresApiGroups`: {', '.join(groups)}")
            return 1
        declared = sum(len(c.get("requiresApiGroups") or []) for c in pins)
        print(f"hard-dep-requires: OK — every cross-feature CRD dependency is declared in "
              f"`requiresApiGroups` ({declared} group requirement(s) across "
              f"{sum(1 for c in pins if c.get('requiresApiGroups'))} component(s))")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
