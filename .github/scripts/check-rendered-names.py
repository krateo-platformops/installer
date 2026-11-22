#!/usr/bin/env python3
"""CI guard: every metadata.name a CHANGED pinned chart renders must be a valid Kubernetes name.

WHY THIS EXISTS, with a date and a cost. On 2026-11-14 tag 0.3.642 moved the 17-member
portal-pages line to 1.8.119. Sixteen converged. alerts-page could not, and never would:

    failed to create resource: Select.widgets.templates.krateo.io "alerts-hasIncidents" is
    invalid: metadata.name: Invalid value: "alerts-hasIncidents": a lowercase RFC 1123 subdomain
    must consist of lower case alphanumeric characters, '-' or '.', and must start and end with
    an alphanumeric character

`alerts-hasIncidents` -- a capital I. The apiserver REFUSES the object, so the composition sat at
Ready=Unknown/ObserveFailed with status.helmChartVersion stuck on the PREVIOUS chart while the cdc
retried observe -> helm -> fail about every three seconds, indefinitely. Structural, not a race:
no retry makes that name valid.

NOTHING WE OWNED WOULD HAVE CAUGHT IT. check-pin-ci confirms the source repo was green.
check-same-version-line confirms the 17 pins AGREE -- and it cannot see that the live cluster then
had 16 members at the new version and one stuck at the old, because it reads the repo. The
registry probe confirms the chart EXISTS. The values-path diff confirms no new key. Every one of
those passed. None of them renders the chart and looks at what it would create.

This does. `helm template` the charts whose pins CHANGED, and validate every rendered
metadata.name. It needs no cluster, and on the real case it finds exactly one bad name among 46
rendered documents.

    python3 .github/scripts/check-rendered-names.py              -> check changed pins
    python3 .github/scripts/check-rendered-names.py --self-test  -> offline cases + the real fixture
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

PINS = "chart/files/component-pins.yaml"
CHART_YAML = "chart/Chart.yaml"
DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"
MERGE_TARGETS = ("origin/main", "main")

# RFC 1123 subdomain: what the apiserver enforces on metadata.name for most kinds.
# Charts whose DEFAULTS deliberately fail the render, with the minimal values that make them
# renderable. Without this they come back "NOT RENDERED" and this gate correctly refuses to claim it
# checked them -- which is right, but means such a chart can never be cleared at all.
#
# internal-ca-bundle is the first: its target.namespaces is required and an EMPTY list fails with a
# sentence, because a Bundle with no namespaceSelector writes its ConfigMap to EVERY namespace in the
# cluster. The guard lives in the chart's RENDER rather than its CRD, because the chart's own
# defaults must satisfy its schema for `helm lint` -- so the generated CRD accepts [] and the render
# refuses it. krateo-blueprints states that as its convention for a required list, so this recurs.
#
# THE VALUES MUST NOT MASK A REGRESSION. The chart is rendered WITHOUT them first, and if it renders
# fine the gate REFUSES: either the fail-closed guard was removed -- a change worth failing on -- or
# the entry is stale. Values that silently substitute for a guard are worse than no entry.
RENDER_VALUES = {
    "internal-ca-bundle": {"target": {"namespaces": ["krateo-system"]}},
    # zendesk-kog and zendesk-organisations 0.3.1, added 2026-11-21. THESE TWO ARE THE REASON THE
    # "must not mask a regression" rule above is written as it is, because at 0.3.0 they did NOT
    # fail -- they rendered ZERO objects and EXITED 0, which is the vacuous pass this gate exists to
    # refuse. Measured, not inferred: `helm template` on each 0.3.0 chart printed nothing with no
    # error. So a fixture at 0.3.0 would have been me supplying values to paper over a missing
    # guard, and the rule would have refused the entry anyway.
    #
    # acmp (the chart owner) made them fail closed in krateo-zendesk-kog#21, released as 0.3.1:
    #   zendesk-kog            restDefinitions.request enabled by default; defaults fail on the
    #                          missing baseUrl, and `enabled: false` fails with "would install
    #                          nothing and report success"
    #   zendesk-organisations  an empty `organisations` fails with "organisations is empty, so
    #                          zendesk-organisations would install nothing"
    # Both messages say the same thing: to turn the service desk off, do not install the chart --
    # disable its FEATURE. That is builder-gate's wording and the same hazard.
    #
    # The guards are template `fail`s rather than schema `required`, so a DISABLED subchart is never
    # affected. And their CI now renders each chart without its ci/render-values.yaml and requires a
    # failure, then with it and requires >= 1 object -- the same two-sided check this gate performs,
    # so the two agree by construction rather than by coincidence.
    #
    # The organisation here is a PROBE, not 057's. 057's real entry lives on 057's CR only, because
    # an organisation names a customer's namespace and its end user's token. See chart/values.yaml.
    "zendesk-kog": {"zendesk": {"baseUrl": "https://krateo.zendesk.com"}},
    "zendesk-organisations": {
        "organisations": [
            {"name": "probe", "namespace": "probe", "tokenSecretRef": {"name": "probe"}}
        ]
    },
    # krateo-keycloak-realm has ALWAYS required this -- templates/userprofile.yaml:2 fails with
    # "parentClient.redirectUris is required when parentClient.enabled". VERIFIED AGAINST THE
    # OUTGOING PIN: 0.5.1 fails identically, so this is not a 0.5.3 regression. The gate only hit it
    # now because it scans CHANGED charts, and this pin had not moved since the gate was written --
    # which is worth noting as a property of the gate rather than of the chart: an unchanged pin's
    # render guard is unexercised here, so `--all` is what finds the rest of these.
    # The value is a PLACEHOLDER in the chart's own documented shape, not the production value;
    # Diego's approved value for 057 is https://portal.krateo.dev/auth?kind=oidc and belongs on the
    # CR, not in a CI fixture.
    "krateo-keycloak-realm": {
        "parentClient": {"redirectUris": ["https://cmp.example.com/auth?kind=oidc"]}
    },
    # builder-gate has ALWAYS required this -- gate.yaml:27 fails with ".Values.instances is empty,
    # so this chart would render NOTHING and report success". VERIFIED AGAINST THE OUTGOING PIN:
    # 1.8.45 fails with the identical message, so this is not a 1.8.46 regression.
    #
    # The chart's own message is worth keeping in mind here, because it describes this gate's job:
    # "If you meant to turn builder-gate OFF, disable its FEATURE rather than emptying instances".
    # Emptying instances is a chart that renders nothing and EXITS 0 -- exactly the vacuous pass this
    # gate exists to refuse, which is why the chart fails closed and why the fixture must supply the
    # value rather than the gate skipping the chart.
    "builder-gate": {"instances": [{"name": "portal-builder", "builders": ["portal-builder"]}]},
}

RFC1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
MAX_NAME = 253

# RBAC IS THE EXCEPTION, AND THIS GATE GOT IT WRONG. Role, ClusterRole, RoleBinding and
# ClusterRoleBinding names are validated as PATH SEGMENT NAMES, not as RFC 1123 subdomains: the
# apiserver forbids only "", ".", "..", "/" and "%". A COLON IS LEGAL AND CONVENTIONAL there --
# `system:controller:bootstrap-signer` ships with Kubernetes itself.
#
# Measured on 057 when this gate rejected trust-manager 0.20.0 for `trust-manager:leaderelection`:
#     clusterroles          384 total,  97 with a colon in the name
#     roles                 456 total,  18 with a colon
#     clusterrolebindings   373 total,  82 with a colon
#     rolebindings          460 total,  20 with a colon
# including `kube-system/cert-manager:leaderelection` -- the SAME shape, from the cert-manager chart
# this repo already pins. So the rejected name is not merely tolerated, it is the house convention of
# the upstream project, and the gate was about to block a correct chart.
#
# That matters beyond this one pin: the <name>:leaderelection convention is common to every
# controller-runtime chart, so the bug was a latent veto on a whole class of upstream components, and
# the only reason it surfaced today is that trust-manager is the first such chart pinned since the
# gate merged. A gate that rejects valid input is worse than no gate: it argues for patching an
# upstream chart to satisfy it.
RBAC_KINDS = frozenset(("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"))
PATH_SEGMENT_FORBIDDEN = ("/", "%")


def name_errors(kind, name):
    """Broken-rule sentences for one metadata.name; [] when it is valid."""
    bad = []
    s = str(name)
    if not s:
        return ["%s has an EMPTY metadata.name" % kind]
    if len(s) > MAX_NAME:
        bad.append("%s %r is %d characters, over the %d-character limit" % (kind, s, len(s), MAX_NAME))
    if kind in RBAC_KINDS:
        # path segment name: everything except "", ".", "..", "/" and "%"
        if s in (".", ".."):
            bad.append("%s %r is %r, which is not a usable path segment name" % (kind, s, s))
        for ch in PATH_SEGMENT_FORBIDDEN:
            if ch in s:
                bad.append("%s %r contains %r, which the apiserver forbids in an RBAC object name. "
                           "The apiserver REFUSES such an object, so the composition cannot converge "
                           "and the cdc retries indefinitely." % (kind, s, ch))
        return bad
    if not RFC1123_SUBDOMAIN.match(s):
        why = []
        if any(c.isupper() for c in s):
            why.append("it contains an UPPERCASE character")
        if s and not (s[0].isalnum() and s[0].islower() or s[0].isdigit()):
            why.append("it does not start with a lowercase alphanumeric")
        if s and not (s[-1].isalnum() and (s[-1].islower() or s[-1].isdigit())):
            why.append("it does not end with a lowercase alphanumeric")
        if re.search(r"[^a-z0-9.\-]", s):
            why.append("it contains a character outside [a-z0-9.-]")
        bad.append("%s %r is not a valid RFC 1123 subdomain%s. The apiserver REFUSES such an "
                   "object, so the composition cannot converge and the cdc retries indefinitely."
                   % (kind, s, (" -- " + "; ".join(why)) if why else ""))
    return bad


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def pins_at(ref=None):
    if ref is None:
        text = open(PINS, encoding="utf-8").read()
    else:
        r = _run(["git", "show", "%s:%s" % (ref, PINS)])
        if r.returncode:
            return None
        text = r.stdout
    doc = yaml.safe_load(text) or {}
    comps = doc.get("components") if isinstance(doc, dict) else doc
    return {c["name"]: c for c in (comps or [])}


def deps_at(ref=None):
    """{name: {chart, repo, version}} from chart/Chart.yaml's dependencies, or None."""
    if ref is None:
        try:
            text = open(CHART_YAML, encoding="utf-8").read()
        except OSError:
            return None
    else:
        r = _run(["git", "show", "%s:%s" % (ref, CHART_YAML)])
        if r.returncode:
            return None
        text = r.stdout
    doc = yaml.safe_load(text) or {}
    return {d["name"]: {"chart": d["name"],
                        "repo": d.get("repository") or DEFAULT_REPO,
                        "version": d.get("version")}
            for d in (doc.get("dependencies") or []) if d.get("name")}


