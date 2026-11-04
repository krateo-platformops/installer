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

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chart", "values.schema.json")
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


def chart_kube_version(chart_dir):
    """A chart declaring a kubeVersion floor cannot be rendered at helm's compiled-in default
    (v1.34.0 on helm 3.19.0) -- it refuses with "incompatible with Kubernetes vX". Render at the
    chart's own floor, exactly as the lint workflow does."""
    try:
        text = open(os.path.join(chart_dir, "Chart.yaml"), encoding="utf-8",
                    errors="replace").read()
    except OSError:
        return None
    m = re.search(r"^kubeVersion:.*?([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    return m.group(1) if m else None


def chart_root(untar_dir):
    """`helm pull --untar --untardir X` puts the chart at X/<chartName>/, which is NOT the path the
    caller asked for -- and the chart name need not equal the component name. The previous
    text-scan walked the tree recursively so it never needed the real root; `helm template` does.
    Returns the directory containing Chart.yaml, searching shallowest-first so a vendored subchart
    under charts/ can never be mistaken for the parent."""
    best = None
    for root, _dirs, files in os.walk(untar_dir):
        if "Chart.yaml" not in files:
            continue
        depth = root[len(untar_dir):].count(os.sep)
        if best is None or depth < best[0]:
            best = (depth, root)
    return best[1] if best else None


# Charts that REFUSE to render without decisions only an operator can make. Not a convenience
# list and not a skip-list: a named exclusion, with the coverage it costs stated, and a check in
# main() that each entry STILL cannot render -- so an entry cannot quietly outlive its reason.
#
# I tried placeholders first and it was the wrong instinct. external-dns `fail`s three times in
# sequence -- txtOwnerId (two clusters sharing a DNS zone overwrite each other's TXT records),
# env (the provider API token, which must come from a Secret placed out-of-band), and then
# policy=sync with empty domainFilters (which lets the instance DELETE records in every zone the
# credential can reach). Those guards are the chart being RIGHT. Every placeholder I supplied was
# me asserting a safe configuration for a component whose entire premise is that there is no safe
# default, and a table of such assertions is exactly the accumulating exception list that rendering
# was supposed to make unnecessary.
#
# COVERAGE COST, stated rather than hidden: this guard does not check external-dns's
# `requiresApiGroups`. Today it declares none, and the only foreign group its templates could
# render is gateway.networking.k8s.io, which is in BUILTIN -- so nothing is currently missed. If
# external-dns ever renders a CRD-backed group another component ships, this guard will not say so.
UNRENDERABLE_BY_DESIGN = {
    "external-dns": "three `fail` guards demand operator decisions with no safe default: "
                    "txtOwnerId, the provider API token in env, and domainFilters under policy=sync",
}


def component_values(comp, schema):
    """The values the UMBRELLA sends this component, from the installer's own schema defaults.

    A `default:` on an installer schema property reaches the CR, so these are the values a real
    install uses when nobody overrides them -- not a guess. Rendering a chart bare is a different
    experiment from rendering it as the installer does: builder-gate `fail`s on an empty
    `.Values.instances` and the schema defaults it to three builders, so a bare render says
    "unrenderable" about a chart that renders fine in production."""
    node = ((schema.get("properties") or {}).get("componentValues") or {}) \
        .get("properties", {}).get(comp, {})
    vals = {}
    if isinstance(node.get("default"), dict):
        vals.update(node["default"])
    for k, v in (node.get("properties") or {}).items():
        if isinstance(v, dict) and "default" in v and k not in vals:
            vals[k] = v["default"]
    return vals


def render(name, chart_dir, values=None):
    """Render the chart with its OWN default values. Returns (stdout, None) or (None, error).

    THIS IS THE WHOLE CORRECTNESS OF THE GUARD, and the previous version did not do it. It read
    TEMPLATE TEXT and called the result `groups_rendered` -- a name asserting something the code
    never established: a template body sits under `{{- if ... }}`, and a gate that is false means
    the object is never created. installer#614 is exactly that. clickhouse-operator and
    mongodb-operator were required to have cert-manager.io because their charts CONTAIN
    cert-manager Certificate templates, while the wrapper charts disable them:

        clickhouse-operator  operator.certManager.enabled: false  (wrapper overrides the upstream
                             operator.webhook.enabled:     false   subchart's `true`)
        mongodb-operator     community.resource.tls.enabled: false (upstream default)

    So neither renders a single cert-manager.io object -- confirmed against the manifests helm
    holds for them on krateo-057, 0 each -- yet both were withheld on every portal install whose
    cluster lacked cert-manager. Rendering evaluates every gate for free and needs no list of
    exceptions, which a text scan can only ever accumulate.

    Values: the chart's own defaults, because that is what the umbrella sends when a component has
    no `componentValues` entry -- the case for both components above. NOT asserted by this guard:
    a cluster that DOES set componentValues can re-enable a gated path (the schema permits
    `componentValues.<name>.operator.*`), and this renders defaults, so it would not see that. A
    requirement that exists only under a non-default value is not expressible in
    `requiresApiGroups`, which is a static field."""
    kv = chart_kube_version(chart_dir)
    cmd = ["helm", "template", name, chart_dir]
    if kv:
        cmd += ["--kube-version", kv]
    vf = None
    if values:
        vf = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(values, vf, default_flow_style=False)
        vf.close()
        cmd += ["-f", vf.name]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if vf:
        os.unlink(vf.name)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300]
    return r.stdout, None


