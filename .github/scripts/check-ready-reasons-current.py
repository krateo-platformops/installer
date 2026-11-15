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
# `condition` arrives with core-provider#529: the table grows from 14 rows over Ready alone to 36
# rows over five condition types, with a new FIRST column naming the condition. THIS PARSER MUST
# WORK ON BOTH SHAPES, because they are holding that merge until it does -- a parser that only
# understood the new format would be useless for validating the file that exists today.
#
# WHY KEY BY (condition, reason) AND NOT reason alone: the same reason string appears under two
# conditions. `ReconcilePaused` is Ready=True AND Synced=False. A reason-keyed map collapses them
# and then disagrees with itself about the status.
REQUIRED_COLUMNS = ("reason", "status")
CONDITION_COLUMN = "condition"
# `parent verdict` is filled only on Ready rows in the new shape; the others read a dash, because
# the rollup reads only Ready.
VERDICT_COLUMN = "parent verdict"
# ApiResolved has one OPEN row whose reason is literally "(authn's reason)": on a refusal authn's
# own discriminator passes through verbatim (NoMapping, AudienceMismatch, MissingBearer, ...). So an
# UNLISTED ApiResolved reason is that family, NOT a drift to refuse. Refusing it would make the gate
# fire whenever authn declined for a new reason -- a false alarm by design.
OPEN_ROW_CONDITIONS = {"ApiResolved"}



def _cells(line):
    """The cells of a markdown table row, or None when the line is not one."""
    s = line.strip()
    if not s.startswith("|"):
        return None
    inner = s[1:-1] if s.endswith("|") else s[1:]
    return [c.strip() for c in inner.split("|")]


