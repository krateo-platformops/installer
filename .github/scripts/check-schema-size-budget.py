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
import os
import re
import subprocess
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

# WALL 1 NOW STATES ITS OWN PRECONDITION rather than assuming it. core-provider #485 stores that
# ConfigMap as COMPACT json (json.Compact: insignificant whitespace removed, key order and number
# literals untouched), which moves the binding quantity from the ON-DISK size of this file to its
# MINIFIED size. Measured on krateo-057 after 2.13.95 rolled: 1,041,075 -> 609,500 bytes for a
# schema of 1,060,819 on disk.
#
# The floor is 2.13.95, established by core-provider from the TAGS rather than from ancestry (#488
# was squash-merged): go/core-provider/internal/tools/deploy/jsonschema.go calls json.Compact at
# 2.13.95 and the file does not exist at 2.13.93 or 2.13.94. They confirmed it is UNCONDITIONAL --
# one call site, deploy.go:514 -> renderJSONSchemaConfigMap, no flag, no values key, no feature
# gate -- so a version check is sufficient and nothing can switch it off.
#
# WHY TIE IT RATHER THAN JUST DELETE IT. The note this replaces said "the on-disk one above should
# be DELETED once a core-provider carrying #485 is pinned". Deleting it would leave the budget
# resting on an assumption nobody re-checks: a later core-provider DOWNGRADE below the floor would
# silently reinstate a 1 MiB ceiling on the on-disk size while this gate kept reporting ~170 KB of
# minified headroom. Tying the two together means the wall comes back by itself.
#
# DO NOT BUDGET ON THE 0.575 COMPACTION RATIO I measured. core-provider's own answer: json.Compact
# removes whitespace and nothing else, so that ratio measures how much indentation THIS file
# happens to carry -- a property of its formatting, not of the compaction. The minified size is the
# quantity to budget, which is what wall 2 already does.
COMPACT_CONFIGMAP_FLOOR = (2, 13, 95)
CHART_YAML = "chart/Chart.yaml"

# WALL 2 -- etcd/apiserver request size, reached only once wall 1 has already wedged a version. A CRD
# carries a full copy of the COMPILED schema per SERVED version; `vacuum` is an unserved storage
# placeholder and cannot absorb the bulk, because a served version must carry its own schema in
# spec.versions[].schema.
#
# THIS WALL AND core-provider's 2.13.85-2.13.94 RETRACTION ARE THE SAME MECHANISM FROM OPPOSITE
# ENDS, and a reader of either should know the other exists. core-provider#484 kept every VACATED
# served version through its grace period, so a definition's CRD could carry a third large schema
# version; they measured the consequence on envtest against real etcd -- a CRD with a third
# ~560 KB-schema version is refused with "etcdserver: request is too large" -- and retracted ten
# releases over it (their retracted-versions.yaml, read by check-retracted-versions.py since
# 2026-11-21). That is the DOWNSTREAM observation. This wall is the UPSTREAM refusal: it declines
# growth that would make the n+1 row exceed APISERVER_LIMIT, which is why the n+1 row is modelled at
# all rather than the current count.
#
# The scale is not hypothetical for this repo. Measured on 057 on 2026-11-21, at ONE served version
# each: installers.composition.krateo.io is 660,805 bytes serialised -- LARGER than the ~560 KB that
# wedged core-provider -- with agentgatewaypolicies at 181,908 and snowplows at 47,291. Nothing on
# that cluster served more than one version at the time, which is the only reason the multiplication
# was not in play.
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

