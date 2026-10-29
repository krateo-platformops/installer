#!/usr/bin/env python3
"""Exercise hack/identity-drift.py against fixtures, with no cluster.

WHY THIS ARM NEEDS A TEST MORE THAN THE OTHERS. It has already been structurally blind once: the
first version compared with join(1), BSD/macOS join has no -j flag, so it printed a usage error,
emitted ZERO rows, and reported "no unexpected changes" having compared nothing. A green line over
an empty comparison is the failure mode this suite exists to make impossible, which is why there is
a case asserting that a no-shared-names comparison WITHHOLDS rather than passes.

It subprocesses the real script rather than reimplementing its rules.

Each case asserts the exit code AND a distinguishing substring, because exit 3 covers five
different findings (undeclared recreation, undeclared addition, undeclared removal, a declared
recreation that did not happen, a declared addition that did not appear) and an exit code alone
cannot tell them apart.
"""
import subprocess
import sys
import tempfile
import os

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "hack", "identity-drift.py")


def cap(rows):
    """rows: list of (name, uid). Writes a capture with the real 10-column layout."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
    for name, uid in rows:
        fh.write(f"krateo-system\t{name}\tKind\t{uid}\t1\t"
                 f"2026-01-01T00:00:00Z\t-\t-\t0\tinstaller-platform\n")
    # A comment line, to prove comments are skipped rather than parsed as a row.
    fh.write("#MANAGED-CHILDREN 122\n")
    fh.close()
    return fh.name


STEADY = [("portal", "uid-p"), ("snowplow", "uid-s")]

CASES = [
    ("steady", STEADY, STEADY, [], 0, "identity matches the declared expectation exactly"),

    ("an undeclared recreation fails",
     STEADY, [("portal", "uid-NEW"), ("snowplow", "uid-s")],
     [], 3, "was recreated and was NOT expected"),

    ("a declared recreation passes",
     STEADY, [("portal", "uid-NEW"), ("snowplow", "uid-s")],
     ["--expect-uid-change", "portal"], 0, "identity matches the declared expectation exactly"),

    ("a declared recreation that did NOT happen fails",
     STEADY, STEADY,
     ["--expect-uid-change", "portal"], 3, "its uid did not change"),

    ("a declared recreation of an ABSENT composition fails",
     STEADY, [("snowplow", "uid-s")],
     ["--expect-uid-change", "portal"], 3, "is not present after the roll"),

    ("THE --expect-new CASE: an undeclared addition fails",
     STEADY, STEADY + [("krateo-audit-worm", "uid-w")],
     [], 3, "APPEARED during the roll and was not there before"),

    ("a declared addition passes",
     STEADY, STEADY + [("krateo-audit-worm", "uid-w")],
     ["--expect-new", "krateo-audit-worm"], 0, "APPEARED — declared with --expect-new"),

    ("a declared addition that did NOT appear fails",
     STEADY, STEADY,
     ["--expect-new", "krateo-audit-worm"], 3, "did NOT appear"),

    ("an undeclared removal fails",
     STEADY, [("portal", "uid-p")],
     [], 3, "DISAPPEARED during the roll"),

    ("a declared removal passes",
     STEADY, [("portal", "uid-p")],
     ["--expect-gone", "snowplow"], 0, "DISAPPEARED — declared with --expect-gone"),

    ("a declared removal that is still present fails",
     STEADY, STEADY,
     ["--expect-gone", "snowplow"], 3, "is still present"),

    ("declaring one does NOT excuse another",
     STEADY, [("portal", "uid-NEW"), ("snowplow", "uid-NEW2")],
     ["--expect-uid-change", "portal"], 3, "snowplow was recreated and was NOT expected"),

    ("no shared names WITHHOLDS rather than passing",
     STEADY, [("other", "uid-o")],
     [], 2, "nothing was compared, verdict WITHHELD"),

    ("an unknown flag is rejected, not ignored",
     STEADY, STEADY, ["--expect-whatever", "portal"], 1, "unknown argument"),
]


def main():
    failures = []
    for name, before, after, extra, want_rc, want_sub in CASES:
        bp, ap = cap(before), cap(after)
        try:
            p = subprocess.run([sys.executable, SCRIPT, bp, ap] + extra,
                               capture_output=True, text=True)
            out = p.stdout + p.stderr
            if p.returncode != want_rc:
                failures.append(f"{name}: want rc {want_rc}, got {p.returncode}\n{out}")
            elif want_sub not in out:
                failures.append(f"{name}: output lacks {want_sub!r}\n{out}")
        finally:
            os.unlink(bp)
            os.unlink(ap)

    if failures:
        print("::error::check-identity-drift: hack/identity-drift.py does not behave as required:")
        for f in failures:
            print("  " + f.replace("\n", "\n    "))
        return 1

    rcs = {c[4] for c in CASES}
    missing = {0, 1, 2, 3} - rcs
    if missing:
        print(f"::error::check-identity-drift: no case exercises exit code(s) {sorted(missing)}")
        return 1
    print(f"check-identity-drift: OK — all {len(CASES)} cases behave as required, "
          f"covering exit codes {sorted(rcs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