def changed_charts():
    """[(label, chart, repo, version, origin)] whose version moved against the merge target.

    BOTH SOURCES, and that is not an embellishment. The installer pins charts in TWO places:
    chart/files/component-pins.yaml, and chart/Chart.yaml's `dependencies` (core-provider and
    core-provider-crds live only there). The first version of this gate read the pins file alone,
    so a core-provider bump would have rendered NOTHING and the gate would have reported "no pin
    version changed" — a clean result from an empty check.

    That is the same blind spot check-same-version-line had and that was closed earlier the SAME
    DAY, rebuilt here by accident hours later. Which is the argument for reading both sources in
    every gate that asks "what moved": the answer is never one file.
    """
    now_p = pins_at()
    now_d = deps_at()
    before_p = before_d = None
    for ref in MERGE_TARGETS:
        if before_p is None:
            before_p = pins_at(ref)
        if before_d is None:
            before_d = deps_at(ref)
        if before_p is not None and before_d is not None:
            break
    if before_p is None:
        return None, "could not read %s at any of %s" % (PINS, ", ".join(MERGE_TARGETS))
    if before_d is None:
        return None, "could not read %s at any of %s" % (CHART_YAML, ", ".join(MERGE_TARGETS))
    out = []
    for n, c in sorted((now_p or {}).items()):
        if (before_p.get(n) or {}).get("version") != c.get("version"):
            out.append((n, c.get("chart") or n, c.get("repo") or DEFAULT_REPO, c.get("version"),
                        PINS))
    for n, c in sorted((now_d or {}).items()):
        if (before_d.get(n) or {}).get("version") != c.get("version"):
            out.append((n, c["chart"], c["repo"], c.get("version"), CHART_YAML))
    return out, None


