#!/usr/bin/env python3
"""Exercises inst.kogUrls — keycloak-config-kog's three DERIVED values.

WHY THIS EXISTS. #968 derived baseUrl and tokenEndpoint from
componentValues.keycloak.keycloak.http.relativePath, so the KOG and the Webhook token generator would
call Keycloak under /keycloak instead of the root. keycloak-provider-kog then measured that the
derivation was UNREACHABLE on every existing cluster: inst.componentValuesResolved is called from the
fresh-install CR AND from inst.componentValuesPatch, the merge patch applied on EVERY upgrade, so a
value filled there is WRITTEN INTO the Installer CR, becomes an explicit operator value, and the
"only if absent" guard never fires again. krateo-057's CR carries both URLs WITHOUT /keycloak, frozen
at first install.

The fix derives them at render with inst.forcePath, which runs BEFORE the componentValues merge --
and that merge uses `merge`, not `mergeOverwrite`, so the destination wins and a stale persisted value
is overridden rather than honoured. No componentValuesUnset is needed on any cluster.

WHY A PROBE AND NOT A RENDER. compositions.yaml emits a component only when all its `deps` are Ready
(Pass B, inst.depsReady) and keycloak-config-kog has SIX. Asserting through a render needs a fixture
carrying six Ready dep compositions -- a lot of scaffolding to reach three printf calls, and brittle
against any dep list change. So the derivation was EXTRACTED into inst.kogUrls, exactly as
inst.platformRoutes was extracted for the same reason, and this probe calls it directly.

WHAT IT CANNOT PROVE: that forcePath beats the componentValues merge. That is a property of
compositions.yaml's ordering, measured separately by rendering line 386's exact shape (`merge` keeps
the destination, `mergeOverwrite` does not). This probe proves the VALUES are right; that measurement
proves they WIN.

Usage: check-kog-urls.py [CHART_DIR]   (default: ../../chart relative to this file)
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required by check-kog-urls.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "chart")

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  kog: |
{{ (include "inst.kogUrls" (list $)) | indent 4 }}
"""

# (label, namespaces.krateo, relativePath or None, expected baseUrl suffix, expected token suffix)
CASES = [
    ("relativePath /keycloak", "krateo-system", "/keycloak",
     ":8080/keycloak", ":8080/keycloak/realms/master/protocol/openid-connect/token"),
    ("relativePath UNSET (today's behaviour)", "krateo-system", None,
     ":8080", ":8080/realms/master/protocol/openid-connect/token"),
    ("TRAILING SLASH normalises", "krateo-system", "/keycloak/",
     ":8080/keycloak", ":8080/keycloak/realms/master/protocol/openid-connect/token"),
    ("ROOT `/` normalises to empty", "krateo-system", "/",
     ":8080", ":8080/realms/master/protocol/openid-connect/token"),
    ("a DIFFERENT namespace is honoured", "krateo-other", "/keycloak",
     ":8080/keycloak", ":8080/keycloak/realms/master/protocol/openid-connect/token"),
]


def probe_dir():
    td = tempfile.mkdtemp(prefix="kogurls-")
    p = os.path.join(td, "probe")
    os.makedirs(os.path.join(p, "templates"))
    with open(os.path.join(p, "Chart.yaml"), "w") as f:
        f.write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    with open(os.path.join(p, "values.yaml"), "w") as f:
        f.write("namespaces: {krateo: krateo-system}\ncomponentValues: {}\n")
    shutil.copy(os.path.join(CHART, "templates", "_helpers.tpl"),
                os.path.join(p, "templates", "_helpers.tpl"))
    with open(os.path.join(p, "templates", "pr.yaml"), "w") as f:
        f.write(PROBE)
    return td, p


def run(chart, values):
    with tempfile.TemporaryDirectory() as td:
        vf = os.path.join(td, "v.yaml")
        with open(vf, "w") as f:
            yaml.safe_dump(values, f)
        r = subprocess.run(["helm", "template", "probe", chart, "-f", vf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap":
                return yaml.safe_load((d.get("data") or {}).get("kog") or "") or {}, None
        return None, "no probe ConfigMap in the render"


def main():
    ok = True

    def check(good, msg):
        nonlocal ok
        print("%s  %s" % ("ok  " if good else "FAIL", msg))
        if not good:
            ok = False

    td, chart = probe_dir()
    try:
        for label, ns, rel, want_base, want_tok in CASES:
            values = {"namespaces": {"krateo": ns}, "componentValues": {}}
            if rel is not None:
                values["componentValues"] = {"keycloak": {"keycloak": {"http": {"relativePath": rel}}}}
            got, err = run(chart, values)
            if got is None:
                check(False, "%s: probe failed to render: %s" % (label, err))
                continue
            base, tok, bns = got.get("baseUrl", ""), got.get("tokenEndpoint", ""), got.get("bootstrapNamespace", "")
            good = (base.endswith(want_base) and tok.endswith(want_tok)
                    and ns in base and bns == ns
                    and "//realms" not in tok and ":8080//" not in base)
            check(good, "%s -> baseUrl %s | token ...%s | bootstrapNamespace %s"
                  % (label, base, tok[-52:], bns))

        # NEGATIVE CONTROL: the probe must be able to FAIL. A deliberately wrong expectation must not pass.
        got, err = run(chart, {"namespaces": {"krateo": "krateo-system"},
                               "componentValues": {"keycloak": {"keycloak": {"http": {"relativePath": "/keycloak"}}}}})
        if got is None:
            check(False, "negative control: probe failed to render: %s" % err)
        else:
            would_pass = got.get("baseUrl", "").endswith(":8080")
            check(not would_pass,
                  "NEGATIVE CONTROL: with relativePath /keycloak the baseUrl must NOT end at bare :8080 "
                  "(got %s)" % got.get("baseUrl"))

        # A STALE value in componentValues must not change what the helper derives -- the helper is the
        # single source; forcePath's precedence over the merge is measured separately.
        got, err = run(chart, {"namespaces": {"krateo": "krateo-system"},
                               "componentValues": {
                                   "keycloak": {"keycloak": {"http": {"relativePath": "/keycloak"}}},
                                   "keycloak-config-kog": {
                                       "keycloak": {"baseUrl": "http://STALE:8080"},
                                       "auth": {"externalSecret": {"tokenEndpoint": "http://STALE:8080/x"},
                                                "bootstrap": {"namespace": "WRONG"}}}}})
        if got is None:
            check(False, "stale-value case: probe failed to render: %s" % err)
        else:
            check("STALE" not in got.get("baseUrl", "") and "STALE" not in got.get("tokenEndpoint", "")
                  and got.get("bootstrapNamespace") == "krateo-system",
                  "a STALE keycloak-config-kog value does not influence the derivation "
                  "(baseUrl %s, bootstrapNamespace %s)" % (got.get("baseUrl"), got.get("bootstrapNamespace")))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    if not ok:
        print("::error::check-kog-urls: the derived KOG URLs are NOT what this gate requires")
        return 1
    print("\ncheck-kog-urls: OK — %d case(s) plus a negative control and a stale-value case; "
          "relativePath is honoured and normalised, and the namespace is followed" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
