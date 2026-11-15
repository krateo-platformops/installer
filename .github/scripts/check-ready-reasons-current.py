#!/usr/bin/env python3
"""hack/ready-reasons.yaml must not drift from core-provider's GENERATED table.

WHY THIS GATE EXISTS. Every roll verification in this repo needs a set of Ready reasons that count
as converged, and I built that set from the reasons I had MET. On 2026-11-15 I met three in one day
-- ObserveFailed, HelmOperationInProgress, PendingHelmOperation -- each time by misreading a healthy
or settling cluster as a broken one, and the worst of them had me one command from `helm rollback`
on an umbrella managing 137 live children.

core-provider#515 asked for a table generated from their code and #517 delivered it. Diffing my set
against it found ReconcileGracefullyPaused missing: Ready=True, rolls up healthy, and my verification
would have reported it as a FAILURE. It has never appeared on krateo-057, which is why I had not
noticed -- a classifier built from observation is complete only for the clusters you have watched.

So the classification is VENDORED (tooling must not need a network read to decide whether a cluster
is healthy) and this gate checks the copy rather than trusting it.

WHY IT DOES NOT RUN ON EVERY PR. It reads a file from another repo. A per-PR network dependency is a
flake source, and a gate that fails closed on a 404 blocks every PR for a reason unrelated to any of
them -- which I argued to oasgen-provider is the reason their retraction `use:` rule is right. So it
runs on a schedule, on dispatch, and on PRs that touch the vendored file itself.

IT FAILS RATHER THAN SKIPS when it cannot fetch. A guard that skips when its source is unreachable
reports success for a thing it did not do, which is how a check that never ran looks exactly like one
that passed.

    python3 .github/scripts/check-ready-reasons-current.py              -> check against upstream
    python3 .github/scripts/check-ready-reasons-current.py --self-test  -> offline falsifiers
"""
import base64
import json
import re
import subprocess
import sys

import yaml

VENDORED = "hack/ready-reasons.yaml"
UPSTREAM_REPO = "krateo-platformops/core-provider"
UPSTREAM_PATH = "docs/ready-reasons.md"

# core-provider's `parent verdict` -> the class this repo files the reason under.
# The two "unevaluated" rows are a DELIBERATE difference, documented in the vendored file: their
# rollup counts such a child healthy for a PARENT, while this repo's tooling counts the composition
# itself not converged. Mapping them here is what keeps the difference explicit instead of looking
# like drift every time the gate runs.
VERDICT_TO_CLASS = {
    "healthy": "converged",
    "converging": "converging",
    "failed": "failed",
    "unevaluated (rolls up as healthy)": "unevaluated",
}

# PARSE BY HEADER NAME, NOT BY POSITION.
#
# This gate read the first four cells positionally. core-provider#523 then inserted an
# "expected to clear" column -- and it happened to land AFTER "parent verdict", so the parse still
# lined up. That was luck. frontend hit the identical bug in the portal's own copy of this gate and
# fixed it the same way; they are the reason this is being corrected rather than left.
#
# Measured on a synthetic table with a column inserted at position 3: the 4th capture read the
# WRITER as the parent verdict, and the gate reported UNMAPPED VERDICT. Loud -- but loud by luck.
# Had that inserted cell contained "healthy" or "converging", the gate would have AGREED with a
# shifted table and reported a clean comparison against nothing.
#
# So: locate the header row, map names to indices, and refuse rather than guess when a required
# column is missing or a row's cell count does not match the header's.
REQUIRED_COLUMNS = ("reason", "status", "parent verdict")