def rendered_names(repo, chart, version):
    """[(kind, name)] from `helm template`, or (None, reason)."""
    if shutil.which("helm") is None:
        return None, "helm is not on PATH"
    tmp = tempfile.mkdtemp(prefix="rendernames-")
    try:
        r = _run(["helm", "pull", "%s/%s" % (repo, chart), "--version", str(version),
                  "-d", tmp, "--untar"])
        if r.returncode:
            return None, "helm pull failed: %s" % (r.stderr or r.stdout).strip()[:200]
        root = os.path.join(tmp, chart)
        if not os.path.isdir(root):
            subdirs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            if len(subdirs) != 1:
                return None, "unexpected unpack layout: %s" % subdirs
            root = os.path.join(tmp, subdirs[0])
        extra = RENDER_VALUES.get(chart)
        vf = None
        if extra is not None:
            bare = _run(["helm", "template", "t", root, "--kube-version", "1.36.0"])
            if bare.returncode == 0:
                return None, ("RENDER_VALUES has an entry for %r but the chart renders FINE without "
                              "it. Either its fail-closed guard was removed -- which is a change "
                              "worth failing on -- or the entry is stale. Remove the entry or "
                              "restore the guard; do not leave values that mask it." % chart)
            vf = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
            yaml.safe_dump(extra, vf, default_flow_style=False)
            vf.close()
        cmd = ["helm", "template", "t", root, "--kube-version", "1.36.0"]
        if vf is not None:
            cmd += ["-f", vf.name]
        r = _run(cmd)
        if vf is not None:
            os.unlink(vf.name)
        if r.returncode:
            return None, "helm template failed: %s" % (r.stderr or r.stdout).strip()[:200]
        names = []
        for doc in yaml.safe_load_all(r.stdout):
            if isinstance(doc, dict) and doc.get("kind"):
                nm = (doc.get("metadata") or {}).get("name")
                if nm is not None:
                    names.append((doc["kind"], nm))
        return names, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


