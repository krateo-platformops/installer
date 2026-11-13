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

# WALL 1 -- the one that actually binds. core-provider writes this schema into a per-version
# ConfigMap, `installers-<version>-jsonschema-configmap`, PRETTY-PRINTED, i.e. essentially this file
# byte-for-byte. A ConfigMap cannot exceed 1 MiB. Measured on krateo-057, 2026-11-13:
#     installers-v0-3-633-jsonschema-configmap   1,041,069 bytes
#     this file at tag 0.3.633                   1,041,058 bytes
# They agree within 11 bytes, so the ON-DISK size of this file is the quantity to guard. The live
# history: 0.3.633 fitted with 7,518 to spare; 0.3.634 was 807 OVER and its ConfigMap was REJECTED,
# which left v0-3-634 wedged in the CRD next to v0-3-633 and only THEN produced the etcd error.
CONFIGMAP_LIMIT = 1_048_576

# WALL 2 -- etcd/apiserver request size, reached only once wall 1 has already wedged a version. A CRD
# carries a full copy of the COMPILED schema per SERVED version; `vacuum` is an unserved storage
# placeholder and cannot absorb the bulk, because a served version must carry its own schema in
# spec.versions[].schema.
APISERVER_LIMIT = 1_572_864
VACUUM_BYTES = 7_947
COMPILE_RATIO = 0.96        # compiled-per-version / minified; measured 0.96 at both 0.3.633 and 0.3.634

# Budget under wall 1. 0.3.633 fitted with 7,518 bytes spare and 0.3.634 exceeded by 807, so the
# usable range is narrow; a budget below the hard limit is what turns a wedged cluster into a
# review-time warning.
BUDGET_BYTES = 1_030_000

# THE DURABLE BUDGET, on the MINIFIED size. core-provider #485 will store the ConfigMap as compact
# JSON (same key, same document, whitespace removed), which retires the on-disk check above: wall 1
# then reads minified-vs-1MiB and 609 KB is comfortable. What remains binding afterwards is wall 2
# under #484, which caps the CRD at "current + one transitional version":
#     minified x COMPILE_RATIO x 2 + VACUUM_BYTES <= APISERVER_LIMIT
#  => minified <= (1_572_864 - 7_947) / (0.96 x 2) ~= 814,000
# so 780,000 keeps ~4% under that with room for an ordinary component node. This check is correct
# both before and after #485; the on-disk one above should be DELETED once a core-provider carrying
# #485 is pinned, and until then it is the one that actually binds.
MINIFIED_BUDGET = 780_000

def projected(minified_bytes: int) -> int:
    """Compiled size of ONE served CRD version, from the minified schema size."""
    return int(minified_bytes * COMPILE_RATIO)


def crd_bytes(per_version: int, n_served: int) -> int:
    return VACUUM_BYTES + per_version * n_served


def verdict(disk_bytes: int, minified_bytes: int, baseline: int = None):
    """(ok, lines) -- pure, so the self-test exercises it without a file."""
    per = projected(minified_bytes)
    over_hard = disk_bytes > CONFIGMAP_LIMIT
    if over_hard and baseline is not None and baseline > CONFIGMAP_LIMIT:
        # main is already over: ratchet, so only growth is refused
        ok_disk = disk_bytes <= baseline
    else:
        ok_disk = disk_bytes <= BUDGET_BYTES
    ok_mini = minified_bytes <= MINIFIED_BUDGET
    ok = ok_disk and ok_mini
    lines = [
        "WALL 1  the per-version schema ConfigMap (the binding one)",
        f"  {SCHEMA} on disk: {disk_bytes:,} bytes",
        f"  hard limit {CONFIGMAP_LIMIT:,}   headroom {CONFIGMAP_LIMIT - disk_bytes:+,}   "
        f"budget {BUDGET_BYTES:,}"
        + ("" if baseline is None else f"   merge target {baseline:,} ({disk_bytes - baseline:+,})"),
        "WALL 2  the CRD object / etcd request",
        f"  minified {minified_bytes:,} -> compiled per served version ~{per:,} (x{COMPILE_RATIO})",
        f"  minified budget {MINIFIED_BUDGET:,} (durable; binds once core-provider #485 ships) "
        f"headroom {MINIFIED_BUDGET - minified_bytes:+,}",
    ]
    for n in (1, 2, 3):
        tot = crd_bytes(per, n)
        lines.append(f"    at {n} served version(s): {tot:>9,}"
                     + ("" if tot <= APISERVER_LIMIT else "  <-- over the apiserver limit"))
    lines.append(f"  hard limit {APISERVER_LIMIT:,}. core-provider ADDS the new version before pruning "
                 f"the old, so a roll transiently needs the n+1 row.")
    return ok, lines


def baseline_disk_bytes():
    """On-disk size of the schema at the merge target, or None if it cannot be read.

    A RATCHET rather than an absolute gate for wall 1, deliberately. main is ALREADY over the 1 MiB
    ConfigMap limit (1,060,819 against 1,048,576 on 2026-11-13), because the schema crossed it at
    tag 0.3.634 and nobody had an instrument that would have said so. An absolute check would
    therefore be red on every pull request from the day it merged, and a permanently red check is one
    people learn to ignore -- the failure mode every other suite in this directory warns about.

    So while main is over the limit this check refuses a change that makes it WORSE, and reports the
    absolute overage loudly on every run. The remedy is core-provider #485, which stores the
    per-version ConfigMap as compact JSON; once a core-provider carrying it is pinned, DELETE the
    wall 1 check entirely and keep MINIFIED_BUDGET, which is the durable one.
    """
    import subprocess
    for ref in ("origin/main", "main"):
        out = subprocess.run(["git", "show", f"{ref}:{SCHEMA}"], capture_output=True)
        if out.returncode == 0 and out.stdout:
            return len(out.stdout)
    return None


