#!/usr/bin/env python3
"""identity-drift.py — which compositions were RECREATED, APPEARED or DISAPPEARED across a roll.

WHY THE EXPECTATION IS DECLARED ON THE COMMAND LINE. A component version bump legitimately
recreates its composition CR under the new served version, so its uid changes; anything else
changing uid is an uninstall/reinstall nobody asked for. Deciding which is which AFTER seeing the
diff is how an expectation gets chosen to fit the outcome -- two uids moved, both plausible, roll
declared fine. So the expected set is named BEFORE the roll and everything else is a failure.

WHY IT IS ITS OWN FILE. This lived as a heredoc inside verify-roll.sh, which needs a live cluster,
so the decision could not be exercised by CI -- the same reason ready-classify.sh and
managed-drift.py were split out. It is also the arm with the longest history of being
structurally blind: the first version used join(1), and BSD/macOS join has no -j flag, so it
printed a usage error, emitted ZERO rows, and reported "no unexpected changes" because it had
compared nothing.

--expect-new EXISTS BECAUSE ENABLING A FEATURE ADDS COMPOSITIONS. Before it, a row that appeared
was unconditionally a failure, so turning on `features.auditWorm` (which adds krateo-audit-worm)
made this arm exit 3 on a roll that did exactly what it was told. The options were to declare the
addition or to read past a failure by hand, and reading past a gate is how a gate stops meaning
anything.

USAGE
  identity-drift.py <before.tsv> <after.tsv>
      [--expect-uid-change <name>]... [--expect-new <name>]... [--expect-gone <name>]...

EXIT CODES
  0  identity matches the declared expectation exactly
  2  WITHHELD -- the two captures share no composition names, so nothing was compared
  3  an undeclared recreation/addition/removal, or a declared one that did not happen
"""
import sys


def load(path):
    """{name: uid} from a capture's TSV rows. Columns 2 and 4 are name and uid."""
    out = {}
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) > 3 and f[1] and f[3]:
                out[f[1]] = f[3]
    return out


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: identity-drift.py <before.tsv> <after.tsv> "
                         "[--expect-uid-change <name>]... [--expect-new <name>]... "
                         "[--expect-gone <name>]...")
    before_path, after_path = argv[0], argv[1]
    exp_uid, exp_new, exp_gone = set(), set(), set()
    bucket = {"--expect-uid-change": exp_uid, "--expect-new": exp_new, "--expect-gone": exp_gone}
    rest = argv[2:]
    while rest:
        if rest[0] not in bucket:
            raise SystemExit(f"identity-drift: unknown argument {rest[0]!r}")
        if len(rest) < 2:
            raise SystemExit(f"identity-drift: {rest[0]} needs a name")
        bucket[rest[0]].add(rest[1])
        rest = rest[2:]

    b, a = load(before_path), load(after_path)
    common = b.keys() & a.keys()
    moved = sorted(n for n in common if b[n] != a[n])
    added = sorted(a.keys() - b.keys())
    removed = sorted(b.keys() - a.keys())

    print(f"  rows: {len(b)} before, {len(a)} after, {len(common)} comparable")
    print(f"  expected recreated : {' '.join(sorted(exp_uid)) or '<none>'}")
    print(f"  actually recreated : {' '.join(moved) or '<none>'}")
    print(f"  expected new       : {' '.join(sorted(exp_new)) or '<none>'}")
    print(f"  actually new       : {' '.join(added) or '<none>'}")

    # The comparison must have had something to compare. An empty intersection would produce no
    # FAIL lines at all and read as a pass.
    if not common:
        print("  FAIL the two captures share no composition names — nothing was compared, "
              "verdict WITHHELD")
        return 2

    rc = 0
    for want in sorted(exp_uid):
        if want not in a:
            print(f"  FAIL {want} was declared with --expect-uid-change but is not present after "
                  f"the roll")
            rc = 3
        elif want not in moved:
            print(f"  FAIL expected {want} to be recreated; its uid did not change "
                  f"({b.get(want, '<absent before>')})")
            rc = 3
    for got in moved:
        if got not in exp_uid:
            print(f"  FAIL {got} was recreated and was NOT expected to be "
                  f"({b[got]} -> {a[got]}) — an uninstall/reinstall nobody asked for")
            rc = 3
    for n in added:
        if n in exp_new:
            print(f"  ok   {n} APPEARED — declared with --expect-new")
        else:
            print(f"  FAIL {n} APPEARED during the roll and was not there before")
            rc = 3
    for n in removed:
        if n in exp_gone:
            print(f"  ok   {n} DISAPPEARED — declared with --expect-gone")
        else:
            print(f"  FAIL {n} DISAPPEARED during the roll")
            rc = 3
    for want in sorted(exp_new - set(added)):
        print(f"  FAIL {want} was declared with --expect-new and did NOT appear")
        rc = 3
    for want in sorted(exp_gone - set(removed)):
        print(f"  FAIL {want} was declared with --expect-gone and is still present")
        rc = 3

    if rc == 0:
        print("  ok  identity matches the declared expectation exactly, over every row in "
              "both captures")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