SELF_TESTS = [
    ("a plain lowercase name", "ConfigMap", "alerts-page", True),
    ("digits and hyphens", "Secret", "a1-b2-c3", True),
    ("a dotted subdomain", "Service", "svc.example.com", True),
    ("THE REAL CASE: a capital letter", "Select", "alerts-hasIncidents", False),
    ("all caps", "Select", "ALERTS", False),
    ("a leading hyphen", "ConfigMap", "-alerts", False),
    ("a trailing hyphen", "ConfigMap", "alerts-", False),
    ("an underscore", "ConfigMap", "alerts_page", False),
    ("a space", "ConfigMap", "alerts page", False),
    ("empty", "ConfigMap", "", False),
    ("254 characters", "ConfigMap", "a" * 254, False),
    ("253 characters", "ConfigMap", "a" * 253, True),
    # --- RBAC path segment names. THE REAL FALSE POSITIVE this gate produced, plus its live
    # precedent on 057, plus the cases that must STILL be refused so the exemption is not a hole.
    ("THE FALSE POSITIVE: a colon in a Role name", "Role", "trust-manager:leaderelection", True),
    ("its LIVE precedent on 057", "Role", "cert-manager:leaderelection", True),
    ("Kubernetes' own convention", "ClusterRole", "system:controller:bootstrap-signer", True),
    ("a colon in a RoleBinding", "RoleBinding", "trust-manager:leaderelection", True),
    ("an UPPERCASE RBAC name is legal too", "ClusterRole", "Admin:Reader", True),
    ("a slash is STILL refused in RBAC", "Role", "foo/bar", False),
    ("a percent is STILL refused in RBAC", "ClusterRole", "foo%bar", False),
    ("'..' is STILL refused in RBAC", "Role", "..", False),
    ("an empty RBAC name is STILL refused", "Role", "", False),
    ("254 chars is STILL refused in RBAC", "Role", "a" * 254, False),
    # and the exemption must NOT leak to other kinds
    ("a colon in a ConfigMap is STILL refused", "ConfigMap", "foo:bar", False),
    ("a colon in a Service is STILL refused", "Service", "foo:bar", False),
]