def parse_table(text):
    """{(condition, reason): {statuses, verdicts}} from the generated markdown, BY HEADER NAME.

    Works on both shapes: with no `condition` column every row is a Ready row, which is what the
    pre-#529 table was. Raises ValueError rather than guessing a column's position."""
    header, idx = None, {}
    rows = {}
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            continue
        low = [c.lower() for c in cells]
        if header is None:
            if "reason" in low:
                header = cells
                idx = {name: i for i, name in enumerate(low)}
                missing = [c for c in REQUIRED_COLUMNS if c not in idx]
                if missing:
                    raise ValueError(
                        "the generated table is missing required column(s) %s; found %s. Refusing "
                        "to guess positions." % (missing, low))
                if CONDITION_COLUMN not in idx and VERDICT_COLUMN not in idx:
                    raise ValueError(
                        "the table has neither a %r nor a %r column, so there is no way to tell "
                        "which condition a row describes or how a parent reads it. Found %s."
                        % (CONDITION_COLUMN, VERDICT_COLUMN, low))
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if len(cells) != len(header):
            raise ValueError(
                "a row has %d cells but the header has %d: %r. A cell-count mismatch means the "
                "column mapping cannot be trusted." % (len(cells), len(header), cells[:3]))
        reason = cells[idx["reason"]].strip().strip("`")
        if not reason:
            continue
        cond = (cells[idx[CONDITION_COLUMN]].strip().strip("`")
                if CONDITION_COLUMN in idx else "Ready")
        e = rows.setdefault((cond, reason), {"statuses": set(), "verdicts": set()})
        e["statuses"].add(cells[idx["status"]].strip())
        if VERDICT_COLUMN in idx:
            v = cells[idx[VERDICT_COLUMN]].strip()
            if v and v not in ("-", "--", "\u2014", "—"):
                e["verdicts"].add(v)
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
    """Broken-rule sentences; [] when the vendored copy agrees with the table.

    READY rows are checked against the converged/converging/failed/unevaluated lists via the parent
    verdict. Rows for the OTHER conditions are checked against condition_types[<cond>], because the
    rollup reads only Ready and those rows carry no verdict."""
    bad = []
    mine = classify(vendored)
    ctypes = vendored.get("condition_types") or {}
    up_ready = {r for (c, r) in upstream_rows if c == "Ready"}

    for r in sorted(up_ready - set(mine)):
        v = sorted(upstream_rows[("Ready", r)]["verdicts"])
        bad.append("MISSING: upstream has Ready reason %r (verdict %s) and the vendored file does "
                   "not classify it. A verification built from this file would treat it as unknown "
                   "-- and if its verdict is healthy, would report a FAILURE on a healthy cluster."
                   % (r, v))
    for r in sorted(set(mine) - up_ready):
        bad.append("STALE: the vendored file classifies Ready reason %r, which upstream no longer "
                   "lists. A classifier naming a reason that cannot be written hides the real set."
                   % r)
    for r in sorted(up_ready & set(mine)):
        want = {VERDICT_TO_CLASS.get(v) for v in upstream_rows[("Ready", r)]["verdicts"]}
        want.discard(None)
        if not want:
            bad.append("UNMAPPED VERDICT: Ready reason %r carries verdict(s) %s, which this gate "
                       "does not know how to map. VERDICT_TO_CLASS needs it."
                       % (r, sorted(upstream_rows[("Ready", r)]["verdicts"])))
            continue
        if mine[r] not in want:
            bad.append("MISCLASSIFIED: Ready reason %r is %r here but upstream's parent verdict %s "
                       "maps to %s." % (r, mine[r],
                                        sorted(upstream_rows[("Ready", r)]["verdicts"]),
                                        sorted(want)))

    for (cond, reason) in sorted(k for k in upstream_rows if k[0] != "Ready"):
        if cond in OPEN_ROW_CONDITIONS:
            continue
        entry = ctypes.get(cond)
        if entry is None:
            bad.append("MISSING CONDITION: upstream lists condition %r (e.g. reason %r) and the "
                       "vendored file has no condition_types entry for it." % (cond, reason))
            continue
        known = set(entry.get("healthy") or []) | set(entry.get("unhealthy") or [])
        if reason not in known:
            bad.append("MISSING: upstream has %s reason %r and the vendored file does not list it "
                       "under condition_types[%r]." % (cond, reason, cond))

    for r in sorted(set(vendored.get("requires_synced_true") or []) - set(mine)):
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
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [], "condition_types": {}}, False),
    ("THE REAL BUG: a healthy reason absent from the vendored file is REFUSED",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("Ready", "ReconcileGracefullyPaused"): {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [], "condition_types": {}}, True),
    ("a converging reason filed as converged is REFUSED",
     {("Ready", "Creating"): {"statuses": {"False"}, "verdicts": {"converging"}}},
     {"converged": ["Creating"], "converging": [], "failed": [], "unevaluated": [], "condition_types": {}}, True),
    ("a failed reason filed as converging is REFUSED",
     {("Ready", "Unavailable"): {"statuses": {"False"}, "verdicts": {"failed"}}},
     {"converged": [], "converging": ["Unavailable"], "failed": [], "unevaluated": [], "condition_types": {}}, True),
    ("a vendored reason upstream dropped is REFUSED",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}}},
     {"converged": ["Available", "GoneForever"], "converging": [], "failed": [],
      "unevaluated": [], "condition_types": {}}, True),
    ("THE DELIBERATE DIFFERENCE is accepted: unevaluated maps to its own class",
     {("Ready", "ObserveFailed"): {"statuses": {"Unknown"},
                        "verdicts": {"unevaluated (rolls up as healthy)"}}},
     {"converged": [], "converging": [], "failed": [], "unevaluated": ["ObserveFailed"], "condition_types": {}}, False),
    ("a NEW upstream verdict value is REFUSED rather than silently mapped",
     {("Ready", "Whatever"): {"statuses": {"False"}, "verdicts": {"brand-new-verdict"}}},
     {"converged": ["Whatever"], "converging": [], "failed": [], "unevaluated": [], "condition_types": {}}, True),
    ("a reason on TWO rows (cdc + runtime) with one verdict is accepted",
     {("Ready", "Creating"): {"statuses": {"False"}, "verdicts": {"converging"}}},
     {"converged": [], "converging": ["Creating"], "failed": [], "unevaluated": [], "condition_types": {}}, False),
    # --- the #529 condition-column paths. Each of these is a NEW code path and none of the cases
    # above exercises it, so without them the rework would be unfalsified.
    ("SAME reason under TWO conditions is NOT collapsed",
     {("Ready", "ReconcilePaused"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("Synced", "ReconcilePaused"): {"statuses": {"False"}, "verdicts": set()}},
     {"converged": ["ReconcilePaused"], "converging": [], "failed": [], "unevaluated": [],
      "condition_types": {"Synced": {"healthy": ["ReconcileSuccess"],
                                     "unhealthy": ["ReconcilePaused"]}}}, False),
    ("an UNLISTED non-Ready reason is REFUSED",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("RBACComplete", "SomethingNew"): {"statuses": {"False"}, "verdicts": set()}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [],
      "condition_types": {"RBACComplete": {"healthy": ["Applied"]}}}, True),
    ("a WHOLE unlisted condition is REFUSED",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("BrandNewCondition", "Whatever"): {"statuses": {"True"}, "verdicts": set()}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [],
      "condition_types": {}}, True),
    ("ApiResolved's OPEN family: an unlisted reason is ACCEPTED, not refused",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("ApiResolved", "AudienceMismatch"): {"statuses": {"False"}, "verdicts": set()}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [],
      "condition_types": {"ApiResolved": {"healthy": ["Resolved"]}}}, False),
    ("a non-Ready row does NOT pollute the Ready classification",
     {("Ready", "Available"): {"statuses": {"True"}, "verdicts": {"healthy"}},
      ("Synced", "ReconcileError"): {"statuses": {"False"}, "verdicts": set()}},
     {"converged": ["Available"], "converging": [], "failed": [], "unevaluated": [],
      "condition_types": {"Synced": {"healthy": ["ReconcileSuccess"],
                                     "unhealthy": ["ReconcileError"]}}}, False),
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
