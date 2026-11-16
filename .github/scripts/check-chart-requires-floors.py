#!/usr/bin/env python3
"""Enforce each pinned chart's `krateo.io/requires-<component>` version floors.

WHY THIS EXISTS, AND IT IS A MEASURED FAILURE RATHER THAN A HYPOTHETICAL.

krateo-keycloak-realm 0.5.3 raises `krateo.io/requires-keycloak-config-kog` to `>= 0.9.2`, because
0.9.2 is the release whose KeycloakRealm CRD declares `ssoSessionIdleTimeoutRememberMe` and
`ssoSessionMaxLifespanRememberMe`. keycloak-provider-kog MEASURED what happens when the pair is
split -- realm 0.5.3 against 0.9.1's CRD, on a disposable GKE cluster:

    the stored CR KEEPS `rememberMe: true` and LOSES both timeouts, and stays Ready

That is a silent prune. The CRD does not declare the fields, so the apiserver drops them and reports
success; `kubectl` prints the object as accepted. Nothing in the composition's conditions says a
value went missing, because from the apiserver's point of view nothing did.

SO THE ANNOTATION WAS THE ONLY CONTROL, AND NOTHING READ IT. `check-same-version-line` keeps the
members of ONE line in step; this is a floor BETWEEN TWO DIFFERENT LINES, and no gate read it. "Pin
the pair or neither" was a sentence in a chat message -- which is exactly the thing
retracted-versions.yaml's own header warns about: prose is not a control, because nothing evaluates
it and the only thing between a cluster and a bad combination is a human remembering at pin time.

DISTINCT FROM check-hard-dep-requires-gate.py. That one is about `requiresApiGroups` -- whether a
chart that CREATES objects in an API group declares the group it needs served. Its unit is an API
group precisely because a component-keyed requirement suppressed working compositions on
krateo-demo. This gate's unit is a COMPONENT VERSION, which is a different question: not "can the
kind be rendered" but "does the pinned peer declare the fields this chart writes".

FAILS CLOSED ON ANYTHING IT CANNOT EVALUATE. An unparseable constraint, an annotation naming a
component that is not pinned, or a chart whose annotations cannot be read are all REFUSALS, not
passes. A floor gate that cannot read a floor has not checked it, and reporting that as green is
how the measured prune above reaches a cluster with CI green.

SCOPED TO CHANGED PINS by default, because reading annotations means pulling charts. `--all` sweeps
every pin, which is what to run when adding a component or auditing. In a PR only the changed pins
can newly violate a floor -- but note the ASYMMETRY, which is why `--all` exists: bumping component
A can break a floor declared by an UNCHANGED chart B that requires A. So the changed-pin scan checks
both directions: floors declared BY a changed chart, and floors declared by any chart ABOUT a
changed component.
"""
import io
import json
import os
import re
import subprocess
import sys

import yaml

PINS = "chart/files/component-pins.yaml"
DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"
ANNOT_RE = re.compile(r"^krateo\.io/requires-(?P<comp>[a-z0-9][a-z0-9-]*)$")
# A term is an operator and a version, or a bare version (treated as ==). Anything else is a refusal.
TERM_RE = re.compile(r"^(?P<op>>=|<=|==|!=|>|<)?\s*(?P<ver>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")


def sh(*argv, timeout=300):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.returncode, p.stderr
    except subprocess.TimeoutExpired:
        return "", 124, "timeout"


def parse_version(v):
    """(major, minor, patch, is_prerelease, prerelease) -- enough for the floors in use.

    Pre-release ordering follows semver only to the extent that a pre-release sorts BEFORE its
    release. That is all that is needed here, and the chart conventions forbid pinning a
    pre-release anyway, so a finer comparison would be unexercised code."""
    core, _, rest = v.partition("-")
    core = core.partition("+")[0]
    parts = core.split(".")
    if len(parts) != 3:
        raise ValueError("not a three-part version: %r" % v)
    nums = tuple(int(x) for x in parts)
    pre = rest.partition("+")[0]
    return nums + (bool(pre), pre)