def self_test():
    fails = 0
    for label, kind, name, want_ok in SELF_TESTS:
        bad = name_errors(kind, name)
        ok = not bad
        if ok != want_ok:
            print("FAIL  %-40s %r -> ok=%s want=%s" % (label, name, ok, want_ok))
            fails += 1
        else:
            print("ok    %-40s %r -> %s" % (label, name[:28], "accepted" if ok else "refused"))

    # THE FIXTURE THAT MATTERS: the real chart must fail, and its predecessor must pass.
    # Skipped only when helm or the network is unavailable, and SAID so rather than silently.
    if shutil.which("helm") is None:
        print("note  helm absent, so the live alerts-page fixture was not exercised")
    else:
        for version, want_bad in (("1.8.119", True), ("1.8.118", False)):
            names, why = rendered_names(DEFAULT_REPO, "alerts-page", version)
            if names is None:
                print("note  alerts-page %s could not be rendered (%s), fixture not exercised"
                      % (version, why[:60]))
                continue
            bad = [(k, n) for k, n in names for _ in [0] if name_errors(k, n)]
            got_bad = bool(bad)
            if got_bad != want_bad:
                print("FAIL  fixture alerts-page %s: %d rendered names, bad=%s want_bad=%s %s"
                      % (version, len(names), got_bad, want_bad, bad[:2]))
                fails += 1
            else:
                print("ok    fixture alerts-page %-8s %3d names rendered, %s"
                      % (version, len(names),
                         ("REFUSED: %s %r" % bad[0]) if bad else "all valid"))

    print("check-rendered-names: %s"
          % ("OK -- %d cases + the live alerts-page fixture both ways" % len(SELF_TESTS)
             if not fails else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    changed, why = changed_charts()
    if changed is None:
        print("::error::check-rendered-names: %s" % why)
        return 2
    if not changed:
        print("check-rendered-names: no pin version changed in this PR — nothing to render")
        return 0
    if shutil.which("helm") is None:
        print("::error::check-rendered-names: helm is not on PATH, so NOTHING was rendered. This "
              "is a refusal, not a skip: an unrendered chart reads exactly like a clean one.")
        return 2
    total_names = 0
    unrenderable = []
    bad = []
    for comp, chart, repo, version, origin in changed:
        names, reason = rendered_names(repo, chart, version)
        if names is None:
            unrenderable.append((comp, chart, version, reason))
            continue
        total_names += len(names)
        for kind, nm in names:
            for e in name_errors(kind, nm):
                bad.append((comp, chart, version, e))
        print("  %-24s %-26s %-9s %3d names   from %s"
              % (comp, chart, version, len(names), os.path.basename(origin)))
    for comp, chart, version, reason in unrenderable:
        print("  %-24s %-26s %-9s NOT RENDERED: %s" % (comp, chart, version, reason[:60]))
    for comp, chart, version, e in bad:
        print("::error::check-rendered-names: %s (%s %s): %s" % (comp, chart, version, e))
    if bad:
        return 1
    # NON-VACUITY: a pass that rendered nothing is not a pass.
    if total_names == 0:
        print("::error::check-rendered-names: %d changed pin(s) but ZERO metadata.name values were "
              "rendered. Either every chart failed to render or the parse is broken; a zero here "
              "reads exactly like a clean result." % len(changed))
        return 2
    if unrenderable:
        print("::error::check-rendered-names: %d of %d changed chart(s) could not be rendered, so "
              "they were NOT checked. Named above. A chart that cannot be rendered has not been "
              "cleared." % (len(unrenderable), len(changed)))
        return 1
    srcs = sorted({os.path.basename(o) for *_, o in changed})
    print("check-rendered-names: OK — %d changed chart(s) from %s, %d metadata.name value(s), all "
          "valid Kubernetes names" % (len(changed), " + ".join(srcs), total_names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
