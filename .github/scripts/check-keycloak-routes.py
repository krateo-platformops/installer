#!/usr/bin/env python3
"""Exercises inst.platformRoutes for KEYCLOAK in path mode — companion to check-platform-routes.py.

WHY THIS EXISTS. Up to 0.3.667 the keycloak pin carries no gatewayPath, so inst.platformRoutes skips
it and, on a path-mode cluster (057: exposure.type Gateway, gatewayMode path), every
https://<portal>/keycloak/... request falls through to frontend's `/` and is answered by the SPA
(200 text/html, measured on krateo-057). Enabling features.keycloak there would give a Keycloak whose
issuer https://<portal>/keycloak/realms/krateo no browser or IDE can reach: every OIDC login fails
while every composition reports Ready.

THE APPROVED SHAPE. Keycloak runs with http.relativePath /keycloak and is exposed through TWO
prefixes only, to keycloak-service:8080, NOT rewritten (Keycloak itself expects the /keycloak prefix):
    /keycloak/realms/krateo/   discovery, authorization, token, certs, the sign-in pages
    /keycloak/resources/       the sign-in pages' static assets
/keycloak/admin is deliberately NOT exposed, and neither is any other realm: Diego decided
2026-11-16 that the master (admin) realm's sign-in and token endpoints must not be internet-reachable,
so the route is the krateo realm's prefix, not /keycloak/realms/. A single /keycloak prefix would satisfy the two positive
assertions while publishing the admin console, which is why the admin negatives are HARD failures.

WHAT IT PROVES, against the RENDERED routes and the REAL pins (chart/files/component-pins.yaml), with
a Gateway API PathPrefix matcher that resolves each request to ONE route by longest-prefix precedence:
  1. the matcher itself, before it judges anything;
  2. control: with features.keycloak OFF the four portal routes render, and /keycloak/... resolves to
     frontend (this is the bug, so the check can tell a fixed render from a broken one);
  3. with features.keycloak ON: discovery and resources resolve to a keycloak route, unrewritten,
     backend port 8080, auth public;
  4. HARD NEGATIVES: /keycloak/admin/, the admin console and admin REST paths, the MASTER realm's
     discovery/token/auth endpoints, and bare /keycloak resolve to NO keycloak route;
  5. /auth?kind=oidc stays on frontend and /authn/... on authn;
  6. the four portal routes are byte-identical with keycloak on and off, and identical to 0.3.667's;
  7. under path-mode TLS the keycloak routes land on platform-https:443 like the rest of the portal.

ROUTE SHAPE. It accepts either a route entry per prefix (`pathPrefix: <str>`) or one entry carrying
`pathPrefixes: [..]` — the helper's output shape is the helper owner's choice. Whatever it is, the
agentgateway-policies chart must accept it (its platformRoutes schema, and HTTPRoute name uniqueness
if two entries share a name); this check does not and cannot prove that part.

WHAT IT CANNOT PROVE. `helm template` disables lookup, so inst.svcname returns "" and the backend NAME
falls back to the pin name ("keycloak"), not the live Service ("keycloak-service"). The name is
therefore REPORTED, not asserted; it is verified separately against a live cluster.

PROVENANCE. Written by the keycloak-provider-kog session, which owns the approved routing shape, and
adopted here with their structure intact: the matcher self-test, the longest-prefix resolver, the
real-pins loader, the control/positive/negative split and the 0.3.667 byte-identity literal are all
theirs. I read all 320 lines before adopting any of it.

WHAT I CHANGED, and why each change was forced by the implementation rather than preferred:
  * stack() now supplies componentValues.keycloak.keycloak.http.relativePath and
    componentValues.krateo-keycloak-realm.realm.name. Their version passed componentValues: {} and
    the probe FAILED TO RENDER against my branch -- because the prefixes are DERIVED from those two
    values (gatewayPathsFrom: keycloakRealm) rather than written literally in the pin. Having to
    supply them is the point: it is what makes the route, baseUrl and tokenEndpoint agree by
    construction instead of by review.
  * A RENAMED-REALM case. realm `acme` must route /keycloak/realms/acme/ and must NOT route
    /keycloak/realms/krateo/. Without it the derivation could be hard-coding `krateo` and every other
    assertion would still pass -- which is exactly the coupling this change exists to remove.
  * A GUARD case: with relativePath empty the render must FAIL with its own sentence. I wrote that
    assertion in chart/tests/keycloak_path_routes_test.yaml first and it reported "rendered manifest
    is empty": the keycloak pin carries deps [keycloak-operator, cloudnative-pg] as well as
    requiresApiGroups, so helm-unittest never reached the derivation and the assertion was judging a
    code path it never entered. It is reachable HERE, because this probe calls inst.platformRoutes
    with synthetic pins and no dependency gates.
  * A MOVED-PREFIX case. relativePath /auth-kc must move BOTH prefixes, proving the prefix half of
    the derivation is live too and not just the realm half.

Usage: check-keycloak-routes.py [CHART_DIR]   (default: ../../chart relative to this file)
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required by check-keycloak-routes.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "chart")

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  routes: |
{{ (include "inst.platformRoutes" (list $ .Values.components 8080 .Values.gwTLS .Values.gwCertRef)) | indent 4 }}
"""

