#!/usr/bin/env python3
"""Exercise hack/managed-drift.py against fixtures, with no cluster.

WHY THIS TEST EXISTS. The arm it guards is the only one in verify-roll.sh that can catch
core-provider#248, where a composition losing `status.managed` reports Ready=True/Available over
everything it renders and never evaluates. Every other arm is structurally blind to it: the uid is
unchanged, the row count holds, the umbrella's own count does not move, and the readiness sweep is
green. If this comparison is wrong, nothing else is watching.

It SUBPROCESSES the real script rather than importing and reimplementing its rules, for the same
reason check-ready-classify.py shells into the real bash function: a test that restates the logic
agrees with itself.

Each case asserts the exit code AND a substring that must appear, because the three interesting
outcomes share exit codes with the boring ones -- a withheld verdict (2) and a no-shared-names
verdict (2) are the same number and very different findings, and "ok" at exit 0 must be
distinguished from "asserted nothing" at exit 0.
"""
import subprocess
import sys
import tempfile
import os

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "hack", "managed-drift.py")

def cap(lines):
    """Write a minimal capture: some real TSV rows plus the #COMPOSITION-MANAGED lines."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
    fh.write("krateo-system\tportal\tPortal\tuid-1\t1\t2026-01-01T00:00:00Z\t-\t-\t0\tinstaller-platform\n")
    for ln in lines:
        fh.write(ln + "\n")
    fh.close()
    return fh.name

M = "#COMPOSITION-MANAGED "

CASES = [
    # name, before lines, after lines, extra argv, want_rc, want_substring
    ("steady",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 10 present"],
     [], 0, "no composition's managed set shrank"),

    ("growth is not a failure",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 14 present"],
     [], 0, "manages more objects: 10 -> 14"),

    ("THE #248 CASE: present -> ABSENT",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 0 ABSENT"],
     [], 4, "HAD status.managed and now has NO KEY AT ALL"),

    ("a plain shrink",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 3 present"],
     [], 4, "manages FEWER objects than before: 10 -> 3"),

    ("shrink to zero but the key SURVIVES is still a shrink",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 0 present"],
     [], 4, "manages FEWER objects than before: 10 -> 0"),

    ("already ABSENT before: the #248 baseline, named not failed",
     [M + "krateo-system/observability-page 0 ABSENT"],
     [M + "krateo-system/observability-page 0 ABSENT"],
     [], 0, "ABSENT ALREADY, before this"),

    ("a declared shrink passes",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 3 present"],
     ["--expect-shrink", "krateo-system/portal"], 0, "declared with --expect-shrink"),

    ("a declared shrink that did NOT happen fails",
     [M + "krateo-system/portal 10 present"],
     [M + "krateo-system/portal 10 present"],
     ["--expect-shrink", "krateo-system/portal"], 4, "did NOT shrink"),

    ("a declaration does not excuse a DIFFERENT composition",
     [M + "krateo-system/portal 10 present", M + "krateo-system/snowplow 8 present"],
     [M + "krateo-system/portal 3 present",  M + "krateo-system/snowplow 2 present"],
     ["--expect-shrink", "krateo-system/portal"], 4, "krateo-system/snowplow manages FEWER"),

    ("an old BEFORE capture asserts NOTHING rather than passing",
     [],
     [M + "krateo-system/portal 10 present"],
     [], 2, "asserts NOTHING"),

    ("an AFTER capture with no lines is a BROKEN INSTRUMENT, not a pass",
     [M + "krateo-system/portal 10 present"],
     [],
     [], 4, "the instrument is broken"),

    ("no shared names compares nothing",
     [M + "krateo-system/portal 10 present"],
     [M + "other-ns/portal 10 present"],
     [], 2, "share no composition names"),

    ("a malformed line is NOT skipped silently",
     [M + "krateo-system/portal 10"],
     [M + "krateo-system/portal 10 present"],
     [], 1, "unparseable line"),
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
        print("::error::check-managed-drift: hack/managed-drift.py does not behave as required:")
        for f in failures:
            print("  " + f.replace("\n", "\n    "))
        return 1

    # Assert the suite covers every verdict the script can reach. A case list that drifted to test
    # only the passing arms would report OK while the failure arms went unexercised.
    rcs = {c[4] for c in CASES}
    missing = {0, 1, 2, 4} - rcs
    if missing:
        print(f"::error::check-managed-drift: no case exercises exit code(s) {sorted(missing)}")
        return 1
    print(f"check-managed-drift: OK — all {len(CASES)} cases behave as required, "
          f"covering exit codes {sorted(rcs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
