#!/usr/bin/env python3
"""features.internalTls wiring gate - the CA issuer must be reachable on an agentGateway cluster.

WHY THIS GATE EXISTS. Before features.internalTls, `componentValues.authn.internal.tls.issuerRef`
was settable but UNUSABLE on 057, for a reason three hops away from anything TLS:

    gateway-api-crds       (no deps)              <- the five cluster-scoped Gateway API CRDs that
                                                      agentgateway-controller ALSO owns  = #557
    agentgateway           deps: [gateway-api-crds]
    cert-manager           deps: [agentgateway]   <- an ORDERING artifact, not a requirement
    cert-manager-issuers   deps: [cert-manager]

cert-manager does not need agentgateway to function; that dep existed so the edge came up before
its certs. But it chained the CA issuer behind gateway-api-crds, and therefore behind the
ingress/agentGateway mutual exclusivity (#557) - an exclusivity with nothing to do with TLS. 057
runs agentGateway, so it could not obtain a CA ClusterIssuer at all: 0 ClusterIssuer objects live.

WHY NOT helm-unittest. This gate's assertions are half ABSENCE ("no document named cert-manager"),
and helm-unittest's documentSelector can only select a document that exists - a selector that
matches nothing fails the test rather than satisfying it. Absence is the half that makes the
presence assertions mean anything, so the whole set lives here instead, the same way
check-rendered-names.py renders and inspects.

WHY RULE 2 IS NOT OPTIONAL. Moving cert-manager-issuers to a new feature outright would have
SILENTLY REMOVED it from every existing features.ingress install. It reaches the new feature through
alsoFeatures (the cloudnative-pg/cloudnativePg pattern), so either flag enables it. Rule 2 asserts
the old path is untouched; without it, rule 1 passing would say nothing about regression.

WHY RULE 5 IS NOT OPTIONAL EITHER. Rules 1-3 assert documents are PRESENT. A bug that rendered every
component regardless of features would pass all of them. Rule 5 asserts absence with no features
set, which is what makes every presence assertion above it informative.

    python3 .github/scripts/check-tls-feature-wiring.py              -> check
    python3 .github/scripts/check-tls-feature-wiring.py --self-test  -> the two falsifiers alone
"""
import json, os, shutil, subprocess, sys, tempfile

import yaml

CHART = "chart"
CM, CMI, TM = "cert-manager", "cert-manager-issuers", "trust-manager"
GWCRDS, AGW, AGC = "gateway-api-crds", "agentgateway", "agentgateway-controller"


def _prepare():
    """Copy the chart, substitute the CI version placeholders, fetch subchart deps."""
    if shutil.which("helm") is None:
        return None, None, "helm is not on PATH"
    root = tempfile.mkdtemp(prefix="tlswiring-")
    dst = os.path.join(root, "chart")
    shutil.copytree(CHART, dst)
    cy = os.path.join(dst, "Chart.yaml")
    with open(cy, encoding="utf-8") as fh:
        t = fh.read()
    if "CHART_VERSION" not in t:
        shutil.rmtree(root, ignore_errors=True)
        return None, None, "Chart.yaml no longer carries CHART_VERSION; this gate's substitution is stale"
    with open(cy, "w", encoding="utf-8") as fh:
        fh.write(t.replace("CHART_VERSION", "0.0.0-gate").replace("APP_VERSION", "0.0.0-gate"))
    r = subprocess.run(["helm", "dependency", "build", dst], capture_output=True, text=True, timeout=900)
    if r.returncode:
        shutil.rmtree(root, ignore_errors=True)
        return None, None, "helm dependency build failed: %s" % (r.stderr or r.stdout).strip()[:300]
    return root, dst, None