# WALL 3 -- HELM'S OWN RELEASE SECRET, discovered the hard way on 2026-11-14. helm stores EVERY
# release revision in a Secret, and a Secret's data may not exceed 1 MiB DECODED. The release record
# embeds the whole chart, so this ceiling is about the PACKAGE, not the schema. Measured on 057:
#
#     rev 543  1,017,344   headroom +31,232
#     rev 545  1,025,764   headroom +22,812
#     rev 546  1,047,692   headroom    +884      <- deployed, 884 bytes from the ceiling
#     rev 547  REFUSED at chart 0.3.639: "data: Too long: may not be more than 1048576 bytes"
#     rev 547  543,348     headroom +505,228     <- after 0.3.640 stopped packaging the comments
#
# THIS IS A PROXY AND SAYS SO. The exact quantity is the length of base64(gzip(json(release))),
# which needs the rendered manifest and helm's own serialisation. I built that projection and
# CALIBRATED it: it reproduced rev 546's absolute size to +4.54%, and then mis-projected 0.3.640's
# record by +43% (predicted 775,693, actual 543,348), because excluded files leave the gzip stream
# entirely and the manifest shrank too. So the projection is not trustworthy in the mode it would
# be used, and this budget is set from DIRECT ANCHORS instead, with no model:
#
#     published 0.3.638  archive 558,624  ->  record 1,047,692  (+884)       the near miss
#     published 0.3.640  archive 287,648  ->  record   543,348  (+505,228)   measured safe
#     vendored-dependency offset 40,279, measured as published-0.3.640 minus a dep-free build of
#     the same content; a dep-free local build of that content is 247,369
#
# We measure the DEP-FREE archive because a checkout has no chart/charts/ -- the release workflow
# resolves dependencies at package time -- so the dep-free size is the quantity CI can measure
# exactly. The budget sits between the two anchors, nearer the safe end. The near-miss anchor's
# dep-free equivalent is 518,345; the safe one's is 247,369.
#
# RE-MEASURE, DO NOT TRUST THIS FOREVER: the offset is a property of core-provider's chart and will
# drift when that chart grows. And the live release record must still be READ after each roll --
# this wall is what stops a tag being cut, not evidence about a cluster.
ARCHIVE_BUDGET = 350_000
RELEASE_SECRET_LIMIT = 1_048_576
VENDORED_DEP_OFFSET = 40_279

def projected(minified_bytes: int) -> int:
    """Compiled size of ONE served CRD version, from the minified schema size."""
    return int(minified_bytes * COMPILE_RATIO)


def crd_bytes(per_version: int, n_served: int) -> int:
    return VACUUM_BYTES + per_version * n_served


def parse_version(s):
    """(major, minor, patch) from a version string, or None when it is not a plain triple."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(s or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def core_provider_version(text=None):
    """The PINNED core-provider version from chart/Chart.yaml, or None if unreadable.

    WALL 1's mode depends on this, so an unreadable or non-triple version must NOT be read as
    "compaction is on" -- that is the reassuring reading, and it would hand back a wall that is
    actually standing. None means "fall back to the on-disk check", which is the safe direction.
    """
    if text is None:
        try:
            text = open(CHART_YAML, encoding="utf-8").read()
        except OSError:
            return None
    try:
        import yaml
        doc = yaml.safe_load(text) or {}
    except Exception:
        return None
    for d in (doc.get("dependencies") or []):
        if d.get("name") == "core-provider":
            return parse_version(d.get("version"))
    return None


def compaction_active(cp_version):
    """True only when the PINNED core-provider is at or above the #485 floor."""
    return cp_version is not None and cp_version >= COMPACT_CONFIGMAP_FLOOR


