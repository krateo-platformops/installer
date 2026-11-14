#!/usr/bin/env python3
"""Exercises inst.platformRoutes — the portal stack's path-mode routing.

WHY THIS EXISTS. The derivation was INLINE in compositions.yaml, whose head LISTs every CRD with
`lookup`. `helm template` DISABLES lookup, so that file renders ZERO composition objects and nothing
in it could be exercised by any render — the same situation that made inst.subAgents untestable, and
extracting THAT immediately exposed a live bug (agentName ignored for non-orchestrators).

THE CLAIM IT EXISTS TO PROVE. /auth in path mode routes to authn with the prefix rewritten away, so
the frontend's OIDC callback — the SPA route /auth?kind=oidc — reaches authn as /?kind=oidc, matches
none of its eleven exact-match routes, and 404s with the authorization code lost. Diego approved
moving authn's gatewayPath to /authn on condition the render is PROVEN rather than read.

frontend asked for the sharper assertion, and it is the one that matters: that **/auth EXACT, WITH A
QUERY, is matched by NO authn route** after the change. "/authn exists" is the weaker test — it
would pass while leaving /auth still claimed by authn.

So this file carries a Gateway API PathPrefix matcher and evaluates the real question against the
RENDERED routes, in both directions: the bug present at /auth, and absent at /authn.
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required by check-platform-routes.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "chart")

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  routes: |
{{ (include "inst.platformRoutes" (list $ .Values.components 8080 .Values.gwTLS .Values.gwCertRef)) | indent 4 }}
"""


def path_matches_prefix(path, prefix):
    """Gateway API PathPrefix semantics, element-wise.

    A prefix matches when the path equals it, or begins with it followed by `/`. `/authn` therefore
    does NOT match `/auth`, which is the whole mechanism of the fix; a naive startswith() WOULD
    match and would make this test pass for the wrong reason.
    The QUERY STRING is not part of path matching, so it is stripped first -- stated explicitly
    because the bug is always written as `/auth?kind=oidc` and the query is what carries the code.
    """
    path = path.split("?", 1)[0]
    prefix = prefix.rstrip("/") or "/"
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix + "/")


def probe_dir():
    td = tempfile.mkdtemp(prefix="platformroutes-")
    p = os.path.join(td, "probe")
    os.makedirs(os.path.join(p, "templates"))
    with open(os.path.join(p, "Chart.yaml"), "w") as f:
        f.write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    with open(os.path.join(p, "values.yaml"), "w") as f:
        # namespaces.krateo is REQUIRED, not decoration: inst.svcname dereferences it and the
        # probe failed with "nil pointer evaluating interface {}.krateo" without it. Recorded
        # because the next person adding a probe for a helper that calls inst.svcname will hit it.
        f.write("features: {}\ncomponentValues: {}\ncomponents: []\ngwTLS: false\n"
                "gwCertRef: {}\nnamespaces: {krateo: krateo-system}\n")
    shutil.copy(os.path.join(CHART, "templates", "_helpers.tpl"),
                os.path.join(p, "templates", "_helpers.tpl"))
    with open(os.path.join(p, "templates", "pr.yaml"), "w") as f:
        f.write(PROBE)
    return p


_D = {"d": None}


def routes(values):
    """The derived routes, or (None, error)."""
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
                raw = (d.get("data") or {}).get("routes") or ""
                parsed = yaml.safe_load(raw) or {}
                return (parsed.get("routes") or []), None
        return None, "no probe ConfigMap in the render"


def stack(authn_path):
    """The 057 portal stack, with authn's prefix as the variable under test."""
    return {
        "features": {"portal": True},
        "componentValues": {},
        "namespaces": {"krateo": "krateo-system"},
        "gwTLS": False,
        "gwCertRef": {},
        "components": [
            {"name": "frontend", "svcMatch": "frontend", "exposePort": 8080,
             "gatewayPath": "/", "gatewayRewrite": False, "gatewayAuth": "public",
             "feature": "portal"},
            {"name": "authn", "svcMatch": "authn", "exposePort": 8082,
             "gatewayPath": authn_path, "gatewayRewrite": True, "gatewayAuth": "public",
             "feature": "portal"},
            {"name": "snowplow", "svcMatch": "snowplow", "exposePort": 8081,
             "gatewayPath": "/snowplow", "gatewayRewrite": True, "gatewayAuth": "public",
             "feature": "portal"},
            {"name": "portal", "svcMatch": "portal", "exposePort": 8080, "feature": "portal"},
        ],
    }


CALLBACK = "/auth?kind=oidc&code=abc&state=xyz"