PORTAL = ["authn", "snowplow", "frontend", "krateo-sse-proxy"]


REALM = "krateo"
POSITIVE = [
    "/keycloak/realms/%s/.well-known/openid-configuration" % REALM,
    "/keycloak/realms/%s/protocol/openid-connect/auth?client_id=krateo-ide&code_challenge=x" % REALM,
    "/keycloak/realms/%s/protocol/openid-connect/token" % REALM,
    "/keycloak/realms/%s/protocol/openid-connect/certs" % REALM,
    "/keycloak/resources/x",
]
NEGATIVE = [
    "/keycloak",
    "/keycloak/",
    "/keycloak/admin",
    "/keycloak/admin/",
    "/keycloak/admin/master/console/",
    "/keycloak/admin/realms/%s/users" % REALM,
    "/keycloak/realmsx/%s" % REALM,
    "/keycloak/realms/",
    "/keycloak/realms/master/.well-known/openid-configuration",
    "/keycloak/realms/master/protocol/openid-connect/token",
    "/keycloak/realms/master/protocol/openid-connect/auth?client_id=security-admin-console",
    "/keycloak/realms/%sx/protocol/openid-connect/token" % REALM,
]
CALLBACK = "/auth?kind=oidc&code=abc&state=xyz"


def norm(prefix):
    return prefix.rstrip("/") or "/"


def path_matches_prefix(path, prefix):
    """Gateway API PathPrefix, element-wise; the query string is not part of the path."""
    path = path.split("?", 1)[0]
    prefix = norm(prefix)
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix + "/")


def expand(routes):
    """(name, prefix, route) for every prefix a route entry carries, whichever shape it uses."""
    out = []
    for r in routes:
        ps = r.get("pathPrefixes")
        if ps is None:
            ps = [r.get("pathPrefix")]
        for p in ps:
            out.append((r.get("name"), p, r))
    return out


def resolve(path, routes):
    """The ONE route that serves `path`: Gateway API precedence among matching PathPrefix rules is
    the longest prefix. Returns (name, prefix, route) or None."""
    hits = [(len(norm(p)), n, p, r) for n, p, r in expand(routes) if p and path_matches_prefix(path, p)]
    if not hits:
        return None
    hits.sort(key=lambda h: h[0], reverse=True)
    if len(hits) > 1 and hits[0][0] == hits[1][0] and hits[0][1] != hits[1][1]:
        raise AssertionError("ambiguous: %r is matched at equal length by %s and %s"
                             % (path, hits[0][1], hits[1][1]))
    _, n, p, r = hits[0]
    return n, p, r


def load_pins():
    for fn in ("component-pins.yaml", "component-pins.min.yaml"):
        f = os.path.join(CHART, "files", fn)
        if os.path.exists(f):
            d = yaml.safe_load(open(f))
            items = d if isinstance(d, list) else d.get("components")
            if items is None:
                items = [v for v in d.values() if isinstance(v, list)][0]
            return {c["name"]: c for c in items}, fn
    sys.exit("::error::no files/component-pins*.yaml under %s" % CHART)


def probe_dir():
    td = tempfile.mkdtemp(prefix="keycloakroutes-")
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


_D = {"d": None}


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
        return None, "no probe ConfigMap in the render"


def stack(pins, keycloak_on, tls=False, relpath="/keycloak", realm=REALM):
    """`relpath` and `realm` are what the prefixes are DERIVED from. Passing them is not test
    scaffolding: the pin carries no literal paths, so a caller that omits them gets a render failure
    with the guard's own sentence -- which is case 8 below."""
    cv = {}
    if relpath is not None:
        cv["keycloak"] = {"keycloak": {"http": {"relativePath": relpath}}}
    if realm is not None:
        cv["krateo-keycloak-realm"] = {"realm": {"name": realm}}
    return {
        "features": {"portal": True, "keycloak": keycloak_on},
        "componentValues": cv,
        "namespaces": {"krateo": "krateo-system"},
        "gwTLS": tls,
        "gwCertRef": {"name": "krateo-portal-tls"} if tls else {},
        "components": [pins[n] for n in PORTAL + ["keycloak"]],
    }