def render(dst, features):
    """(set_of_CompositionDefinition_names, stderr) for a feature map. names is None on failure."""
    vals = {"bootstrap": {"coreProvider": {"enabled": False}},
            "installDefinitions": True, "features": features}
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(vals, fh, default_flow_style=False)
    fh.close()
    try:
        r = subprocess.run(["helm", "template", "t", dst, "-f", fh.name, "--kube-version", "1.36.0"],
                           capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(fh.name)
    if r.returncode:
        return None, (r.stderr or r.stdout).strip()
    names = set()
    for doc in yaml.safe_load_all(r.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "CompositionDefinition":
            names.add((doc.get("metadata") or {}).get("name"))
    return names, ""


def main(argv):
    self_test = "--self-test" in argv
    root, dst, err = _prepare()
    if err:
        print("REFUSED: %s" % err)
        return 2
    try:
        fails = []

        def ck(ok, label, detail=""):
            print("  %s %s%s" % ("ok  " if ok else "FAIL", label, ("   " + detail) if detail else ""))
            if not ok:
                fails.append(label)

        # ---- FALSIFIER A: with no features, the TLS components are ABSENT.
        # Without this, every "is present" assertion below could be satisfied by a chart that
        # rendered everything unconditionally.
        base, e = render(dst, {})
        if base is None:
            print("REFUSED: the no-features render failed, so nothing below can be trusted: %s" % e[:300])
            return 2
        ck(CMI not in base and TM not in base,
           "FALSIFIER A: with NO features, cert-manager-issuers and trust-manager are absent",
           "(so their presence below is real, not unconditional)")

        # ---- FALSIFIER B: a mis-cased feature key is REFUSED by values.schema.json.
        # A schema that accepted anything would make rule 1 vacuous: the flag would "work" by
        # being ignored.
        bad, e = render(dst, {"internalTLS": True})
        ck(bad is None, "FALSIFIER B: features.internalTLS (wrong case) is REFUSED by the schema",
           "render exit != 0" if bad is None else "ACCEPTED -- the schema is not closed")

        if self_test:
            print()
            print("  self-test: %s" % ("both falsifiers hold" if not fails else "FAILURES: %s" % fails))
            return 1 if fails else 0

        # ---- RULE 1: internalTls alone brings the issuer and the Bundle controller,
        # and NEITHER our cert-manager blueprint NOR the Gateway API CRDs.
        a, e = render(dst, {"internalTls": True})
        if a is None:
            ck(False, "RULE 1: internalTls renders", e[:200])
        else:
            ck(CMI in a, "RULE 1: internalTls alone enables cert-manager-issuers")
            ck(TM in a, "RULE 1: internalTls alone enables trust-manager")
            ck(CM not in a, "RULE 1: internalTls alone does NOT render our cert-manager blueprint",
               "a hand-installed cert-manager is never adopted (cdc TakeOwnership, core-provider 2.13.82)")
            ck(GWCRDS not in a, "RULE 1: internalTls alone does NOT bring gateway-api-crds",
               "so #557 cannot fire against agentgateway-controller")

        # ---- RULE 2: THE REGRESSION GUARD. An existing features.ingress install must be unchanged.
        b, e = render(dst, {"ingress": True})
        if b is None:
            ck(False, "RULE 2: ingress renders", e[:200])
        else:
            for n in (GWCRDS, AGW, CM, CMI):
                ck(n in b, "RULE 2 (regression): ingress alone still enables %s" % n)
            ck(TM not in b, "RULE 2: ingress alone does NOT pull in trust-manager",
               "the new component stays opt-in")

        # ---- RULE 3: the combination that was IMPOSSIBLE before this feature existed.
        c, e = render(dst, {"internalTls": True, "agentGateway": True})
        if c is None:
            ck(False, "RULE 3: internalTls + agentGateway renders",
               "THIS IS THE WHOLE POINT OF THE FEATURE: %s" % e[:200])
        else:
            ck(CMI in c and AGC in c,
               "RULE 3: the CA issuer AND the agent gateway in ONE render",
               "impossible before: features.ingress and features.agentGateway are exclusive (#557)")
            ck(GWCRDS not in c, "RULE 3: and still no gateway-api-crds, so #557 cannot fire")

        # ---- RULE 4: trust-manager really comes from upstream, not from a krateo registry.
        # If it were silently repointed at a krateo mirror, every rule above would still pass.
        pins = yaml.safe_load(open("chart/files/component-pins.min.yaml", encoding="utf-8"))
        comps = pins["components"] if isinstance(pins, dict) else pins
        tm = [x for x in comps if x.get("name") == TM]
        ck(len(tm) == 1, "RULE 4: trust-manager is pinned exactly once", "found %d" % len(tm))
        if tm:
            ck(tm[0].get("repo") == "oci://quay.io/jetstack/charts",
               "RULE 4: trust-manager pins from upstream quay.io", repr(tm[0].get("repo")))
            ck(tm[0].get("upstream") is True,
               "RULE 4: trust-manager carries upstream: true",
               "check-pin-ci's provenance arm is EXPLICIT, never inferred")
            ck(tm[0].get("requiresApiGroups") == ["cert-manager.io"],
               "RULE 4: trust-manager requiresApiGroups [cert-manager.io], not deps:[cert-manager]",
               repr(tm[0].get("requiresApiGroups")))
        cmi = [x for x in comps if x.get("name") == CMI]
        if cmi:
            ck(cmi[0].get("requiresApiGroups") == ["cert-manager.io"],
               "RULE 4: cert-manager-issuers requiresApiGroups [cert-manager.io]",
               repr(cmi[0].get("requiresApiGroups")))
            ck("internalTls" in (cmi[0].get("alsoFeatures") or []),
               "RULE 4: cert-manager-issuers reaches internalTls via alsoFeatures",
               "which is what keeps features.ingress working unchanged")
            ck("deps" not in cmi[0],
               "RULE 4: cert-manager-issuers no longer deps on our cert-manager component")
        cm = [x for x in comps if x.get("name") == CM]
        if cm:
            ck("deps" not in cm[0],
               "RULE 4: cert-manager no longer deps on agentgateway",
               "that ordering artifact is what chained TLS to #557")

        print()
        print("  %s" % ("all rules pass" if not fails else "FAILURES: %s" % fails))
        return 1 if fails else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