def verdict(disk_bytes: int, minified_bytes: int, baseline: int = None,
            cp_version=None, archive_bytes=None):
    """(ok, lines) -- pure, so the self-test exercises it without a file or a cluster."""
    per = projected(minified_bytes)
    compact = compaction_active(cp_version)
    lines = []

    # ---- WALL 1: which quantity the per-version ConfigMap actually holds ----
    if compact:
        ok_wall1 = minified_bytes <= CONFIGMAP_LIMIT
        lines += [
            "WALL 1  the per-version schema ConfigMap — COMPACT, because core-provider %s >= %s"
            % (".".join(map(str, cp_version)), ".".join(map(str, COMPACT_CONFIGMAP_FLOOR))),
            "  stored quantity is the MINIFIED schema: %s bytes" % f"{minified_bytes:,}",
            "  hard limit %s   headroom %s"
            % (f"{CONFIGMAP_LIMIT:,}", f"{CONFIGMAP_LIMIT - minified_bytes:+,}"),
            "  (on disk %s — NOT the binding quantity while compaction is on)" % f"{disk_bytes:,}",
        ]
    else:
        # THE RATCHET RETIRES WITH THIS MODE, and that is the point of tying wall 1 to the pin.
        # The ratchet existed because the schema was already over the hard limit and the only
        # remedy was upstream (#485), so a permanently-red absolute check would have taught people
        # to ignore it. The remedy has SHIPPED. So if compaction is off while this file is over the
        # hard limit, that is now a REFUSAL with an actionable fix -- keep core-provider at or above
        # the floor -- and not a "you did not make it worse" pass. A downgrade must re-raise the
        # wall loudly; absorbing it into the ratchet is exactly the silent invalidation this tie
        # exists to prevent.
        if disk_bytes > CONFIGMAP_LIMIT:
            ok_wall1 = False
        else:
            ok_wall1 = disk_bytes <= BUDGET_BYTES
        why = ("core-provider is pinned %s, BELOW the #485 floor %s"
               % (".".join(map(str, cp_version)) if cp_version else "unreadable",
                  ".".join(map(str, COMPACT_CONFIGMAP_FLOOR))))
        lines += [
            "WALL 1  the per-version schema ConfigMap — PRETTY-PRINTED, because %s" % why,
            "  stored quantity is this file ON DISK: %s bytes" % f"{disk_bytes:,}",
            "  hard limit %s   headroom %s   budget %s"
            % (f"{CONFIGMAP_LIMIT:,}", f"{CONFIGMAP_LIMIT - disk_bytes:+,}", f"{BUDGET_BYTES:,}")
            + ("" if baseline is None else "   merge target %s (%s)"
               % (f"{baseline:,}", f"{disk_bytes - baseline:+,}")),
        ]

    # ---- WALL 2: the CRD object / etcd request ----
    lines += [
        "WALL 2  the CRD object / etcd request",
        "  minified %s -> compiled per served version ~%s (x%s)"
        % (f"{minified_bytes:,}", f"{per:,}", COMPILE_RATIO),
        "  minified budget %s   headroom %s"
        % (f"{MINIFIED_BUDGET:,}", f"{MINIFIED_BUDGET - minified_bytes:+,}"),
    ]
    for n in (1, 2, 3):
        tot = crd_bytes(per, n)
        lines.append("    at %d served version(s): %9s" % (n, f"{tot:,}")
                     + ("" if tot <= APISERVER_LIMIT else "  <-- over the apiserver limit"))
    lines.append("  hard limit %s. core-provider ADDS the new version before pruning the old, so a "
                 "roll transiently needs the n+1 row." % f"{APISERVER_LIMIT:,}")
    ok_mini = minified_bytes <= MINIFIED_BUDGET

    # ---- WALL 3: helm's own release Secret, via the dep-free packaged archive ----
    if archive_bytes is None:
        # NOT MEASURED IS NOT A PASS. An earlier draft of this returned ok while printing "not a
        # pass", which is the same contradiction as a gate that cannot fail: the line said one
        # thing and the exit code said another, and the exit code is what CI reads.
        ok_arch = False
        lines += ["WALL 3  helm's release Secret — NOT MEASURED. This is a REFUSAL, not a skip: "
                  "`helm package` must run for this wall to mean anything, and an unmeasured wall "
                  "reads exactly like a safe one."]
    else:
        ok_arch = archive_bytes <= ARCHIVE_BUDGET
        lines += [
            "WALL 3  helm's release Secret (every revision is stored in one; 1 MiB DECODED)",
            "  dep-free packaged archive: %s bytes   budget %s   headroom %s"
            % (f"{archive_bytes:,}", f"{ARCHIVE_BUDGET:,}", f"{ARCHIVE_BUDGET - archive_bytes:+,}"),
            "  + measured vendored-dependency offset %s -> published archive ~%s"
            % (f"{VENDORED_DEP_OFFSET:,}", f"{archive_bytes + VENDORED_DEP_OFFSET:,}"),
            "  ANCHORS (measured, not modelled): published 558,624 -> record 1,047,692 (+884, the "
            "near miss); published 287,648 -> record 543,348 (+505,228, safe)",
        ]

    return (ok_wall1 and ok_mini and ok_arch), lines