def cmp_version(a, b):
    av, bv = parse_version(a), parse_version(b)
    if av[:3] != bv[:3]:
        return -1 if av[:3] < bv[:3] else 1
    # same numeric core: a pre-release is LOWER than the release
    if av[3] != bv[3]:
        return -1 if av[3] else 1
    if av[4] == bv[4]:
        return 0
    return -1 if av[4] < bv[4] else 1


def satisfies(version, constraint):
    """True/False, or raise ValueError if the constraint cannot be evaluated.

    Raising rather than returning False is deliberate: an unevaluable constraint must become a
    REFUSAL with its text quoted, never a silent failure that reads as a violated floor."""
    terms = [t.strip() for t in constraint.split(",") if t.strip()]
    if not terms:
        raise ValueError("empty constraint")
    for t in terms:
        m = TERM_RE.match(t)
        if not m:
            raise ValueError("unparseable constraint term %r" % t)
        op = m.group("op") or "=="
        want = m.group("ver")
        c = cmp_version(version, want)
        ok = {">=": c >= 0, ">": c > 0, "<=": c <= 0, "<": c < 0,
              "==": c == 0, "!=": c != 0}[op]
        if not ok:
            return False
    return True


def chart_ref(pin):
    repo = (pin.get("repo") or DEFAULT_REPO).rstrip("/")
    return "%s/%s" % (repo, pin.get("chart") or pin["name"])


def chart_annotations(pin):
    """{annotation: value} from the pinned chart, or None if it could not be read."""
    out, rc, err = sh("helm", "show", "chart", chart_ref(pin), "--version", pin["version"])
    if rc != 0:
        return None, (err or "").strip().replace("\n", " ")[:200]
    try:
        meta = yaml.safe_load(out) or {}
    except yaml.YAMLError as e:
        return None, "unparseable Chart.yaml: %s" % e
    return (meta.get("annotations") or {}), None


def load_pins(path=PINS):
    d = yaml.safe_load(io.open(path, encoding="utf-8"))
    return {c["name"]: c for c in d["components"]}


def changed_pins(pins, base):
    """Names whose pinned VERSION differs from `base`'s, or None if base is unreadable."""
    out, rc, _ = sh("git", "show", "%s:%s" % (base, PINS))
    if rc != 0:
        return None
    try:
        old = {c["name"]: c["version"] for c in (yaml.safe_load(out) or {}).get("components", [])}
    except yaml.YAMLError:
        return None
    return {n for n, c in pins.items() if old.get(n) != c["version"]}


def evaluate(pins, scope, fetch=chart_annotations):
    """(lines, failures). `scope` is the set of component names whose charts to read.

    Checks BOTH directions for a changed component: floors it declares, and floors declared about
    it by any other pinned chart. The second direction is the one a naive changed-only scan misses
    -- bumping A can violate a floor that unchanged B declares about A."""
    lines, failures = [], []
    # which charts must be READ: the ones in scope, plus any chart that might declare a floor about
    # a component in scope. We cannot know who declares what without reading, so for the reverse
    # direction the caller passes the full set via --all. Document the limit rather than hide it.
    for name in sorted(scope):
        pin = pins.get(name)
        if pin is None:
            failures.append("%s: in scope but not pinned -- the scan is built from the pins file, "
                            "so this is a bug in the gate, not in the chart" % name)
            continue
        annots, err = fetch(pin)
        if annots is None:
            failures.append(
                "%s %s: COULD NOT READ the chart's annotations (%s). Not a pass: a floor gate that "
                "cannot read a floor has not checked it." % (name, pin["version"], err))
            continue
        found = 0
        for key, raw in sorted(annots.items()):
            m = ANNOT_RE.match(key)
            if not m:
                continue
            found += 1
            dep = m.group("comp")
            constraint = str(raw).strip()
            dep_pin = pins.get(dep)
            if dep_pin is None:
                failures.append(
                    "%s %s declares %s %r, and %r IS NOT PINNED in this chart. Either the "
                    "annotation names a component by a name this chart does not use, or a required "
                    "peer is missing. Both are refusals: an unpinned peer cannot satisfy a floor."
                    % (name, pin["version"], key, constraint, dep))
                continue
            try:
                ok = satisfies(dep_pin["version"], constraint)
            except ValueError as e:
                failures.append(
                    "%s %s declares %s %r which this gate CANNOT EVALUATE (%s). Refusing rather "
                    "than guessing -- quote the constraint to whoever owns the chart."
                    % (name, pin["version"], key, constraint, e))
                continue
            lines.append("  %-26s %-8s requires %-26s %-18s pinned %-8s %s"
                         % (name, pin["version"], dep, constraint, dep_pin["version"],
                            "ok" if ok else "VIOLATED"))
            if not ok:
                failures.append(
                    "%s %s requires %s %s but %s is pinned at %s. THIS IS THE SILENT-PRUNE SHAPE: "
                    "a chart writing a field the pinned peer's CRD does not declare has that field "
                    "DROPPED by the apiserver, with the object reported as accepted and the "
                    "composition staying Ready. Measured on krateo-keycloak-realm 0.5.3 against "
                    "keycloak-config-kog 0.9.1: the stored CR kept `rememberMe: true` and lost both "
                    "timeouts. Pin the pair or neither."
                    % (name, pin["version"], dep, constraint, dep, dep_pin["version"]))
        if not found:
            lines.append("  %-26s %-8s (no krateo.io/requires-* annotation)" % (name, pin["version"]))
    return lines, failures


