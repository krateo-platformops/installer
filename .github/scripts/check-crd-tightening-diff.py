#!/usr/bin/env python3
"""CI guard: hack/crd-tightening-diff.py's OFFLINE fixture suite.

Same pattern as check-chart-root-schema-gate.py: the hack/ tool is the pin-time instrument and
needs the network (it pulls two chart versions); this runs its SELF-TEST, which is synthetic and
offline, so the instrument cannot rot between pins.

WHY THAT MATTERS MORE THAN USUAL HERE. The tool decides one thing: does a tightening in a -crd
bump reach a shape that ALREADY EXISTS (stored objects become unupdatable at their next write) or
only a NEW optional field (nothing can be rejected)? Its first draft inverted exactly that verdict
-- it recorded only paths that already had a constraint, so "did the parent exist before?" was
answered by "did it already have a constraint?", and a CEL rule added to a previously
unconstrained EXISTING field read as safe. Three self-test cases failed on it. An instrument whose
only important output can silently invert is one that has to be exercised on every PR, not only
when someone remembers to pin a -crd chart.

The suite's own first case is the one authn asked for after catching a blind spot in an earlier
ad-hoc walker of mine: an EXISTING array-of-objects gains a `required` on its ITEMS, which a
walker that follows only `properties` cannot see at all.
"""
import os
import subprocess
import sys

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                    "hack", "crd-tightening-diff.py")
MIN_CASES = 8


def main() -> int:
    if not os.path.isfile(TOOL):
        print("::error::%s not found -- the gate cannot run its fixtures" % TOOL)
        return 1
    r = subprocess.run([sys.executable, TOOL, "--self-test"], capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().split("\n"):
        print("  %s" % line)
    cases = sum(1 for l in out.split("\n") if l.startswith("ok  ") or l.startswith("FAIL"))
    if cases < MIN_CASES:
        print("::error::the suite reported %d case(s), expected at least %d. Either a case was "
              "deleted or the output format changed -- a runner that counts nothing must FAIL, "
              "because '0 cases, 0 failures' is indistinguishable from success."
              % (cases, MIN_CASES))
        return 1
    if r.returncode != 0:
        print("::error::crd-tightening-diff self-test FAILED. The instrument that decides whether "
              "a -crd bump can break stored objects is not trustworthy until this passes.")
        return 1
    print("\ncheck-crd-tightening-diff: OK -- %d fixture case(s) passed" % cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
