#!/usr/bin/env python3
"""CI guard: refuse a values.schema.json whose COMPILED CRD version would leave no headroom.

WHY THIS EXISTS, AND WHAT IT DOES *NOT* CATCH. On 2026-11-13 the installer release on krateo-057
went `failed` with:

    CompositionDefinition installer
      Ready=False  Unavailable     crd for 'composition.krateo.io/v0-3-635' does not exists yet
      Synced=False ReconcileError  error applying CRD update: etcdserver: request is too large

The PROXIMATE cause was not schema size: it was that core-provider's served-version prune had
stopped. The runbook docs/runbooks/crd-version-recovery.md measured 0 of 68 composition CRDs with
more than one served version on 2026-10-27; by 2026-11-13 it was 8 of 73. So this gate would NOT
have prevented that night. It guards the OTHER half of the same ceiling — the half we control.

THE MEASURED ARITHMETIC, from the live CRD on krateo-057 (2026-11-13):

    vacuum    (storage, not served)      7,947 bytes
    v0-3-633  (served)                 596,630 bytes
    v0-3-634  (served)                 601,964 bytes
    CRD total                        1,207,643 bytes

A CRD carries a FULL copy of the compiled schema PER SERVED VERSION. `vacuum` is the storage
version and is tiny (a placeholder with no properties), so the size is essentially
`n_served x per_version`. With the prune working there is one served version and ~620 KB is
comfortable. With two it is 1.21 MB and still fits — which is precisely why nobody noticed. Adding
a third puts the UPDATE REQUEST at ~1.81 MB, past the apiserver/etcd limit of 1.5 MiB, and the
update fails. core-provider adds the new version before pruning the old, so the transient state is
`n+1`.

THE RATIO IS MEASURED, NOT ASSUMED. Compiled per-version size against our own normalised
values.schema.json at the tag that produced it:

    0.3.633   619,398 -> 596,630   (0.96x)
    0.3.634   624,937 -> 601,964   (0.96x)

Two points, same ratio to two decimals. crdgen drops our `title`/`description` text in some places
and adds Kubernetes scaffolding in others, and they very nearly cancel. The ratio is a constant
here rather than a fit: if a future crdgen changes it materially this gate's numbers drift, so
RE-MEASURE it against a live CRD rather than trusting this constant if the projection and reality
ever disagree by more than a few per cent.

WHY A BUDGET ON ONE VERSION RATHER THAN ON n+1. A gate keyed on the three-version projection would
fail TODAY, on every pull request, because 3 x 607 KB is already over the limit — and a gate that
cannot pass teaches people to skip it, which is the failure mode docs/runbooks and the other gates
in this directory keep warning about. So the budget is on the single compiled version, the quantity
a schema change actually moves, and the multi-version arithmetic is PRINTED as context so a reader
can see how close the cliff is.

WHAT TO DO WHEN IT FAILS. Not "raise the budget". Options, cheapest first: drop `description` text
that duplicates the chart's own; check whether the component needs every leaf declared, since an
undeclared leaf is only unsettable and not broken; and for a genuinely large component ask whether
its values belong behind a `registerOnly` entry. Raising BUDGET_BYTES is a decision about how much
headroom to spend, so it needs a reason recorded here next to the number.
"""
import json
import sys

SCHEMA = "chart/values.schema.json"

# Measured on krateo-057's live Installer CRD, 2026-11-13. See the module docstring.
COMPILE_RATIO = 0.96
VACUUM_BYTES = 7_947
APISERVER_LIMIT = 1_572_864          # 1.5 MiB, the etcd/apiserver request ceiling

# Headroom budget for ONE compiled served version. 2026-11-13: the live versions were 596,630 and
# 601,964 bytes and main projected to ~607,500, so 700,000 is about 15% of room — enough for several
# ordinary component nodes and not enough to hide a large one.
BUDGET_BYTES = 700_000


def projected(schema_bytes: int) -> int:
    """Compiled size of ONE served version, from the normalised schema size."""
    return int(schema_bytes * COMPILE_RATIO)


