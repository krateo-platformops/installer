#!/usr/bin/env python3
"""Print an RBAC subject's grants COMPLETELY, including the fields that are easy to omit.

WHY THIS EXISTS. On 2026-11-18 I dumped a peer's ClusterRole during a narrow read-only window and
reported one of its rules as malformed:

    apiGroups=None    resources=None    verbs=['get']

It was not malformed. It was a perfectly ordinary `nonResourceURLs` rule granting discovery on
/api, /api/*, /apis, /apis/* and /version -- and my dumper printed only `apiGroups` and `resources`,
so a rule that has NEITHER rendered as two Nones. A FIELD THE READER DOES NOT KNOW ABOUT READS AS
ABSENT, and I published that absence as a defect in someone else's object.

So this prints every field a PolicyRule can carry, and says so when a rule has no resource-shaped
field at all rather than showing empty columns.
"""
import argparse
import json
import subprocess
import sys

# Every field in rbac/v1 PolicyRule. If Kubernetes adds one, UNKNOWN_FIELDS below will surface it
# rather than silently dropping it -- which is the whole point of this file.
RULE_FIELDS = ("apiGroups", "resources", "resourceNames", "verbs", "nonResourceURLs")


def kget(ctx, kind, name, ns=None):
    cmd = ["kubectl", "--context", ctx, "--request-timeout=30s", "get", kind, name, "-o", "json"]
    if ns:
        cmd += ["-n", ns]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200]
    return json.loads(r.stdout), None


def show_rule(r, i):
    unknown = sorted(set(r) - set(RULE_FIELDS))
    kinds = [f for f in ("resources", "nonResourceURLs") if r.get(f)]
    if not kinds:
        # A rule with neither is genuinely inert -- say that, do not print blank columns.
        print("    rule %d: NO resources and NO nonResourceURLs -- this rule matches nothing."
              " verbs=%s" % (i, r.get("verbs")))
    for f in RULE_FIELDS:
        if r.get(f):
            print("    rule %d  %-16s %s" % (i, f, r[f]))
    if unknown:
        print("    rule %d  !! FIELDS THIS SCRIPT DOES NOT KNOW: %s -- values: %s"
              % (i, unknown, {k: r[k] for k in unknown}))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--role", action="append", default=[],
                    help="clusterrole name, or ns/name for a namespaced Role; repeatable")
    ap.add_argument("--binding", action="append", default=[],
                    help="clusterrolebinding name, or ns/name for a RoleBinding; repeatable")
    a = ap.parse_args(argv)

    if not a.role and not a.binding:
        print("dump-grants: pass at least one --role or --binding")
        return 2

    missing = 0
    for spec in a.role:
        ns, _, name = spec.rpartition("/")
        kind = "role" if ns else "clusterrole"
        o, err = kget(a.context, kind, name, ns or None)
        print()
        print("  === %s %s ===" % (kind, spec))
        if o is None:
            print("    NOT READ: %s" % err)
            missing += 1
            continue
        rules = o.get("rules") or []
        print("    %d rule(s)" % len(rules))
        for i, r in enumerate(rules, 1):
            show_rule(r, i)

    for spec in a.binding:
        ns, _, name = spec.rpartition("/")
        kind = "rolebinding" if ns else "clusterrolebinding"
        o, err = kget(a.context, kind, name, ns or None)
        print()
        print("  === %s %s ===" % (kind, spec))
        if o is None:
            print("    NOT READ: %s" % err)
            missing += 1
            continue
        rr = o.get("roleRef") or {}
        print("    roleRef %s/%s" % (rr.get("kind"), rr.get("name")))
        for s in (o.get("subjects") or []):
            print("    subject %s %s%s"
                  % (s.get("kind"), ("%s/" % s["namespace"]) if s.get("namespace") else "",
                     s.get("name")))

    if missing:
        print()
        print("::error::dump-grants: %d object(s) could not be read -- this dump is INCOMPLETE and "
              "must not be read as the subject's full grant set." % missing)
        return 1
    return 0


def _selftest():
    """Exercise the rule printer on the shapes that caused the original error."""
    cases = [
        ("nonResourceURLs only (the one I called malformed)",
         {"nonResourceURLs": ["/api", "/api/*", "/apis", "/apis/*", "/version"], "verbs": ["get"]}),
        ("resources only", {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"]}),
        ("resourceNames-scoped",
         {"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["a", "b"],
          "verbs": ["get"]}),
        ("genuinely inert: neither", {"verbs": ["get"]}),
        ("a field from the future", {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"],
                                     "someNewField": ["x"]}),
    ]
    import io
    import contextlib
    fails = 0
    for label, rule in cases:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            show_rule(rule, 1)
        out = buf.getvalue()
        if "nonResourceURLs" in rule:
            ok = "nonResourceURLs" in out and "matches nothing" not in out
        elif not rule.get("resources") and not rule.get("nonResourceURLs"):
            ok = "matches nothing" in out
        elif "someNewField" in rule:
            ok = "FIELDS THIS SCRIPT DOES NOT KNOW" in out and "someNewField" in out
        else:
            ok = "resources" in out and "matches nothing" not in out
        fails += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
    print()
    print("  %d case(s), %d failure(s)" % (len(cases), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    sys.exit(main(sys.argv[1:]))
