#!/usr/bin/env python3
"""managed-drift.py — compare the PER-COMPOSITION managed-set sizes of two captures.

WHY THIS IS NOT COVERED BY ANYTHING ELSE IN hack/.

verify-roll.sh already has three arms, and a composition losing its own managed set slips past
all of them:

  * identity    -- the CR still exists with the same uid, so nothing moves.
  * row count   -- 83 before, 83 after.
  * umbrella    -- `#MANAGED-CHILDREN` is the UMBRELLA's count. The umbrella manages the component
                   CRs; it does not manage their children. A component composition dropping its own
                   managed set leaves the umbrella's total untouched.
  * Ready       -- core-provider#248: `setStatus` writes `managed` only when resources are present,
                   and the rollup's `!found || len == 0` branch then returns **Available**. So the
                   condition reports True precisely BECAUSE the set is gone. The gate is not merely
                   silent here; it is actively green.

Confirmed on krateo-057 at installer-0.3.466: 7 of 83 compositions have NO `status.managed` key
and report Ready=True/Available -- agentgateway-policies, alerts-page, blueprints-page,
builders-page, config-refs-mcp-server, observability-page, portal-agents. The remaining 76 account
for 1156 managed objects between them.

ABSENT IS NOT THE SAME AS EMPTY, and this script keeps them apart everywhere.
`managed: []` is an evaluation that found nothing. A missing key is no evaluation at all. Only the
second is #248. Collapsing both to 0 would throw away the one distinction that tells the bug from a
chart that legitimately renders nothing.

WHAT COUNTS AS A FAILURE. A set that SHRANK, or that went present -> ABSENT. Not growth, and not a
composition that was already ABSENT in the before-capture -- those are the #248 baseline, named in
the output so that a fix moving them to Unknown/ZeroEvidence reads as the declared transition
rather than as a regression. A legitimate shrink is declared up front with --expect-shrink, for the
same reason --expect-uid-change exists: an expectation chosen after seeing the outcome always fits.

USAGE
  managed-drift.py <before.tsv> <after.tsv> [--expect-shrink <ns/name>]...

EXIT CODES
  0  no undeclared shrink
  2  the BEFORE capture predates these lines -- NOTHING was compared, verdict withheld
  4  an undeclared shrink, or a declared one that did not happen
"""
import sys

MARK = "#COMPOSITION-MANAGED "


def load(path):
    """Return {ns/name: (length, present_bool)} from a capture's #COMPOSITION-MANAGED lines."""
    out = {}
    with open(path) as fh:
        for line in fh:
            if not line.startswith(MARK):
                continue
            parts = line[len(MARK):].split()
            if len(parts) != 3:
                # A malformed line must not be skipped silently: a capture whose lines all failed
                # to parse would otherwise look exactly like a capture that predates them.
                raise SystemExit(f"managed-drift: unparseable line in {path}: {line.rstrip()!r}")
            key, length, state = parts
            out[key] = (int(length), state == "present")
    return out


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: managed-drift.py <before.tsv> <after.tsv> [--expect-shrink <ns/name>]...")
    before_path, after_path = argv[0], argv[1]
    expect, rest = set(), argv[2:]
    while rest:
        if rest[0] != "--expect-shrink":
            raise SystemExit(f"managed-drift: unknown argument {rest[0]!r}")
        if len(rest) < 2:
            raise SystemExit("managed-drift: --expect-shrink needs a <ns/name>")
        expect.add(rest[1])
        rest = rest[2:]

    b, a = load(before_path), load(after_path)

    if not b:
        print("  -- the BEFORE capture records no per-composition managed sets (it predates this")
        print("     check). Nothing was compared, so this arm asserts NOTHING. Re-capture to get it;")
        print("     a missing comparison is reported here rather than passing quietly.")
        return 2
    if not a:
        print("  FAIL the AFTER capture records no per-composition managed sets, while the BEFORE one")
        print(f"       has {len(b)}. The capture script did not emit them — the instrument is broken,")
        print("       which is not the same as finding nothing wrong.")
        return 4

    common = b.keys() & a.keys()
    if not common:
        print("  FAIL the two captures share no composition names — nothing was compared.")
        return 2

    rc = 0
    baseline_absent = sorted(k for k in common if not b[k][1])
    shrank, vanished, grew = [], [], []
    for k in sorted(common):
        bl, bp = b[k]
        al, ap = a[k]
        if bp and not ap:
            vanished.append(k)
        elif al < bl:
            shrank.append((k, bl, al))
        elif al > bl:
            grew.append((k, bl, al))

    print(f"  compared {len(common)} compositions "
          f"({len(b)} in before, {len(a)} in after)")

    if baseline_absent:
        print(f"  -- {len(baseline_absent)} composition(s) had status.managed ABSENT ALREADY, before this")
        print("     roll — the core-provider#248 baseline, not caused by it. Listed so that a fix")
        print("     moving them to Unknown/ZeroEvidence reads as the declared transition:")
        for k in baseline_absent:
            print(f"       {k}")

    for k in vanished:
        if k in expect:
            print(f"  ok   {k} lost its status.managed key — declared with --expect-shrink")
        else:
            print(f"  FAIL {k} HAD status.managed and now has NO KEY AT ALL "
                  f"(was {b[k][0]} objects). It will report Ready=True/Available over everything it")
            print("       renders and never evaluates (core-provider#248). The condition is green")
            print("       BECAUSE the evidence is gone, so no readiness check can catch this.")
            rc = 4
    for k, bl, al in shrank:
        if k in expect:
            print(f"  ok   {k} manages fewer objects: {bl} -> {al} — declared with --expect-shrink")
        else:
            print(f"  FAIL {k} manages FEWER objects than before: {bl} -> {al}. The health rollup only")
            print("       evaluates what was rendered (core-provider#230), so it will find the smaller")
            print("       set healthy and say nothing about the ones that left.")
            rc = 4
    for k, bl, al in grew:
        print(f"  ok   {k} manages more objects: {bl} -> {al} (stated so it is not mistaken for noise)")

    declared_but_quiet = sorted(expect - {k for k in vanished} - {k for k, _, _ in shrank})
    for k in declared_but_quiet:
        print(f"  FAIL {k} was declared with --expect-shrink and its managed set did NOT shrink")
        rc = 4

    for k in sorted(a.keys() - b.keys()):
        print(f"  -- {k} is new in the after-capture; no before value to compare")
    for k in sorted(b.keys() - a.keys()):
        print(f"  -- {k} is gone from the after-capture (the identity arm above owns that finding)")

    if rc == 0 and not vanished and not shrank:
        print("  ok   no composition's managed set shrank")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
