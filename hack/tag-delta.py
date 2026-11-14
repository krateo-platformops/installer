#!/usr/bin/env python3
"""Compute the pin delta between two refs and EMIT the claim a tag message must carry.

WHY THIS EXISTS. On 2026-11-14 I cut tag 0.3.638 and wrote in its message "THIS TAG CARRIES THE TWO
PAIRS AND NOTHING ELSE". It carried 29 pin changes. I had rigorously asserted my pull request's
difference set against `origin/main` -- which was exactly two components, and which was the right
question for the PR -- and then described the TAG as if that were its content. A tag is cut from
main, so it carries everything main accumulated since the PREVIOUS TAG: in that case 27 catch-up
pins that were merged and waiting on an unwedge.

The consequence was not a broken cluster, it was worse in a specific way: five sessions and the user
were told the roll carried one thing, and reasoned about attribution on that basis. An acted-flag
regression was attributed to the one component I said had moved; a latency comparison was planned as
if it could isolate a single image; and the user was asked to decide when to ship a change that had
already shipped.

THE MISSING ASSERTION IS TAG-AGAINST-PREVIOUS-TAG. Every pin gate in .github/scripts compares
against the MERGE TARGET, which is correct for a pull request and silent about a tag.

SO THE CLAIM IS GENERATED, NEVER TYPED. This prints a machine-readable block for the tag message:

    PINS-CHANGED: <n>
      <name> <old> -> <new>
      ...
    CHART-DEPS-CHANGED: <n>
      <name> <old> -> <new>

check-tag-delta.py re-derives the same delta from the two refs and refuses a tag whose block does
not match. A hand-written prose claim cannot be checked; a generated block cannot be wrong unless
the generator is, and the generator has its own falsifiers.

    python3 hack/tag-delta.py 0.3.637 0.3.638     -> the block + a human summary
    python3 hack/tag-delta.py 0.3.640             -> that tag against its predecessor
    python3 hack/tag-delta.py --self-test
"""
import re
import subprocess
import sys

import yaml

# The chart's pins moved to a generated data-only copy on 2026-11-14 (helm's release Secret hit
# 1 MiB). Older tags have only the annotated file. Either yields the same data -- check-pins-min-in-
# sync asserts that -- so try the generated one first and fall back.
PIN_PATHS = ("chart/files/component-pins.min.yaml", "chart/files/component-pins.yaml")
CHART = "chart/Chart.yaml"