# ---------------------------------------------------------------------------------------------
# Self-tests. No registry, no git: `fetch` is injected. Every rule asserted in BOTH directions,
# because a gate that refuses a legitimate pin is worse than no gate -- that lesson is written into
# check-hard-dep-requires-gate.py's header after two drafts did exactly that.
SELF_TESTS = [
    ("a satisfied floor is accepted",
     {"a": {"name": "a", "version": "1.0.0"}, "b": {"name": "b", "version": "2.0.0"}},
     {"a": {"krateo.io/requires-b": ">= 2.0.0"}}, {"a"}, False),
    ("THE MEASURED BUG: a violated floor is REFUSED",
     {"realm": {"name": "realm", "version": "0.5.3"}, "kog": {"name": "kog", "version": "0.9.1"}},
     {"realm": {"krateo.io/requires-kog": ">= 0.9.2"}}, {"realm"}, True),
    ("equal-to-the-floor satisfies >=",
     {"realm": {"name": "realm", "version": "0.5.3"}, "kog": {"name": "kog", "version": "0.9.2"}},
     {"realm": {"krateo.io/requires-kog": ">= 0.9.2"}}, {"realm"}, False),
    ("a multi-term constraint: both terms must hold",
     {"a": {"name": "a", "version": "1.0.0"}, "b": {"name": "b", "version": "0.66.0"}},
     {"a": {"krateo.io/requires-b": ">= 0.62.0, != 0.66.0"}}, {"a"}, True),
    ("the same multi-term constraint PASSES on a version it allows",
     {"a": {"name": "a", "version": "1.0.0"}, "b": {"name": "b", "version": "0.67.0"}},
     {"a": {"krateo.io/requires-b": ">= 0.62.0, != 0.66.0"}}, {"a"}, False),
    ("an UNPARSEABLE constraint is REFUSED, not assumed satisfied",
     {"a": {"name": "a", "version": "1.0.0"}, "b": {"name": "b", "version": "2.0.0"}},
     {"a": {"krateo.io/requires-b": "latest-ish"}}, {"a"}, True),
    ("an annotation naming an UNPINNED component is REFUSED",
     {"a": {"name": "a", "version": "1.0.0"}},
     {"a": {"krateo.io/requires-nosuch": ">= 1.0.0"}}, {"a"}, True),
    ("a chart whose annotations CANNOT BE READ is REFUSED, not skipped",
     {"a": {"name": "a", "version": "1.0.0"}},
     {"a": None}, {"a"}, True),
    ("a chart with NO requires annotation is accepted",
     {"a": {"name": "a", "version": "1.0.0"}},
     {"a": {}}, {"a"}, False),
    ("an unrelated krateo.io annotation is NOT read as a floor",
     {"a": {"name": "a", "version": "1.0.0"}},
     {"a": {"krateo.io/connectors": "github", "krateo.io/verify": "true"}}, {"a"}, False),
    ("a PRE-RELEASE peer does NOT satisfy a floor at its release version",
     {"a": {"name": "a", "version": "1.0.0"}, "b": {"name": "b", "version": "2.0.0-rc1"}},
     {"a": {"krateo.io/requires-b": ">= 2.0.0"}}, {"a"}, True),
]


