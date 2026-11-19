#!/usr/bin/env python3
"""Exercises inst.platformRoutes for identity-lifecycle's ACCESS-REQUEST INTAKE in path mode.

WHY THIS EXISTS. keycloak-provider-kog measured that on a path-mode cluster NOTHING routes the
intake: IL's own intake.route renders only for a HOST-mode gateway ("where the installer routes by
path, it renders nothing"), and the installer's identity-lifecycle pin carried no exposure keys at
all. Confirmed on krateo-057: 26 HTTPRoutes, 27 rules, ZERO mentioning request-access. A browser's
POST /request-access/api/... matched platform-frontend `/` and was answered by the SPA -- a 200 with
the wrong handler, which is worse than a 404 because it looks like it worked.

WHY A PROBE CHART AND NOT A helm-unittest SUITE. I wrote the suite first, against
templates/httproutes.yaml, and it could never have worked: on a path-mode cluster the routes are not
httproutes.yaml documents at all. All 26 live routes on 057 are owned by the agentgateway-policies
release and named agentgateway-policies-platform-*, rendered from the platform route list that
inst.platformRoutes threads into that composition. httproutes.yaml is the HOST-mode path.

Then the same suite aimed at compositions.yaml hits the wall this repo already documented, in
chart/tests/keycloak_path_routes_test.yaml:

    "the keycloak pin carries deps as well as requiresApiGroups, so reaching the derivation needs a
     fixture that satisfies ALL of those gates. Until it does, the component is withheld,
     inst.compEnabled is false, and the derivation is skipped -- so the assertion was passing
     judgement on a code path it never entered. An assertion that cannot reach its subject is worse
     than no assertion: it reads as coverage."

identity-lifecycle carries requiresApiGroups: [policies.kyverno.io] and feature identityLifecycle,
so it has the same problem. check-platform-routes.py and check-keycloak-routes.py already solved it:
render a PROBE CHART that calls inst.platformRoutes directly with synthetic pins, bypassing every
chart-level gate so the derivation itself is the subject.

FOUR THINGS I GOT WRONG BEFORE ARRIVING HERE, all by reasoning from a plausible mechanism instead of
reading the thing: a guard that fired on a vacuous lookup and broke every non-cluster render;
`notExists` with a documentSelector, which reports "document not found" as an ERROR rather than a
pass; five gates in httproutes.yaml of which I had set one; and requiresApiGroups, whose matcher
also needs a spec.versions entry with served: true. Each was real. None was the cause.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(os.path.dirname(os.path.dirname(HERE)), "chart")

PROBE = '''apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  routes: |
{{ (include "inst.platformRoutes" (list $ .Values.components 8080 .Values.gwTLS .Values.gwCertRef)) | indent 4 }}
'''

IL_PIN = {
    "name": "identity-lifecycle",
    "expose": True,
    "exposePort": 80,
    "svcMatch": "identity-lifecycle-intake",
    "gatewayPathsFrom": "accessRequestIntake",
    "gatewayRewrite": False,
    "gatewayAuth": "public",
}

_D = {"d": None}


def probe_dir():
    td = tempfile.mkdtemp(prefix="intakeroute-")
    p = os.path.join(td, "probe")
    os.makedirs(os.path.join(p, "templates"))
    with open(os.path.join(p, "Chart.yaml"), "w") as f:
        f.write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    with open(os.path.join(p, "values.yaml"), "w") as f:
        f.write("features: {}\ncomponentValues: {}\ncomponents: []\ngwTLS: false\n"
                "gwCertRef: {}\nnamespaces: {krateo: krateo-system}\n")
    shutil.copy(os.path.join(CHART, "templates", "_helpers.tpl"),
                os.path.join(p, "templates", "_helpers.tpl"))
    with open(os.path.join(p, "templates", "pr.yaml"), "w") as f:
        f.write(PROBE)
    return p


def routes(values):
    if _D["d"] is None:
        _D["d"] = probe_dir()
    with tempfile.TemporaryDirectory() as td:
        vf = os.path.join(td, "v.yaml")
        with open(vf, "w") as f:
            yaml.safe_dump(values, f)
        r = subprocess.run(["helm", "template", "probe", _D["d"], "-f", vf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap":
                parsed = yaml.safe_load((d.get("data") or {}).get("routes") or "") or {}
                return (parsed.get("routes") or []), None
        return None, "no ConfigMap in the probe render"


# A SECOND, UNRELATED PIN SO THE NEGATIVES CANNOT PASS VACUOUSLY. With only the IL pin in the list,
# "renders no /request-access route" and "renders nothing at all" are the same observation -- and a
# derivation that always returned empty would satisfy both negatives. This control route must be
# present in EVERY case, including the ones where the intake is off.
CONTROL_PIN = {
    "name": "frontend",
    "expose": True,
    "exposePort": 8080,
    "svcMatch": "frontend",
    "gatewayPath": "/",
    "gatewayRewrite": False,
    "gatewayAuth": "public",
}


def base(enabled=None, pin=None):
    v = {"features": {"identityLifecycle": True}, "componentValues": {},
         "components": [dict(CONTROL_PIN), dict(pin or IL_PIN)], "gwTLS": False, "gwCertRef": {},
         "namespaces": {"krateo": "krateo-system"}}
    if enabled is not None:
        v["componentValues"] = {"identity-lifecycle":
                                {"accessRequest": {"intake": {"enabled": enabled}}}}
    return v


def find(rs, path):
    return [r for r in (rs or []) if path in json.dumps(r)]


def main():
    fails = []

    def check(label, ok, detail=""):
        print("  %-4s %s%s" % ("ok" if ok else "FAIL", label, ("  -- " + detail) if detail and not ok else ""))
        if not ok:
            fails.append(label)

    # ---- THE POSITIVE. Without this every negative below passes vacuously. ----
    rs, err = routes(base(enabled=True))
    if rs is None:
        print("::error::check-intake-route: the probe did not render: %s" % err)
        return 2
    hit = find(rs, "/request-access/api")
    check("intake.enabled TRUE renders exactly one /request-access/api route",
          len(hit) == 1, "got %d" % len(hit))
    if len(hit) == 1:
        r = hit[0]
        blob = json.dumps(r)
        check("  backend is the EXACT Service identity-lifecycle-intake",
              "identity-lifecycle-intake" in blob and "identity-lifecycle-intake-metrics" not in blob,
              blob[:160])
        port = ((r.get("backend") or {}).get("port")) if isinstance(r, dict) else None
        check("  backend port is 80", port == 80, "port=%r" % (port,))
        # READ THE FIELD, DO NOT GREP THE BLOB. My first version searched the serialised route for
        # the string "rewrite" -- which is absent whether rewriting is off OR the key is simply not
        # emitted, so it could not tell the two apart and failed on a correct route.
        rw = r.get("rewrite") if isinstance(r, dict) else None
        check("  rewrite is off (field absent or falsy, never a prefix strip)",
              not rw, "rewrite=%r" % (rw,))

    # ---- THE CONTROL that stops the negatives passing vacuously. ----
    check("the CONTROL route (frontend /) is present alongside the intake",
          len(find(rs, '"/"')) >= 1 or any((x.get("name") or "").startswith("frontend")
                                           for x in rs if isinstance(x, dict)),
          "routes: %s" % [x.get("name") for x in rs if isinstance(x, dict)])

    # ---- THE NEGATIVES. ----
    for label, val in (("ABSENT", None), ("FALSE", False)):
        rs2, err2 = routes(base(enabled=val))
        if rs2 is None:
            check("intake.enabled %s renders" % label, False, err2)
            continue
        check("intake.enabled %s renders NO /request-access route" % label,
              len(find(rs2, "/request-access")) == 0,
              "got %d" % len(find(rs2, "/request-access")))
        check("intake.enabled %s still renders the other routes" % label,
              len(rs2) == len(rs) - 1 if rs else False,
              "expected %d, got %d" % (len(rs) - 1, len(rs2)))

    # ---- THE UNKNOWN-DERIVATION REFUSAL, which must stay loud. ----
    bad = dict(IL_PIN)
    bad["gatewayPathsFrom"] = "notADerivation"
    rs3, err3 = routes(base(enabled=True, pin=bad))
    check("an unknown derivation FAILS the render rather than rendering nothing",
          rs3 is None and "not a derivation" in (err3 or ""), (err3 or "rendered anyway")[:120])

    print()
    if fails:
        print("::error::check-intake-route: %d check(s) failed: %s" % (len(fails), fails[:4]))
        return 1
    print("check-intake-route: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