def shipped_crd_groups(chart_dir):
    """CRD groups the chart ships from its `crds/` DIRECTORY, parsed as YAML.

    Only crds/ is read here, because helm does NOT render that directory -- it applies those files
    install-once, outside the template pipeline, so they never appear in `helm template` output.
    CRDs a chart renders as TEMPLATES do appear there and are picked up by scan_rendered, so
    scanning template text for them would be redundant as well as unsafe.

    PARSED, not regex-matched, and that distinction caught a false positive in this guard's own
    first honest run. An earlier version matched a `group:` line across any file that merely contained
    the string "CustomResourceDefinition", and over the rendered output it matched the `group:`
    FIELD inside a RESTAction -- a resource the chart QUERIES. builders-page was recorded as
    shipping ogen.krateo.io on the strength of three RESTAction templates and zero CRDs, which made
    github-provider-kog's real requirement look satisfied and silently disabled this guard's own
    self-test. `spec.group` of a document whose `kind` is CustomResourceDefinition is the only thing
    that means "this chart provides this group". It is the same discipline the column-0 apiVersion
    rule already applied, which I failed to carry over to `group`."""
    shipped, unparsed = set(), []
    for root, _dirs, files in os.walk(chart_dir):
        if os.path.basename(root) != "crds":
            continue
        for f in sorted(files):
            if not f.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(root, f)
            try:
                docs = list(yaml.safe_load_all(open(path, encoding="utf-8", errors="replace")))
            except yaml.YAMLError as e:
                unparsed.append(f"{path}: {str(e)[:90]}")
                continue
            for d in docs:
                if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition":
                    g = ((d.get("spec") or {}).get("group") or "")
                    if g and g not in BUILTIN:
                        shipped.add(g)
    return shipped, unparsed