def dep_free_archive_bytes():
    """Bytes of `helm package` on a dependency-free copy of the chart, or (None, reason).

    WHY DEP-FREE. A checkout has no chart/charts/ -- the release workflow resolves dependencies at
    package time -- so a dep-free package is the quantity CI can measure EXACTLY, with no registry
    access and no network. The published archive is this plus a measured offset
    (VENDORED_DEP_OFFSET), and the constant is documented next to the anchors so it can be
    re-measured when core-provider's chart grows.

    WHY NOT PROJECT THE RELEASE RECORD DIRECTLY. Measured: that projection reproduced an absolute
    record size to +4.54% and mis-projected a DELTA by +43%. A proxy whose error is known and
    bounded beats a model whose error is known to be large.
    """
    import shutil
    import tempfile
    if shutil.which("helm") is None:
        return None, ("helm is not on PATH, so wall 3 could not be measured. This is a REFUSAL, "
                      "not a skip: an unmeasured wall reads exactly like a safe one.")
    tmp = tempfile.mkdtemp(prefix="sizebudget-")
    try:
        dst = os.path.join(tmp, "chart")
        shutil.copytree("chart", dst)
        try:
            import yaml
            cy = os.path.join(dst, "Chart.yaml")
            doc = yaml.safe_load(open(cy, encoding="utf-8").read()) or {}
            doc.pop("dependencies", None)
            # CHART_VERSION is a placeholder the release workflow fills; helm package rejects it.
            for k in ("version", "appVersion"):
                if str(doc.get(k)) == "CHART_VERSION":
                    doc[k] = "9.9.9"
            open(cy, "w", encoding="utf-8").write(yaml.safe_dump(doc, default_flow_style=False,
                                                                sort_keys=False))
        except Exception as exc:
            return None, "could not prepare the measurement copy: %s" % exc
        out = os.path.join(tmp, "dist")
        os.makedirs(out, exist_ok=True)
        r = subprocess.run(["helm", "package", dst, "-d", out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, "helm package failed: %s" % (r.stderr or r.stdout).strip()[:300]
        tgz = [f for f in os.listdir(out) if f.endswith(".tgz")]
        if len(tgz) != 1:
            return None, "expected one .tgz, got %d" % len(tgz)
        return os.path.getsize(os.path.join(out, tgz[0])), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    """The instrument must detect something: every wall, both directions, and the measured history."""
    bad = 0
    ON, BELOW = (2, 13, 95), (2, 13, 82)
    SAFE_ARCH = 247_364          # measured: the dep-free archive of main on 2026-11-14

    def case(label, want, **kw):
        nonlocal bad
        a = dict(disk_bytes=900_000, minified_bytes=600_000, baseline=None,
                 cp_version=ON, archive_bytes=SAFE_ARCH)
        a.update(kw)
        got, _ = verdict(**a)
        if got != want:
            bad += 1
        print("  %s %-58s ok=%-5s (wanted %s)" % ("ok " if got == want else "FAIL", label, got, want))

    # ---- WALL 1, compaction ON: the MINIFIED size binds and the on-disk size does not ----
    case("compaction ON: minified comfortably under the ConfigMap limit", True)
    case("compaction ON: on-disk OVER the hard limit is NOT binding", True,
         disk_bytes=CONFIGMAP_LIMIT + 50_000)
    case("compaction ON: on-disk over the OLD budget is NOT binding", True,
         disk_bytes=BUDGET_BYTES + 1)
    case("compaction ON: minified over the ConfigMap limit is refused", False,
         minified_bytes=CONFIGMAP_LIMIT + 1)

    # ---- WALL 1, compaction OFF: the on-disk size binds again, and the ratchet is RETIRED ----
    # This is the whole point of tying wall 1 to the pin. Before, an overage with the merge target
    # also over the limit RATCHETED to a pass, because no remedy existed. The remedy shipped, so a
    # downgrade must now be refused outright rather than absorbed.
    case("DOWNGRADE below the floor, on-disk over the limit: refused", False,
         cp_version=BELOW, disk_bytes=1_060_819, baseline=1_060_819)
    case("DOWNGRADE, and the ratchet no longer rescues an unchanged size", False,
         cp_version=BELOW, disk_bytes=1_060_819, baseline=1_060_819)
    case("one patch BELOW the floor is still off", False,
         cp_version=(2, 13, 94), disk_bytes=1_060_819)
    case("the floor itself is on", True, cp_version=(2, 13, 95), disk_bytes=1_060_819)
    case("above the floor is on", True, cp_version=(2, 13, 96), disk_bytes=1_060_819)
    case("UNREADABLE pin is treated as OFF, not as on", False,
         cp_version=None, disk_bytes=1_060_819)
    case("compaction OFF but on-disk within budget: passes", True,
         cp_version=BELOW, disk_bytes=900_000)

    # ---- WALL 2: unchanged, and still the durable one ----
    case("minified over the durable budget is refused", False,
         minified_bytes=MINIFIED_BUDGET + 1)

    # ---- WALL 3: the release Secret ----
    case("archive under the budget passes", True, archive_bytes=ARCHIVE_BUDGET - 1)
    case("archive over the budget is refused", False, archive_bytes=ARCHIVE_BUDGET + 1)
    case("archive NOT MEASURED is refused, not skipped", False, archive_bytes=None)
    case("the 0.3.638 near-miss archive would have been refused", False,
         archive_bytes=558_624 - VENDORED_DEP_OFFSET)
    case("the 0.3.640 safe archive passes", True,
         archive_bytes=287_648 - VENDORED_DEP_OFFSET)

    # ---- the measured ConfigMap history. These are facts about the PRE-#485 regime and are kept
    # because they are what calibrated the hard limit in the first place.
    for tag, disk, fits in (("0.3.632", 1_038_979, True), ("0.3.633", 1_041_058, True),
                            ("0.3.634", 1_049_383, False), ("0.3.637", 1_060_819, False)):
        got = disk <= CONFIGMAP_LIMIT
        if got != fits:
            print("  FAIL %s: hard-limit verdict %s, the cluster says %s" % (tag, got, fits))
            bad += 1
        else:
            print("  ok  %-9s %s vs %s -> %s, matching the cluster (pre-#485 regime)"
                  % (tag, f"{disk:,}", f"{CONFIGMAP_LIMIT:,}", "fits" if got else "exceeds"))

    two, three = crd_bytes(601_964, 2), crd_bytes(601_964, 3)
    if not (two <= APISERVER_LIMIT < three):
        print("  FAIL wall 2 does not reproduce: 2x=%s 3x=%s" % (f"{two:,}", f"{three:,}"))
        bad += 1
    else:
        print("  ok  wall 2 reproduces: 2 served=%s fits, 3 served=%s does not"
              % (f"{two:,}", f"{three:,}"))

    # version parsing, since wall 1's mode hangs off it
    for s, want in (("2.13.95", (2, 13, 95)), ("2.13.100", (2, 13, 100)), ("2.13", None),
                    ("2.13.95-rc1", None), ("", None), (None, None)):
        got = parse_version(s)
        if got != want:
            print("  FAIL parse_version(%r) = %r, wanted %r" % (s, got, want)); bad += 1
        else:
            print("  ok  parse_version(%-12r) -> %r" % (s, got))

    # and that a Chart.yaml is read for the RIGHT dependency
    y = ("dependencies:\n- name: other\n  version: 9.9.9\n"
         "- name: core-provider\n  version: 2.13.95\n")
    got = core_provider_version(y)
    if got != (2, 13, 95):
        print("  FAIL core_provider_version picked %r from a two-dependency Chart.yaml" % (got,)); bad += 1
    else:
        print("  ok  core_provider_version picks core-provider, not the first dependency")
    if core_provider_version("dependencies:\n- name: other\n  version: 1.0.0\n") is not None:
        print("  FAIL core_provider_version returned something when core-provider is absent"); bad += 1
    else:
        print("  ok  core_provider_version returns None when core-provider is absent (-> OFF)")

    print("  self-test: %d failing" % bad)
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
    if base is None and disk > CONFIGMAP_LIMIT:
        # WITHHELD. Without the merge target we cannot tell "this PR grew the schema" from "the
        # schema was already over", so the ratchet cannot be evaluated. Reporting either verdict
        # would be a guess, and SILENTLY falling back to the absolute budget is what this check did
        # on its first CI run: the ratchet degraded to a stricter test with no line of output saying
        # so, which is the exact instrument failure the rest of this repo's gates warn about.
        print(f"::error::check-schema-size-budget: could not read {SCHEMA} at the merge target "
              f"(origin/main or main), so the RATCHET could not be evaluated, and this file is "
              f"{disk - CONFIGMAP_LIMIT:,} bytes over the hard ConfigMap limit. This is WITHHELD, not "
              f"a verdict. The workflow must fetch main before running this check — see "
              f"check-schema-size-budget.yaml.")
        return 2
    cp = core_provider_version()
    arch, arch_why = dep_free_archive_bytes()
    ok, lines = verdict(disk, mini, base, cp_version=cp, archive_bytes=arch)
    if arch is None:
        lines.append("  reason: %s" % arch_why)
    print(f"check-schema-size-budget: {len(props)} component nodes examined")
    for line in lines:
        print(f"  {line}")
    if ok:
        print(f"check-schema-size-budget: OK — wall 1 on the MINIFIED schema ({mini:,} <= "
              f"{CONFIGMAP_LIMIT:,}) because core-provider "
              f"{'.'.join(map(str, cp))} carries #485; wall 2 minified <= {MINIFIED_BUDGET:,}; "
              f"wall 3 dep-free archive {arch:,} <= {ARCHIVE_BUDGET:,}")
        return 0
    if arch is None:
        print(f"::error::check-schema-size-budget: wall 3 was NOT MEASURED — {arch_why}")
        return 2
    if not compaction_active(cp) and disk > CONFIGMAP_LIMIT:
        print(f"::error::check-schema-size-budget: core-provider is pinned "
              f"{'.'.join(map(str, cp)) if cp else 'unreadably'}, BELOW the #485 floor "
              f"{'.'.join(map(str, COMPACT_CONFIGMAP_FLOOR))}, so the per-version ConfigMap would be "
              f"stored PRETTY-PRINTED — and this schema is {disk:,} bytes on disk, "
              f"{disk - CONFIGMAP_LIMIT:,} over the 1 MiB ConfigMap limit. That ConfigMap would be "
              f"REJECTED and the version would wedge in the CRD, which is exactly what happened at "
              f"0.3.634 and cost krateo-057 a day. The remedy is NOT to shrink the schema: keep "
              f"core-provider at or above {'.'.join(map(str, COMPACT_CONFIGMAP_FLOOR))}. This check "
              f"used to RATCHET here, tolerating the overage because no remedy existed; the remedy "
              f"has shipped, so a downgrade is now a refusal.")
        return 1
    if arch > ARCHIVE_BUDGET:
        print(f"::error::check-schema-size-budget: the dep-free packaged archive is {arch:,} bytes, "
              f"over the {ARCHIVE_BUDGET:,} budget. helm stores EVERY release revision in a Secret "
              f"whose data may not exceed {RELEASE_SECRET_LIMIT:,} bytes DECODED, and the release "
              f"record embeds the whole chart. Measured anchors: a published archive of 558,624 "
              f"produced a record of 1,047,692 — 884 bytes from the ceiling — and the next revision "
              f"was REFUSED outright with \"data: Too long\". A published archive of 287,648 produced "
              f"543,348. Do not raise this budget to get a tag out: find what grew. On 2026-11-14 it "
              f"was 722,800 bytes of comment prose in files/component-pins.yaml, which is now "
              f".helmignore'd in favour of the generated data-only copy.")
        return 1
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