def _show(ref, path):
    r = subprocess.run(["git", "show", "%s:%s" % (ref, path)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def pins_at(ref):
    """{name: version} at ref, or None when no pins file exists there."""
    for p in PIN_PATHS:
        t = _show(ref, p)
        if t is None:
            continue
        d = yaml.safe_load(t) or {}
        c = d.get("components") if isinstance(d, dict) else d
        if c is None:
            continue
        return {x["name"]: x for x in c}
    return None


def deps_at(ref):
    t = _show(ref, CHART)
    if t is None:
        return None
    d = yaml.safe_load(t) or {}
    return {x["name"]: {"version": x.get("version")} for x in (d.get("dependencies") or [])}


def delta(old, new):
    """[(name, field, old, new)] sorted, for EVERY field that differs -- not just `version`.

    COMPARING ONLY `version` WAS THIS GATE'S OWN FIRST BUG, caught by running it against real
    history before shipping it. Tag 0.3.639's entire content was authn's `gatewayPath` moving
    /auth -> /authn, which changed live routing on 057 and which a version-only delta reported as
    ZERO CHANGES. The fields that are not versions are exactly the ones that bite: gatewayPath
    moves a route, `kind` and `deps` change what renders, a renamed mapping destroys a credential.
    So every field is compared, and a name appearing or disappearing is reported as such.
    """
    if old is None or new is None:
        return None
    out = []
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a == b:
            continue
        if a is None:
            out.append((k, "(added)", None, b.get("version") if isinstance(b, dict) else b))
            continue
        if b is None:
            out.append((k, "(removed)", a.get("version") if isinstance(a, dict) else a, None))
            continue
        for f in sorted(set(a) | set(b)):
            if a.get(f) != b.get(f):
                out.append((k, f, a.get(f), b.get(f)))
    return out


def block(pin_delta, dep_delta):
    """The machine-readable claim. Generated, never typed."""
    def fmt(rows):
        return ["  %s %s %s -> %s" % (n, f, "~" if a is None else a, "~" if b is None else b)
                for n, f, a, b in rows]
    lines = ["PINS-CHANGED: %d" % len(pin_delta)] + fmt(pin_delta)
    lines += ["CHART-DEPS-CHANGED: %d" % len(dep_delta)] + fmt(dep_delta)
    return "\n".join(lines)


BLOCK_RE = re.compile(
    r"^PINS-CHANGED:\s*(\d+)\s*$(?P<pins>(?:\n\s+\S+ \S+ \S* -> \S*)*)"
    r"\nCHART-DEPS-CHANGED:\s*(\d+)\s*$(?P<deps>(?:\n\s+\S+ \S+ \S* -> \S*)*)",
    re.M)
LINE_RE = re.compile(r"^\s+(\S+) (\S+) (\S*) -> (\S*)$")


def parse_block(text):
    """(pin_delta, dep_delta) from a tag message, or None when no block is present."""
    m = BLOCK_RE.search(text or "")
    if not m:
        return None
    def rows(s):
        out = []
        for l in (s or "").split("\n"):
            mm = LINE_RE.match(l)
            if mm:
                a = None if mm.group(3) == "~" else mm.group(3)
                b = None if mm.group(4) == "~" else mm.group(4)
                out.append((mm.group(1), mm.group(2), a, b))
        return out
    pins, deps = rows(m.group("pins")), rows(m.group("deps"))
    if len(pins) != int(m.group(1)) or len(deps) != int(m.group(3)):
        return "COUNT_MISMATCH"
    return pins, deps


def previous_tag(ref):
    r = subprocess.run(["git", "tag", "--sort=-v:refname"], capture_output=True, text=True)
    tags = [t for t in r.stdout.split("\n") if re.match(r"^\d+\.\d+\.\d+$", t)]
    if ref not in tags:
        return None
    i = tags.index(ref)
    return tags[i + 1] if i + 1 < len(tags) else None


def self_test():
    fails = 0
    E = lambda **kw: dict(kw)
    cases = [
        ("one version moved", {"a": E(version="1")}, {"a": E(version="2")},
         [("a", "version", "1", "2")]),
        ("nothing moved", {"a": E(version="1")}, {"a": E(version="1")}, []),
        ("a pin added", {"a": E(version="1")}, {"a": E(version="1"), "b": E(version="3")},
         [("b", "(added)", None, "3")]),
        ("a pin removed", {"a": E(version="1"), "b": E(version="3")}, {"a": E(version="1")},
         [("b", "(removed)", "3", None)]),
        ("THE 0.3.639 CASE: gatewayPath moved, version did not",
         {"a": E(version="1", gatewayPath="/auth")}, {"a": E(version="1", gatewayPath="/authn")},
         [("a", "gatewayPath", "/auth", "/authn")]),
        ("a non-version field APPEARS", {"a": E(version="1")},
         {"a": E(version="1", registerOnly=True)}, [("a", "registerOnly", None, True)]),
        ("two fields on one component", {"a": E(version="1", kind="K")},
         {"a": E(version="2", kind="L")},
         [("a", "kind", "K", "L"), ("a", "version", "1", "2")]),
    ]
    for label, a, b, want in cases:
        got = delta(a, b)
        if got != want:
            print("FAIL  delta %-26s got %s want %s" % (label, got, want)); fails += 1
        else:
            print("ok    delta %-26s %s" % (label, got or "(empty)"))

    # round trip: a generated block must parse back to the same delta
    pd = [("snowplow", "version", "1.12.67", "1.12.70"),
          ("authn", "gatewayPath", "/auth", "/authn")]
    dd = [("core-provider", "version", "2.13.82", "2.13.95")]
    rt = parse_block("header\n\n" + block(pd, dd) + "\n\ntrailer")
    if rt != (pd, dd):
        print("FAIL  round trip: %r" % (rt,)); fails += 1
    else:
        print("ok    round trip: a generated block parses back to the same delta")

    # FALSIFIERS -- the parser must refuse what it must not accept
    for label, text in [
        ("prose instead of a block", "THIS TAG CARRIES THE TWO PAIRS AND NOTHING ELSE."),
        ("no block at all", "0.3.638 - some release\n\nwords words"),
    ]:
        if parse_block(text) is not None:
            print("FAIL  falsifier: %s was parsed as a block" % label); fails += 1
        else:
            print("ok    falsifier: %-34s is not a block" % label)
    # a count that disagrees with its own rows must be refused
    bad = "PINS-CHANGED: 5\n  a version 1 -> 2\nCHART-DEPS-CHANGED: 0"
    if parse_block(bad) != "COUNT_MISMATCH":
        print("FAIL  falsifier: a block whose count disagrees with its rows was accepted"); fails += 1
    else:
        print("ok    falsifier: a block whose count disagrees with its rows is refused")
    # THE REAL CASE: 0.3.638's actual prose claim must not satisfy the gate
    real = ("THIS TAG CARRIES THE TWO PAIRS AND NOTHING ELSE. The 27-change catch-up stays on "
            "main for a later tag")
    if parse_block(real) is not None:
        print("FAIL  falsifier: 0.3.638's real prose claim parsed as a block"); fails += 1
    else:
        print("ok    falsifier: 0.3.638's REAL prose claim does not satisfy this gate")

    print("tag-delta: %s" % ("OK -- %d field-level delta cases, 1 round trip, 4 falsifiers" % len(cases)
                             if not fails else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if not argv:
        sys.stderr.write("usage: tag-delta.py <ref> [<newer-ref>] | --self-test\n")
        return 2
    if len(argv) >= 2:
        old, new = argv[0], argv[1]
    else:
        new = argv[0]
        old = previous_tag(new)
        if not old:
            sys.stderr.write("tag-delta: cannot find the tag before %s\n" % new)
            return 2
    po, pn = pins_at(old), pins_at(new)
    do, dn = deps_at(old), deps_at(new)
    if po is None or pn is None:
        sys.stderr.write("tag-delta: no pins file at %s or %s\n" % (old, new))
        return 2
    pd, dd = delta(po, pn), delta(do, dn) or []
    sys.stderr.write("tag-delta: %s -> %s   %d pin change(s), %d chart dependency change(s)\n"
                     % (old, new, len(pd), len(dd)))
    sys.stderr.write("           component count %d -> %d\n" % (len(po), len(pn)))
    print(block(pd, dd))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