def scan_rendered(output, chart_dir):
    """returns (groups_rendered, groups_shipped_as_crds, parse_errors)

    The rendered output is PARSED rather than regex-scanned. Each document's own `apiVersion` is
    the group the chart creates an object in -- no column-0 heuristic needed, because that heuristic
    existed only to tell an object's apiVersion from a field VALUE while reading template TEXT.
    Parsed documents cannot confuse the two."""
    rendered, shipped, errs = set(), set(), []
    try:
        docs = list(yaml.safe_load_all(output))
    except yaml.YAMLError as e:
        return set(), set(), [f"rendered output is not parseable YAML: {str(e)[:120]}"]
    for d in docs:
        if not isinstance(d, dict):
            continue
        av = str(d.get("apiVersion") or "")
        g = group_of(av)
        if g and g not in BUILTIN:
            rendered.add(g)
        if d.get("kind") == "CustomResourceDefinition":
            sg = ((d.get("spec") or {}).get("group") or "")
            if sg and sg not in BUILTIN:
                shipped.add(sg)
    disk, unparsed = shipped_crd_groups(chart_dir)
    shipped |= disk
    errs += unparsed
    return rendered - shipped, shipped, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--only", help="limit to one component (debugging)")
    a = ap.parse_args()

    pins = yaml.safe_load(open(PINS))["components"]
    schema = json.load(open(SCHEMA))
    by_name = {c["name"]: c for c in pins}

    tmp = tempfile.mkdtemp(prefix="hard-dep-gate-")
    try:
        rendered, shipped, failed, unrendered, stale_input, excluded = {}, {}, [], [], [], []
        for c in pins:
            if a.only and c["name"] != a.only:
                continue
            d = pull(c["name"], c["version"], c.get("chart", c["name"]), c.get("repo"), tmp)
            if d is None:
                failed.append(f"{c['name']}@{c['version']} (pull)")
                continue
            root = chart_root(d)
            if root is None:
                unrendered.append(f"{c['name']}@{c['version']}: no Chart.yaml found under the "
                                  f"untarred chart")
                continue
            vals = component_values(c["name"], schema)
            out, err = render(c["name"], root, vals)
            if c["name"] in UNRENDERABLE_BY_DESIGN:
                # The exclusion must still be earned. If this chart NOW renders, the entry has
                # outlived its reason and must go -- an exclusion that excludes nothing reads as
                # diligence while quietly removing a component from the guard's subject.
                if out is not None:
                    stale_input.append(c["name"])
                excluded.append(c["name"])
                continue
            if out is None:
                # A render failure must NOT read as "renders nothing". That is the false negative
                # this guard cannot afford: it would silently drop a real requirement.
                unrendered.append(f"{c['name']}@{c['version']}: {err}")
                continue
            r, sh, perrs = scan_rendered(out, root)
            if perrs:
                # An unparseable document is not an absence of CRDs. Withhold rather than compute a
                # comparison over a chart whose contents were only partly read.
                unrendered.append(f"{c['name']}@{c['version']}: " + "; ".join(perrs[:2]))
                continue
            rendered[c["name"]], shipped[c["name"]] = r, sh

        if failed:
            print(f"::error::WITHHOLDING — could not pull {len(failed)} chart(s): "
                  + ", ".join(failed[:6]))
            print("  A guard that inspected nothing passes identically to one that found nothing.")
            return 2
        if stale_input:
            print(f"::error::{len(stale_input)} UNRENDERABLE_BY_DESIGN entr(ies) now RENDER, so the "
                  f"exclusion has outlived its reason and is silently removing a component from "
                  f"this guard's subject. Remove the entry: " + ", ".join(stale_input))
            return 2
        if unrendered:
            print(f"::error::WITHHOLDING — {len(unrendered)} chart(s) could not be RENDERED. A "
                  f"render failure is not an absence of requirements, and treating it as one is "
                  f"how a real dependency goes undeclared:")
            for u in unrendered[:8]:
                print(f"  {u}")
            return 2
        if not any(shipped.values()):
            print("::error::WITHHOLDING — no component was found to ship ANY CRD group. The scan "
                  "is not seeing CRDs, so every comparison below would be vacuously clean.")
            return 2

        if excluded:
            print(f"  EXCLUDED (unrenderable by design, requirements NOT checked): "
                  + ", ".join(f"{n} — {UNRENDERABLE_BY_DESIGN[n]}" for n in excluded))

        # A group is SATISFIED WITHIN THE FEATURE if any component that is on whenever this one is
        # also ships it: same `feature`, or no feature at all (always rendered). Only then is there
        # no cross-feature exposure.
        #
        # THE PREVIOUS RULE ASKED THE WRONG QUESTION and it produced nine false positives on the
        # first honest run. It skipped the same-feature PAIRING (`continue`) and then kept looking,
        # so finding ANY differently-featured shipper was reported as a missing requirement -- even
        # when a same-feature component ships the group and the requirement cannot fail. All nine
        # coreAgents components render kagent.dev; kagent-crds ships kagent.dev and is itself
        # feature=coreAgents (verified: pulled 0.1.6 from krateo-agentiko, 13 CRD-bearing files,
        # group kagent.dev present). With coreAgents ON, kagent-crds is ON. portal-agents also
        # ships the group under feature=portal, and that coincidence is the whole false positive.
        #
        # Acting on those nine would have added `requiresApiGroups: [kagent.dev]` to every agent in
        # the fleet and suppressed all of them on any install with coreAgents ON and portal OFF --
        # which is the failure this guard exists to prevent, caused by the guard.
        satisfied_in_feature = {}
        for a_name in rendered:
            af = by_name[a_name].get("feature")
            ok = set()
            for b_name, b_groups in shipped.items():
                bf = by_name[b_name].get("feature")
                if bf == af or bf is None:
                    ok |= b_groups
            satisfied_in_feature[a_name] = ok

        findings = []
        for a_name, a_groups in rendered.items():
            ac = by_name[a_name]
            for b_name, b_groups in shipped.items():
                if b_name == a_name:
                    continue
                overlap = (a_groups & b_groups) - satisfied_in_feature[a_name]
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

        # THE OTHER DIRECTION: a group DECLARED that the chart does not render. Reported because
        # #614 is this defect, not the missing kind: clickhouse-operator and mongodb-operator both
        # declared cert-manager.io, neither renders a cert-manager object (their wrappers disable
        # the Certificate templates), and the declaration withheld them on every portal install
        # whose cluster lacked cert-manager. A guard that only finds MISSING requirements cannot
        # see that, and a requirement that is not real is not harmless -- it suppresses a working
        # component, which is the same damage as the defect this guard was built for.
        #
        # Judged against the values the UMBRELLA sends (schema defaults). A requirement that exists
        # only under a non-default componentValues override is NOT expressible in
        # `requiresApiGroups` anyway -- it is a static field -- so there is no legitimate shape this
        # report can be wrong about. Excluded charts are skipped: nothing was rendered for them.
        overdeclared = []
        for name, groups in sorted(((c["name"], set(c.get("requiresApiGroups") or []))
                                    for c in pins if c.get("requiresApiGroups")), key=lambda x: x[0]):
            if name in UNRENDERABLE_BY_DESIGN or name not in rendered:
                continue
            extra = sorted(groups - rendered[name])
            if extra:
                overdeclared.append((name, extra))

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
                if (rendered[probe] & b_groups) - satisfied_in_feature.get(probe, set()) \
                        and by_name[b_name].get("feature") != by_name[probe].get("feature"):
                    refound.append(b_name)
            by_name[probe]["requiresApiGroups"] = saved
            if not refound:
                print(f"::error::SELF-TEST FAILED — removing `requires` from {probe} produced no "
                      f"finding, so this guard would not have caught the 0.3.474 defect.")
                # A self-test that only says "failed" sends the next reader to re-derive what it
                # already knows. Print the evidence: what the probe renders, who ships each of
                # those groups, under which feature, and whether satisfied_in_feature absorbed it.
                pf = by_name[probe].get("feature")
                print(f"  {probe} (feature={pf}) renders: {sorted(rendered[probe])}")
                sat = satisfied_in_feature.get(probe, set())
                for g in sorted(rendered[probe]):
                    shippers = [(b, by_name[b].get("feature")) for b, gs in shipped.items()
                                if g in gs]
                    print(f"    {g}: absorbed_by_same_feature={g in sat}")
                    for b, bf in shippers:
                        same = "SAME feature" if bf == pf else f"feature={bf}"
                        print(f"      shipped by {b} ({same})")
                    if not shippers:
                        print("      shipped by NOBODY — so no component provides it at all")
                return 2
            print(f"  self-test OK — dropping `requires` from {probe} is detected "
                  f"(would demand: {', '.join(refound)})")

        if overdeclared:
            print(f"::error::{len(overdeclared)} component(s) DECLARE a `requiresApiGroups` group "
                  f"their chart does not render. The declaration is not harmless: it withholds the "
                  f"component on every cluster that lacks the group, for a dependency it does not "
                  f"have (installer#614):")
            for name, extra in overdeclared:
                print(f"  {name} declares {', '.join(extra)} but renders "
                      f"{sorted(rendered[name]) or 'no foreign group at all'}")
                print(f"    -> remove {', '.join(extra)} from its pin's `requiresApiGroups`")
            print("  NOT a failure, deliberately. Removing a requirement that IS real re-creates "
                  "the 0.3.474 defect -- the component is emitted into a cluster with no such "
                  "kind, chart-inspector cannot render it, and the composition 500s forever. This "
                  "guard renders with DEFAULT values, so it cannot see a requirement that only "
                  "exists under a componentValues override. Each line above is a candidate for a "
                  "human to verify and remove, not an instruction.")

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