def crd_bytes(per_version: int, n_served: int) -> int:
    return VACUUM_BYTES + per_version * n_served


def verdict(schema_bytes: int):
    """(ok, per_version, lines) — pure, so the self-test can exercise it without a file."""
    per = projected(schema_bytes)
    ok = per <= BUDGET_BYTES
    lines = [
        f"{SCHEMA} normalised: {schema_bytes:,} bytes",
        f"projected compiled per served version: {per:,} bytes "
        f"(x{COMPILE_RATIO} measured) against a budget of {BUDGET_BYTES:,}",
    ]
    for n in (1, 2, 3):
        tot = crd_bytes(per, n)
        flag = "" if tot <= APISERVER_LIMIT else "  <-- EXCEEDS the apiserver limit"
        lines.append(f"  CRD at {n} served version(s): {tot:>9,} bytes{flag}")
    lines.append(
        f"apiserver/etcd limit {APISERVER_LIMIT:,}. core-provider ADDS the new version before "
        f"pruning the old, so a roll transiently needs the n+1 row."
    )
    return ok, per, lines


def self_test() -> int:
    """The instrument must detect something. Both directions, and the boundary."""
    cases = [
        ("a schema well under budget", 400_000, True),
        ("a schema just under budget", int(BUDGET_BYTES / COMPILE_RATIO) - 1_000, True),
        ("a schema just over budget", int(BUDGET_BYTES / COMPILE_RATIO) + 1_000, False),
        ("a schema far over budget", 2_000_000, False),
    ]
    bad = 0
    for label, size, want in cases:
        got, per, _ = verdict(size)
        mark = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"  {mark} {label}: {size:,} -> per-version {per:,}, ok={got} (wanted {want})")
    # and the arithmetic that explains the 057 failure must reproduce it
    per_057 = 601_964
    three = crd_bytes(per_057, 3)
    two = crd_bytes(per_057, 2)
    if not (two <= APISERVER_LIMIT < three):
        print(f"  FAIL the 057 arithmetic does not reproduce: 2x={two:,} 3x={three:,}")
        bad += 1
    else:
        print(f"  ok  reproduces the 057 failure: 2 served={two:,} fits, 3 served={three:,} does not")
    print(f"  self-test: {len(cases) + 1} assertions, {bad} failing")
    return 1 if bad else 0


def main(argv) -> int:
    if "--self-test" in argv:
        return self_test()
    try:
        with open(SCHEMA) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        print(f"::error::check-schema-size-budget: {SCHEMA} not found. This check examined nothing, "
              f"which is not a pass.")
        return 2
    except json.JSONDecodeError as exc:
        print(f"::error::check-schema-size-budget: {SCHEMA} is not valid JSON ({exc}). Nothing was "
              f"measured.")
        return 2

    # A floor: an empty or stub schema must never report a comfortable pass.
    props = (doc.get("properties") or {}).get("componentValues", {}).get("properties") or {}
    if len(props) < 10:
        print(f"::error::check-schema-size-budget: parsed only {len(props)} componentValues "
              f"component(s) from {SCHEMA}. The file was read but yielded almost nothing, so this "
              f"check verified nothing. That is not a pass.")
        return 2

    size = len(json.dumps(doc))
    ok, per, lines = verdict(size)
    print(f"check-schema-size-budget: {len(props)} component nodes examined")
    for line in lines:
        print(f"  {line}")
    if ok:
        print(f"check-schema-size-budget: OK — {per:,} <= {BUDGET_BYTES:,}")
        return 0
    print(f"::error::check-schema-size-budget: the compiled CRD version would be {per:,} bytes, over "
          f"the {BUDGET_BYTES:,} budget. Do NOT raise the budget as the first move: drop description "
          f"text that duplicates the chart's own, check whether every leaf needs declaring (an "
          f"undeclared leaf is unsettable, not broken), or consider registerOnly for a large "
          f"component. Raising BUDGET_BYTES spends headroom, so record the reason next to it.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
