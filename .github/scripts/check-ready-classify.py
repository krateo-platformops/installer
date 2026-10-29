#!/usr/bin/env python3
"""CI guard: exercise hack/ready-classify.sh, the ONE place the OPERATOR-side gate decides what a
composition's Ready condition means.

WHY THIS EXISTS. verify-roll.sh needs a live cluster, so a decision embedded in it could not be
exercised by CI. That is precisely how this repo's CHART-side gate shipped a version that only
accepted "True" and went unexamined until core-provider#165 asked what it does with `Unknown` --
the fix there was to split the decision out (inst.readyFromConditions + check-ready-gate.py). This
is the same move for the operator-side gate.

IT SHELLS INTO THE REAL FUNCTION rather than reimplementing the rule. A test that restates the
logic in Python would agree with itself and prove nothing about the shipped code.

BOTH FAILURE DIRECTIONS ARE LIVE:
  calling `Creating` a FAULT      -> every roll sampled before it settles reports UNHEALTHY
                                     COMPOSITION. Observed on the 0.3.466 roll: the umbrella read
                                     Ready=False/Creating while the roll completed normally.
  calling a FAULT `converging`    -> a real failure is downgraded to "still converging" and the
                                     verdict is withheld forever instead of failing loudly.

SYNONYMS MUST FAIL. core-provider#234 recognises the literal string `Creating` and DELIBERATELY
rejects Progressing/Converging/Pending, which fall back to Unavailable. Accepting a synonym here
would make this gate disagree with the controller that stamped the condition.
"""
import subprocess, sys

SH = "hack/ready-classify.sh"

CASES = [
    # (status,     reason,          expected,       why this case is here)
    ("True",       "",              "healthy",      "the ordinary pass"),
    ("True",       "Available",     "healthy",      "reason is irrelevant when status is True"),
    ("False",      "Creating",      "converging",   "core-provider#234's recognised value"),
    ("False",      "creating",      "fail",         "CASE VARIANT: off-contract. EqualFold applies to the author-declared .status.health.reason, NOT to the condition this reads, whose reason is the Go constant"),
    ("False",      "CREATING",      "fail",         "CASE VARIANT: same reason. A tolerant match would report healthy progress for a value the controller cannot emit"),
    ("False",      "Unavailable",   "fail",         "a real fault stays a fault"),
    ("False",      "Progressing",   "fail",         "SYNONYM: rejected upstream, must fail here too"),
    ("False",      "Converging",    "fail",         "SYNONYM: rejected upstream, must fail here too"),
    ("False",      "Pending",       "fail",         "SYNONYM: rejected upstream, must fail here too"),
    ("False",      "",              "fail",         "no reason means what ready=false always meant"),
    ("Unknown",    "ZeroEvidence",  "zeroevidence", "health could not be assessed at all (#165)"),
    ("Unknown",    "Creating",      "fail",         "a converging child reports Unknown too and MUST block"),
    ("Unknown",    "",              "fail",         "bare Unknown must never be waved through"),
    ("Weird",      "x",             "fail",         "an unrecognised status is the case nobody planned for"),
]


def classify(status: str, reason: str) -> str:
    out = subprocess.run(
        ["bash", "-c", f'source {SH}; classify_ready "$1" "$2"', "_", status, reason],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print(f"::error::classify_ready exited {out.returncode} for ({status!r}, {reason!r}): "
              f"{out.stderr.strip()}")
        sys.exit(1)
    return out.stdout.strip()


def main() -> int:
    bad = []
    for status, reason, want, why in CASES:
        got = classify(status, reason)
        mark = "ok  " if got == want else "FAIL"
        print(f"  {mark} Ready={status:<8} reason={reason or '<empty>':<14} -> {got:<12} ({why})")
        if got != want:
            bad.append((status, reason, want, got))

    # The instrument must have discriminated, not merely run. A classify_ready that returned the
    # same answer for every input would satisfy a one-sided table silently.
    distinct = {classify(s, r) for s, r, _, _ in CASES}
    if len(distinct) < 4:
        print(f"::error::check-ready-classify: the classifier returned only {sorted(distinct)} "
              f"across {len(CASES)} inputs. A classifier that cannot tell the cases apart passes "
              f"a one-sided table while deciding nothing.")
        return 1

    if bad:
        print("::error::check-ready-classify: hack/ready-classify.sh does not discriminate as required:")
        for status, reason, want, got in bad:
            print(f"  Ready={status} reason={reason or '<empty>'}: want {want!r}, got {got!r}")
        return 1

    print(f"check-ready-classify: OK — all {len(CASES)} cases discriminate across "
          f"{sorted(distinct)}, incl. False/Creating CONVERGING and the three rejected synonyms failing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