def main() -> int:
    ok = True

    # ---- the matcher itself, before it is trusted to judge anything ----
    cases = [("/auth", "/auth", True), ("/auth", "/authn", False), ("/auth", "/", True),
             ("/authn", "/authn", True), ("/authn/x", "/authn", True), ("/auth", "/snowplow", False),
             (CALLBACK, "/auth", True), (CALLBACK, "/authn", False)]
    for path, prefix, want in cases:
        got = path_matches_prefix(path, prefix)
        if got != want:
            print("FAIL  matcher: %r vs prefix %r -> %s, want %s" % (path, prefix, got, want))
            ok = False
    if not ok:
        print("::error::the PathPrefix matcher is wrong, so every verdict below is meaningless")
        return 1
    print("ok    matcher: %d Gateway API PathPrefix cases, including the callback with its query"
          % len(cases))

    # ---- control: the probe derives routes at all ----
    base, err = routes(stack("/auth"))
    if base is None:
        print("::error::the probe failed to render: %s" % err)
        return 1
    if len(base) != 3:
        print("::error::CONTROL FAILED -- expected 3 routes (frontend, authn, snowplow; portal has "
              "no gatewayPath), got %d: %s" % (len(base), [r.get("name") for r in base]))
        return 1
    print("ok    control: 3 routes derived; `portal` has no gatewayPath and contributes none")

    def authn_route(rs):
        hit = [r for r in rs if r.get("name") == "authn"]
        assert len(hit) == 1, "expected exactly one authn route, got %d" % len(hit)
        return hit[0]

    # ---- THE BUG, proven PRESENT at /auth ----
    a = authn_route(base)
    bug = path_matches_prefix(CALLBACK, a["pathPrefix"])
    good = a["pathPrefix"] == "/auth" and bug and a.get("rewrite") is True
    print("%s  BUG PRESENT at /auth: authn prefix %r matches the callback %r -> %s (rewrite=%s)"
          % ("ok  " if good else "FAIL", a["pathPrefix"], CALLBACK, bug, a.get("rewrite")))
    if not good:
        ok = False

    # ---- THE FIX, proven: /auth matched by NO authn route ----
    moved, err = routes(stack("/authn"))
    if moved is None:
        print("::error::the /authn render failed: %s" % err)
        return 1
    m = authn_route(moved)
    still = path_matches_prefix(CALLBACK, m["pathPrefix"])
    good = m["pathPrefix"] == "/authn" and not still
    print("%s  FIX at /authn: authn prefix %r matches the callback -> %s (want False)"
          % ("ok  " if good else "FAIL", m["pathPrefix"], still))
    if not good:
        ok = False
    # frontend's assertion in its strongest form: NO authn route claims the callback path
    claimers = [r.get("name") for r in moved if path_matches_prefix(CALLBACK, r["pathPrefix"])]
    good = "authn" not in claimers
    print("%s  frontend's assertion: routes matching %r = %s -- authn absent: %s"
          % ("ok  " if good else "FAIL", CALLBACK, claimers, good))
    if not good:
        ok = False
    # and the fall-through: the frontend DOES claim it, so the SPA serves the callback
    good = "frontend" in claimers
    print("%s  fall-through: the frontend's `/` prefix claims the callback: %s"
          % ("ok  " if good else "FAIL", good))
    if not good:
        ok = False

    # ---- every other route is untouched by the move ----
    def by_name(rs):
        return {r["name"]: r for r in rs}
    b, mv = by_name(base), by_name(moved)
    diff = sorted(n for n in set(b) | set(mv) if b.get(n) != mv.get(n))
    good = diff == ["authn"]
    print("%s  only the authn route changes: differing routes = %s (want ['authn'])"
          % ("ok  " if good else "FAIL", diff))
    if not good:
        ok = False

    # ---- TLS flips the shared listener to platform-https:443, and that is independent ----
    tls = stack("/authn")
    tls["gwTLS"] = True
    tls["gwCertRef"] = {"name": "portal-tls"}
    tr, err = routes(tls)
    if tr is None:
        print("::error::the TLS render failed: %s" % err)
        return 1
    la = authn_route(tr)["listener"]
    good = la.get("name") == "platform-https" and int(la.get("port")) == 443
    print("%s  under TLS the shared listener becomes platform-https:443 -> %s:%s"
          % ("ok  " if good else "FAIL", la.get("name"), la.get("port")))
    if not good:
        ok = False

    if not ok:
        print("::error::check-platform-routes: the routing property Diego approved A on is NOT "
              "proven by this render")
        return 1
    print("\ncheck-platform-routes: OK — the /auth -> /authn move is proven by render, including "
          "frontend's negative assertion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