def self_test() -> int:
    """The instrument must detect something: both directions, and the measured history."""
    bad = 0

    def case(label, disk, want):
        nonlocal bad
        got, _ = verdict(disk, 600_000)
        if got != want:
            bad += 1
        print(f"  {'ok ' if got == want else 'FAIL'} {label}: {disk:,} ok={got} (wanted {want})")

    case("comfortably under budget", 900_000, True)
    case("just under budget", BUDGET_BYTES - 1, True)
    case("just over budget", BUDGET_BYTES + 1, False)
    case("over the hard ConfigMap limit", CONFIGMAP_LIMIT + 1, False)

    for tag, disk, fits in (("0.3.632", 1_038_979, True), ("0.3.633", 1_041_058, True),
                            ("0.3.634", 1_049_383, False), ("0.3.637", 1_060_819, False)):
        got = disk <= CONFIGMAP_LIMIT
        if got != fits:
            print(f"  FAIL {tag}: hard-limit verdict {got}, the cluster says {fits}")
            bad += 1
        else:
            print(f"  ok  {tag}: {disk:,} vs {CONFIGMAP_LIMIT:,} -> "
                  f"{'fits' if got else 'exceeds'}, matching the cluster")

    # RATCHET: with the merge target already over the hard limit, equal passes and +1 fails
    for label, disk, baseline, want in (
            ("ratchet, unchanged size", 1_060_819, 1_060_819, True),
            ("ratchet, one byte smaller", 1_060_818, 1_060_819, True),
            ("ratchet, one byte larger", 1_060_820, 1_060_819, False)):
        got, _ = verdict(disk, 600_000, baseline)
        if got != want:
            print(f"  FAIL {label}: ok={got} (wanted {want})")
            bad += 1
        else:
            print(f"  ok  {label}: {disk:,} vs target {baseline:,} -> ok={got}")

    two, three = crd_bytes(601_964, 2), crd_bytes(601_964, 3)
    if not (two <= APISERVER_LIMIT < three):
        print(f"  FAIL wall 2 does not reproduce: 2x={two:,} 3x={three:,}")
        bad += 1
    else:
        print(f"  ok  wall 2 reproduces: 2 served={two:,} fits, 3 served={three:,} does not")

    print(f"  self-test: 12 assertions, {bad} failing")
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

    disk = len(open(SCHEMA, "rb").read())
    mini = len(json.dumps(doc, separators=(",", ":")).encode())
    base = baseline_disk_bytes()
    ok, lines = verdict(disk, mini, base)
    if base is not None and base > CONFIGMAP_LIMIT:
        lines.insert(0, "  RATCHET MODE: the merge target is ALREADY over the 1 MiB ConfigMap limit "
                        f"({base:,}), so this check refuses only an INCREASE. Remedy: core-provider "
                        "#485 (compact ConfigMap); delete the wall 1 check once it is pinned.")
    print(f"check-schema-size-budget: {len(props)} component nodes examined")
    for line in lines:
        print(f"  {line}")
    if ok:
        if base is not None and base > CONFIGMAP_LIMIT:
            print(f"check-schema-size-budget: OK — RATCHET: on disk {disk:,} did not grow past the "
                  f"merge target's {base:,}. It is STILL {disk - CONFIGMAP_LIMIT:,} over the hard "
                  f"ConfigMap limit, so no new installer version can be served until "
                  f"core-provider #485 ships.")
        else:
            print(f"check-schema-size-budget: OK — on disk {disk:,} <= {BUDGET_BYTES:,}")
        return 0
    if not (minified_bytes_ok := mini <= MINIFIED_BUDGET):
        print(f"::error::check-schema-size-budget: {SCHEMA} is {mini:,} bytes MINIFIED, over the "
              f"durable budget of {MINIFIED_BUDGET:,}. That budget comes from wall 2 under "
              f"core-provider #484, which caps the CRD at current + one transitional version: "
              f"minified x {COMPILE_RATIO} x 2 + {VACUUM_BYTES:,} must stay under {APISERVER_LIMIT:,}. "
              f"This one does not go away when #485 ships.")
    if disk <= BUDGET_BYTES:
        return 1 if not minified_bytes_ok else 0
    if base is not None and base > CONFIGMAP_LIMIT and disk > base:
        print(f"::error::check-schema-size-budget: {SCHEMA} GREW by {disk - base:,} bytes while the "
              f"merge target is already {base - CONFIGMAP_LIMIT:,} over the 1 MiB ConfigMap limit. "
              f"Every installer version since 0.3.634 has been unservable for this reason, so adding "
              f"to the schema now makes a wedged cluster harder to unwedge. Shrink the addition, or "
              f"wait for core-provider #485 (compact ConfigMap, frees ~43%).")
        return 1
    print(f"::error::check-schema-size-budget: {SCHEMA} is {disk:,} bytes ON DISK, over the "
          f"{BUDGET_BYTES:,} budget (hard ConfigMap limit {CONFIGMAP_LIMIT:,}). core-provider writes "
          f"this file PRETTY-PRINTED into installers-<version>-jsonschema-configmap; a ConfigMap over "
          f"1 MiB is REJECTED, which wedges that version in the CRD and only then produces the etcd "
          f"error. Measured: 0.3.633 fitted, 0.3.634 exceeded by 807 bytes and never became usable. "
          f"Do NOT raise the budget first: drop description text duplicating the chart's own, check "
          f"whether every leaf needs declaring (an undeclared leaf is unsettable, not broken), or ask "
          f"whether the ConfigMap should store the schema MINIFIED, which frees about 43%.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