def self_test():
    bad = []
    for label, pins, annot_map, scope, want_fail in SELF_TESTS:
        def fetch(pin, _m=annot_map):
            a = _m.get(pin["name"], {})
            return (None, "injected read failure") if a is None else (a, None)
        _lines, fails = evaluate(pins, scope, fetch=fetch)
        got = bool(fails)
        ok = got == want_fail
        print("  %s %-62s %s" % ("ok  " if ok else "FAIL", label,
                                 "refused" if got else "accepted"))
        if not ok:
            bad.append(label)
    print()
    if bad:
        print("::error::check-chart-requires-floors SELF-TEST FAILED: %s" % bad)
        return 1
    print("check-chart-requires-floors: OK -- %d cases" % len(SELF_TESTS))
    return 0


def print_registry_breakdown(pins):
    """DERIVED, NOT STATED IN A COMMENT, and printed on EVERY run including the no-change one.

    A prose count drifts as pins move and nothing re-derives it. This gate's own header said "SEVEN
    registries" long after the real figure was six; I quoted that comment to another session as a
    measurement and they put it in their repo's documentation, attributed, before I had counted it.

    A COUNT IN A COMMENT IS WORSE THAN A CLAIM IN A COMMENT, because a number is quotable: it travels
    into other people's files, arrives looking measured, and nothing it passes through can tell that
    it was not. A stale claim stays where it was written and is arguable when read; a stale count
    LEAVES.

    It prints before the early return on purpose. The breakdown is a fact about the PINS FILE, not
    about this PR's diff -- so a run that checks no floors must still report it, or the figure is
    only available on the runs that happen to change a pin.
    """
    reg = {}
    for c in (pins.values() if isinstance(pins, dict) else pins):
        r = (c.get("repo") if isinstance(c, dict) else None) or DEFAULT_REPO
        reg[r] = reg.get(r, 0) + 1
    orgs = {r.split("/")[3] for r in reg if r.count("/") >= 3}
    print("  registries in use: %d distinct path(s) across %d org(s) -- DERIVED on this run; "
          "do not quote a figure from a comment" % (len(reg), len(orgs)))
    for r, n in sorted(reg.items(), key=lambda kv: (-kv[1], kv[0])):
        print("    %-54s %d pin(s)" % (r, n))


def main(argv):
    if "--self-test" in argv:
        return self_test()
    pins = load_pins()
    print_registry_breakdown(pins)
    sweep_all = "--all" in argv
    if sweep_all:
        scope = set(pins)
        note = "ALL %d pins" % len(scope)
    else:
        base = os.environ.get("GITHUB_BASE_REF") or "origin/main"
        ch = changed_pins(pins, base)
        if ch is None:
            print("  could not read %s at %s -- falling back to --all so this cannot pass vacuously"
                  % (PINS, base))
            scope, note = set(pins), "ALL %d pins (base unreadable)" % len(pins)
        elif not ch:
            print("chart-requires-floors: no pin version changed in this PR -- nothing to check")
            return 0
        else:
            scope = ch
            note = "%d changed pin(s): %s" % (len(ch), ", ".join(sorted(ch)))
    print("  scope: %s" % note)
    if not sweep_all:
        print("  NOTE: a changed-pin scan reads the floors DECLARED BY the changed charts. A floor "
              "declared ABOUT a changed component by an UNCHANGED chart is only checked by --all, "
              "which the nightly sweep runs.")
    lines, failures = evaluate(pins, scope)
    for ln in lines:
        print(ln)
    print()
    if failures:
        for f in failures:
            print("::error::chart-requires-floors: %s" % f)
        return 1
    print("chart-requires-floors: OK -- %d chart(s) checked" % len(scope))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