def _cells(line):
    """The cells of a markdown table row, or None when the line is not one."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    inner = s[1:-1] if s.endswith("|") else s[1:]
    return [c.strip() for c in inner.split("|")]


def parse_table(text):
    """{reason: {statuses, verdicts}} from the generated markdown, read BY HEADER NAME.

    Raises ValueError when the table is missing, a required column is absent, or a row's cell
    count does not match the header's -- never guesses a column's position."""
    header, idx = None, {}
    rows = {}
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            continue
        low = [c.lower() for c in cells]
        if header is None:
            if "reason" in low:                      # the header row
                header = cells
                idx = {name: i for i, name in enumerate(low)}
                missing = [c for c in REQUIRED_COLUMNS if c not in idx]
                if missing:
                    raise ValueError(
                        "the generated table is missing required column(s) %s; found %s. Refusing "
                        "to guess positions." % (missing, low))
            continue
        if set("".join(cells)) <= set("-: "):        # the |---|---| separator
            continue
        if len(cells) != len(header):
            raise ValueError(
                "a row has %d cells but the header has %d: %r. A cell-count mismatch means the "
                "column mapping cannot be trusted." % (len(cells), len(header), cells[:3]))
        reason = cells[idx["reason"]].strip().strip("`")
        if not reason:
            continue
        e = rows.setdefault(reason, {"statuses": set(), "verdicts": set()})
        e["statuses"].add(cells[idx["status"]].strip())
        e["verdicts"].add(cells[idx["parent verdict"]].strip())
    if header is None:
        raise ValueError("no markdown table found in the generated file at all. A comparison "
                         "against an empty set would pass everything.")
    return rows


def classify(vendored):
    """{reason: class} from the vendored file; refuses a reason filed under two classes."""
    out = {}
    for cls in ("converged", "converging", "failed", "unevaluated"):
        for r in vendored.get(cls) or []:
            if r in out:
                raise ValueError("%s is filed under both %s and %s" % (r, out[r], cls))
            out[r] = cls
    return out


def compare(upstream_rows, vendored):
    """Broken-rule sentences; [] when the vendored copy agrees with the table."""
    bad = []
    mine = classify(vendored)
    up = set(upstream_rows)
    missing = up - set(mine)
    extra = set(mine) - up
    for r in sorted(missing):
        v = sorted(upstream_rows[r]["verdicts"])
        bad.append("MISSING: upstream has %r (verdict %s) and the vendored file does not classify "
                   "it. Any verification built from this file would treat it as unknown -- and if "
                   "its verdict is healthy, would report a FAILURE on a healthy cluster." % (r, v))
    for r in sorted(extra):
        bad.append("STALE: the vendored file classifies %r, which upstream no longer lists. Either "
                   "it was removed or renamed; a classifier naming a reason that cannot be written "
                   "is dead weight that hides the real set." % r)
    for r in sorted(up & set(mine)):
        want = {VERDICT_TO_CLASS.get(v) for v in upstream_rows[r]["verdicts"]}
        want.discard(None)
        if not want:
            bad.append("UNMAPPED VERDICT: %r carries verdict(s) %s, which this gate does not know "
                       "how to map. Upstream added a verdict value; VERDICT_TO_CLASS needs it."
                       % (r, sorted(upstream_rows[r]["verdicts"])))
            continue
        if mine[r] not in want:
            bad.append("MISCLASSIFIED: %r is %r here but upstream's parent verdict %s maps to %s."
                       % (r, mine[r], sorted(upstream_rows[r]["verdicts"]), sorted(want)))
    # a reason that is healthy upstream AND paired with Synced!=True must be listed as needing it
    req = set(vendored.get("requires_synced_true") or [])
    for r in sorted(req - set(mine)):
        bad.append("requires_synced_true names %r, which is not classified at all." % r)
    return bad


def fetch_upstream():
    r = subprocess.run(["gh", "api", "repos/%s/contents/%s" % (UPSTREAM_REPO, UPSTREAM_PATH),
                        "--jq", ".content"], capture_output=True, text=True, timeout=180)
    if r.returncode or not r.stdout.strip():
        return None, "could not fetch %s:%s -- %s" % (
            UPSTREAM_REPO, UPSTREAM_PATH, (r.stderr or r.stdout).strip()[:200])
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8"), None
    except Exception as e:  # noqa: BLE001
        return None, "upstream content did not decode: %s" % e


SELF_TESTS = [
    # (label, upstream rows, vendored dict, expect_refused)
    ("agreement is accepted",
     {"Available": {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": []}, False),
    ("THE REAL BUG: a healthy reason absent from the vendored file is REFUSED",
     {"Available": {"statuses": {"True"}, "verdicts": {"healthy"}},
      "ReconcileGracefullyPaused": {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": []}, True),
    ("a converging reason filed as converged is REFUSED",
     {"Creating": {"statuses": {"False"}, "verdicts": {"converging"}}},
     {"converged": ["Creating"], "converging": [], "failed": [], "unevaluated": []}, True),
    ("a failed reason filed as converging is REFUSED",
     {"Unavailable": {"statuses": {"False"}, "verdicts": {"failed"}}},
     {"converged": [], "converging": ["Unavailable"], "failed": [], "unevaluated": []}, True),
    ("a vendored reason upstream dropped is REFUSED",
     {"Available": {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available", "GoneForever"], "converging": [], "failed": [],
      "unevaluated": []}, True),
    ("THE DELIBERATE DIFFERENCE is accepted: unevaluated maps to its own class",
     {"ObserveFailed": {"statuses": {"Unknown"},
                        "verdicts": {"unevaluated (rolls up as healthy)"}}},
     {"converged": [], "converging": [], "failed": [], "unevaluated": ["ObserveFailed"]}, False),
    ("a NEW upstream verdict value is REFUSED rather than silently mapped",
     {"Whatever": {"statuses": {"False"}, "verdicts": {"brand-new-verdict"}}},
     {"converged": ["Whatever"], "converging": [], "failed": [], "unevaluated": []}, True),
    ("a reason on TWO rows (cdc + runtime) with one verdict is accepted",
     {"Creating": {"statuses": {"False"}, "verdicts": {"converging"}}},
     {"converged": [], "converging": ["Creating"], "failed": [], "unevaluated": []}, False),
]


def self_test():
    fails = 0
    for label, up, vend, want_refused in SELF_TESTS:
        bad = compare(up, vend)
        refused = bool(bad)
        if refused != want_refused:
            print("FAIL  %-62s refused=%s want=%s  %s" % (label, refused, want_refused, bad))
            fails += 1
        else:
            print("ok    %-62s %s" % (label, "refused" if refused else "accepted"))
    # a reason in two classes must raise rather than pick one
    try:
        classify({"converged": ["X"], "converging": ["X"], "failed": [], "unevaluated": []})
        print("FAIL  a reason in two classes is refused")
        fails += 1
    except ValueError:
        print("ok    %-62s refused" % "a reason filed under two classes")
    print()
    print("check-ready-reasons-current: %s" % ("OK -- %d cases" % (len(SELF_TESTS) + 1)
                                               if not fails else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    vendored = yaml.safe_load(open(VENDORED, encoding="utf-8"))
    if "--self-test" in argv:
        return self_test()
    text, err = fetch_upstream()
    if err:
        # FAIL, never skip: an unreachable source reads exactly like a clean comparison.
        print("::error::check-ready-reasons-current: %s" % err)
        return 2
    rows = parse_table(text)
    if not rows:
        print("::error::check-ready-reasons-current: parsed ZERO rows from %s:%s. The table's "
              "format changed, and a gate that compares against an empty set would pass "
              "everything." % (UPSTREAM_REPO, UPSTREAM_PATH))
        return 2
    print("  upstream %s:%s -> %d distinct reason(s)" % (UPSTREAM_REPO, UPSTREAM_PATH, len(rows)))
    bad = compare(rows, vendored)
    for b in bad:
        print("::error::check-ready-reasons-current: %s" % b)
    if bad:
        return 1
    print("  OK -- %d reason(s), classification agrees with the generated table "
          "(2 deliberate 'unevaluated' differences mapped explicitly)" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