def _expected():
    l = {"name": "platform-http", "port": 8080}
    def r(name, prefix, rewrite, svc, port, auth):
        return {"name": name, "listener": dict(l), "pathPrefix": prefix, "rewrite": rewrite,
                "backend": {"name": svc, "port": port}, "auth": {"mode": auth}}
    return {
        "authn": r("authn", "/authn", True, "authn", 8082, "public"),
        "snowplow": r("snowplow", "/content", True, "snowplow", 8081, "optional"),
        "frontend": r("frontend", "/", False, "frontend", 8080, "public"),
        "krateo-sse-proxy": r("krateo-sse-proxy", "/events", True, "krateo-sse-proxy", 8080, "optional"),
    }


def main() -> int:
    ok = True

    def check(good, msg):
        nonlocal ok
        print("%s  %s" % ("ok  " if good else "FAIL", msg))
        if not good:
            ok = False

    # ---- 1. the matcher and the resolver, before they judge anything ----
    cases = [("/keycloak/realms/krateo", "/keycloak/realms/krateo/", True),
             ("/keycloak/realms/master/x", "/keycloak/realms/krateo/", False),
             ("/keycloak/realms/krateox/x", "/keycloak/realms/krateo/", False),
             ("/keycloak/realms/krateo", "/keycloak/realms/", True),
             ("/keycloak/realms", "/keycloak/realms/", True),
             ("/keycloak/realmsx", "/keycloak/realms/", False),
             ("/keycloak/admin/", "/keycloak/realms/", False),
             ("/keycloak/admin/", "/keycloak", True),
             ("/keycloak/resources/a/b.css", "/keycloak/resources/", True),
             ("/auth?kind=oidc", "/authn", False), ("/auth?kind=oidc", "/", True)]
    bad = [(p, x, w) for p, x, w in cases if path_matches_prefix(p, x) != w]
    if bad:
        print("::error::the PathPrefix matcher is wrong on %s, so every verdict below is meaningless" % bad)
        return 1
    synth = [{"name": "frontend", "pathPrefix": "/"}, {"name": "kc", "pathPrefix": "/keycloak/realms/"}]
    if resolve("/keycloak/realms/krateo", synth)[0] != "kc" or resolve("/keycloak/admin/", synth)[0] != "frontend":
        print("::error::longest-prefix resolution is wrong, so every verdict below is meaningless")
        return 1
    print("ok    matcher: %d PathPrefix cases; resolver: longest prefix wins over frontend's `/`" % len(cases))

    pins, pinfile = load_pins()
    missing = [n for n in PORTAL + ["keycloak"] if n not in pins]
    if missing:
        print("::error::pins missing from %s: %s" % (pinfile, missing))
        return 1
    kp = pins["keycloak"]
    print("      keycloak pin (%s): %s" % (pinfile, {k: kp.get(k) for k in sorted(kp) if k.startswith("gateway")} or "no gateway* keys"))

    # ---- 2. control: keycloak OFF -> /keycloak/... is the SPA (the bug's shape) ----
    off, err = routes(stack(pins, False))
    if off is None:
        print("::error::the probe failed to render (keycloak off): %s" % err)
        return 1
    names_off = sorted({n for n, _, _ in expand(off)})
    check(names_off == sorted(PORTAL), "control, keycloak OFF: routes = %s (want the four portal routes)" % names_off)
    rv = resolve(POSITIVE[0], off)
    check(rv is not None and rv[0] == "frontend",
          "control, keycloak OFF: %s resolves to %s (want frontend)" % (POSITIVE[0], rv and rv[0]))

    # ---- 3. keycloak ON: the two exposed prefixes reach keycloak, unrewritten ----
    on, err = routes(stack(pins, True))
    if on is None:
        print("::error::the probe failed to render (keycloak on): %s" % err)
        return 1
    kc_names = sorted({n for n, _, r in expand(on) if n not in PORTAL})
    check(bool(kc_names), "keycloak ON: non-portal routes rendered = %s (want at least one)" % kc_names)
    for path in POSITIVE:
        rv = resolve(path, on)
        if rv is None or rv[0] in PORTAL:
            check(False, "keycloak ON: %s resolves to %s (want a keycloak route)" % (path, rv and rv[0]))
            continue
        n, p, r = rv
        be = r.get("backend") or {}
        good = r.get("rewrite") is False and int(be.get("port", 0)) == 8080 and (r.get("auth") or {}).get("mode") == "public"
        check(good, "keycloak ON: %s -> route %s prefix %r rewrite=%s port=%s auth=%s"
              % (path, n, p, r.get("rewrite"), be.get("port"), (r.get("auth") or {}).get("mode")))
        print("      backend name %r (REPORTED, not asserted: lookup is disabled under helm template)" % be.get("name"))

    # ---- 4. HARD NEGATIVES: the admin console and bare /keycloak are NOT routed to keycloak ----
    for path in NEGATIVE:
        rv = resolve(path, on)
        check(rv is not None and rv[0] == "frontend",
              "HARD NEGATIVE: %s resolves to %s (want frontend, i.e. no keycloak route)" % (path, rv and rv[0]))

    # ---- 5. the portal's own paths are untouched ----
    rv = resolve(CALLBACK, on)
    check(rv is not None and rv[0] == "frontend", "%s resolves to %s (want frontend)" % (CALLBACK, rv and rv[0]))
    rv = resolve("/authn/strategies", on)
    check(rv is not None and rv[0] == "authn", "/authn/strategies resolves to %s (want authn)" % (rv and rv[0]))

    # ---- 6. the four portal routes: identical with keycloak on/off, and identical to 0.3.667 ----
    def by_name(rs):
        return {r["name"]: r for r in rs if r.get("name") in PORTAL}
    b_off, b_on, exp = by_name(off), by_name(on), _expected()
    diff = sorted(n for n in PORTAL if b_off.get(n) != b_on.get(n))
    check(diff == [], "portal routes unchanged by features.keycloak: differing = %s (want [])" % diff)
    diff = sorted(n for n in PORTAL if b_on.get(n) != exp[n])
    check(diff == [], "portal routes byte-identical to 0.3.667: differing = %s (want [])" % diff)
    for n in diff:
        print("      %s\n        got  %s\n        want %s" % (n, b_on.get(n), exp[n]))

    # ---- 7. path-mode TLS moves the keycloak routes to platform-https:443 with the portal ----
    tr, err = routes(stack(pins, True, tls=True))
    if tr is None:
        print("::error::the TLS render failed: %s" % err)
        return 1
    rv = resolve(POSITIVE[0], tr)
    lst = (rv[2].get("listener") if rv else None) or {}
    check(rv is not None and rv[0] not in PORTAL and lst.get("name") == "platform-https" and int(lst.get("port", 0)) == 443
          and (lst.get("tls") or {}).get("certificateRef") == {"name": "krateo-portal-tls"},
          "under TLS, %s -> %s on %s:%s cert %s" % (POSITIVE[0], rv and rv[0], lst.get("name"), lst.get("port"),
                                                   (lst.get("tls") or {}).get("certificateRef")))

    # ---- 8. THE DERIVATION IS LIVE: rename the realm, move the prefix, and omit the prefix ----
    # Without these three, the derivation could be returning the literal /keycloak/realms/krateo/ and
    # every assertion above would still pass. This is the half that proves it is derived at all.
    ar, err = routes(stack(pins, True, realm="acme"))
    if ar is None:
        check(False, "renamed realm: the probe failed to render: %s" % err)
    else:
        rv = resolve("/keycloak/realms/acme/.well-known/openid-configuration", ar)
        check(rv is not None and rv[0] not in PORTAL,
              "renamed realm acme: /keycloak/realms/acme/... -> %s (want a keycloak route)" % (rv and rv[0]))
        rv = resolve("/keycloak/realms/krateo/.well-known/openid-configuration", ar)
        check(rv is not None and rv[0] == "frontend",
              "renamed realm acme: the OLD /keycloak/realms/krateo/... -> %s (want frontend: the "
              "prefix must MOVE, not accumulate)" % (rv and rv[0]))

    mr, err = routes(stack(pins, True, relpath="/auth-kc"))
    if mr is None:
        check(False, "moved prefix: the probe failed to render: %s" % err)
    else:
        rv = resolve("/auth-kc/realms/%s/protocol/openid-connect/token" % REALM, mr)
        check(rv is not None and rv[0] not in PORTAL,
              "moved prefix /auth-kc: /auth-kc/realms/.../token -> %s (want a keycloak route)" % (rv and rv[0]))
        rv = resolve("/auth-kc/resources/x", mr)
        check(rv is not None and rv[0] not in PORTAL,
              "moved prefix /auth-kc: /auth-kc/resources/x -> %s (want a keycloak route)" % (rv and rv[0]))
        rv = resolve("/keycloak/realms/%s/protocol/openid-connect/token" % REALM, mr)
        check(rv is not None and rv[0] == "frontend",
              "moved prefix /auth-kc: the OLD /keycloak/... -> %s (want frontend)" % (rv and rv[0]))

    gr, gerr = routes(stack(pins, True, relpath=None))
    check(gr is None and gerr is not None and "relativePath" in gerr,
          "THE GUARD: relativePath unset -> the render FAILS naming relativePath (got: %s)"
          % ("a render" if gr is not None else (gerr or "")[:90]))

    if not ok:
        print("::error::check-keycloak-routes: Keycloak's path-mode exposure is NOT proven by this render")
        return 1
    print("\ncheck-keycloak-routes: OK — /keycloak/realms/%s/ and /keycloak/resources/ reach Keycloak "
          "unrewritten; the admin console and the master realm are not routed; the portal routes are "
          "unchanged" % REALM)
    return 0


if __name__ == "__main__":
    sys.exit(main())
