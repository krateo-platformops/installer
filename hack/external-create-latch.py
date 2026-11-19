#!/usr/bin/env python3
"""Find CRs stranded by oasgen #818: a create that answered 404 stamped
`krateo.io/external-create-pending` and nothing will ever clear it.

WHY THIS IS NOT A CONDITION CHECK. The stranded CR reports
`Ready=False/CreateOutcomeUnknown`, which roll-snapshot already surfaces -- but a condition says
the state, not what will change it. The remedy here is a HUMAN ACTION, per object, and which
action depends on the kind. A finding whose fix is manual must say so, or it reads as something
that will clear itself on the next reconcile. It will not.

THE PREDICATE IS THE RUNTIME'S, NOT A PARAPHRASE. unstructured-runtime pkg/meta/meta.go:

    func ExternalCreateIncomplete(o metav1.Object) bool {
        pending := GetExternalCreatePending(o)
        if pending.IsZero() { return false }
        latest := GetExternalCreateSucceeded(o)
        for _, t := range []time.Time{GetExternalCreateFailed(o), GetExternalCreateNotRequested(o)} {
            if t.After(latest) { latest = t }
        }
        return pending.After(latest)
    }

So: `pending` STRICTLY newer than max(succeeded, failed, not-requested).

THREE WAYS TO GET THIS WRONG, all of which I was handed or nearly shipped:

  comparing against `succeeded` ALONE
      oasgen's first version of this predicate did. The counterexample is on krateo-057:
      repositories.github.krateo.io/publish-bp-v1-20261002-1-repo carries pending AND
      external-create-failed at the SAME SECOND, succeeded from 10-02, Ready=True/Available,
      and its id/full_name/html_url in status -- the external repo exists and the CR is healthy.
      Omitting `failed` flags it.

  treating same-second as incomplete
      `pending.After(latest)` is STRICT and the timestamps are second-granular. Same second is
      COMPLETE. The CR above is the live example.

  adding `Ready != True` as a filter
      It looks like a sensible narrowing and it HIDES REAL LATCHES: a latched CR can still read
      Ready from an earlier success. That is the exact inverse of the case above, and the filter
      would suppress precisely the objects this exists to find.

THE REMEDY BRANCHES ON findby, AND GETTING IT WRONG CREATES A DUPLICATE.

  kind WITH a findby     removing krateo.io/external-create-pending is safe: the findby re-adopts
                         the existing object. Measured 52 s to Ready, n=1, on a findby-bearing
                         kind.
  kind WITHOUT a findby  removing it makes the controller POST AGAIN -- measured on 0.76.0 as a
                         re-create in the same second, leaving the original vendor object
                         UNTRACKED. That is the duplicate the latch exists to prevent.
                         Instead: write the vendor id into the field the `get` reads, via the
                         STATUS SUBRESOURCE. ~3 min to Ready, n=1, on 0.76.0.

Both remedies are n=1 and measured on different versions. That is stated in the output, because a
remedy quoted without its n and its version reads as established practice.

ON krateo-057 THE STAKE IS THE WRONG WAY ROUND: 3 of 34 RestDefinitions declare a findby and 31 do
not, so the simple remedy is the WRONG one for 91% of kinds there.

#818 is present on oasgen 0.80.0 and fixed only on unreleased main (#825). It bites a FRESH install
of a parent/child chart -- children POST before the parent exists, take a 404, and latch. A
long-lived cluster is not exposed until the next such install.
"""
import argparse
import datetime
import json
import subprocess
import sys

PENDING = "krateo.io/external-create-pending"
SUCCEEDED = "krateo.io/external-create-succeeded"
FAILED = "krateo.io/external-create-failed"
NOT_REQUESTED = "krateo.io/external-create-not-requested"


def incomplete(ann):
    """The runtime's ExternalCreateIncomplete, as a pure function over an annotations dict.

    Returns (verdict, unparseable) -- unparseable is a list of (key, value) that could not be read
    as a timestamp. An unparseable marker is NEVER silently treated as absent: the caller reports
    it, because "we could not read the clearer" and "there is no clearer" produce opposite verdicts.
    """
    bad = []

    def ts(key):
        """PARSE the marker, never string-compare it.

        This used to require a `Z` suffix and compare lexicographically, which is correct ONLY
        for equal-length values. A FRACTIONAL-SECOND marker is >=20 chars and ends with `Z`, so
        it passed that guard and then compared WRONGLY: `"...:00Z" > "...:00.000001Z"` is True,
        because `Z` (0x5A) sorts after `.` (0x2E). A composition whose `succeeded` carried
        fractional seconds therefore read as STILL LATCHED with bad=[] -- a silent wrong verdict,
        not a reported one. Found 2026-11-19 while answering core-provider's question about
        their move to UTC marker values (core-provider cdc -> unstructured-runtime v1.29.0).

        Parsing also makes a non-UTC OFFSET comparable instead of unparseable, which is the
        point of their change: `+01:00` and `Z` now order correctly against each other rather
        than one of them being discarded.
        """
        v = (ann or {}).get(key)
        if not v:
            return None
        try:
            d = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            bad.append((key, v))
            return None
        # A naive value has no zone; treat it as UTC rather than crashing the comparison.
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d

    pending = ts(PENDING)
    if pending is None:
        return False, bad
    clearers = [d for d in (ts(SUCCEEDED), ts(FAILED), ts(NOT_REQUESTED)) if d is not None]
    if not clearers:
        return True, bad
    return (pending > max(clearers)), bad


def _kinds_from_restdefinitions(ctx):
    """[(group, plural, has_findby)] for every kind an oasgen RestDefinition generates.

    THE PLURAL IS READ FROM THE CRD, NEVER DERIVED. My first version did `kind.lower() + "s"`,
    which produced `repositorys`, `custompropertys`, `deploymentbranchpolicys` and four more that
    do not exist -- 7 of 34 kinds unlistable, and the script correctly refused to call the result
    a clean bill. English pluralisation is not a function of the kind name, and the apiserver
    already knows the answer: spec.names.plural.
    """
    raw = subprocess.run(["kubectl", "--context", ctx, "--request-timeout=60s", "get",
                          "restdefinitions.ogen.krateo.io", "-A", "-o", "json"],
                         capture_output=True, text=True)
    if raw.returncode != 0:
        return None, "listing RestDefinitions: %s" % (raw.stderr or "").strip()[:160]
    wanted = set()
    for rd in json.loads(raw.stdout).get("items", []):
        spec = rd.get("spec") or {}
        group = spec.get("resourceGroup") or spec.get("group")
        kind = (spec.get("resource") or {}).get("kind")
        if group and kind:
            wanted.add((group, kind, "findby" in json.dumps(spec).lower()))
    if not wanted:
        return [], None

    crds = subprocess.run(["kubectl", "--context", ctx, "--request-timeout=60s", "get", "crd",
                           "-o", "json"], capture_output=True, text=True)
    if crds.returncode != 0:
        return None, "listing CRDs for plurals: %s" % (crds.stderr or "").strip()[:160]
    plural = {}
    for c in json.loads(crds.stdout).get("items", []):
        sp = c.get("spec") or {}
        names = sp.get("names") or {}
        if names.get("kind") and names.get("plural"):
            plural[(sp.get("group"), names["kind"])] = names["plural"]

    out, unknown = [], []
    for group, kind, has_findby in sorted(wanted):
        p = plural.get((group, kind))
        if not p:
            unknown.append("%s/%s" % (group, kind))
            continue
        out.append((group, p, has_findby))
    if unknown:
        # Reported, not dropped: a kind whose CRD is absent is a gap in coverage, and a gap
        # read as "nothing to check there" is how an absence becomes a false clean bill.
        return out, "no CRD found for %d kind(s), so they were NOT checked: %s" % (
            len(unknown), unknown[:6])
    return out, None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    a = ap.parse_args(argv)

    kinds, err = _kinds_from_restdefinitions(a.context)
    if kinds is None:
        print("::error::external-create-latch: the kind set is UNKNOWN, so this result is not a "
              "clean bill: %s" % err)
        return 2
    partial = bool(err)
    if partial:
        print("  COVERAGE GAP: %s" % err)
    if not kinds:
        print("external-create-latch: 0 generated kinds on this cluster -- nothing to check, and "
              "nothing is claimed about kinds that are not oasgen-generated.")
        return 0

    findby = {(g, r): f for g, r, f in kinds}
    latched, unparseable, unreadable = [], [], []
    scanned = 0

    for (group, res), has_findby in sorted(findby.items()):
        raw = subprocess.run(["kubectl", "--context", a.context, "--request-timeout=60s", "get",
                              "%s.%s" % (res, group), "-A", "-o", "json"],
                             capture_output=True, text=True)
        if raw.returncode != 0:
            unreadable.append("%s.%s" % (res, group))
            continue
        for o in json.loads(raw.stdout).get("items", []):
            scanned += 1
            m = o.get("metadata") or {}
            verdict, bad = incomplete(m.get("annotations"))
            for k, v in bad:
                unparseable.append(("%s/%s" % (m.get("namespace"), m.get("name")), k, v))
            if verdict:
                latched.append((group, res, m.get("namespace"), m.get("name"), has_findby))

    print("  scanned %d CR(s) across %d generated kind(s)" % (scanned, len(findby)))
    if unreadable:
        print("  could NOT list %d kind(s): %s" % (len(unreadable), unreadable[:5]))
    if unparseable:
        print("  %d UNPARSEABLE marker(s) -- treated as neither present nor absent:" % len(unparseable))
        for n, k, v in unparseable[:5]:
            print("    %s %s=%r" % (n, k, v))

    if not latched:
        ok = not unreadable and not unparseable and not partial
        print()
        print("external-create-latch: 0 latched CR(s)%s" %
              ("" if ok else " among what could be read -- see above, this is NOT a clean bill"))
        return 0 if ok else 2

    print()
    print("::error::external-create-latch: %d CR(s) are STRANDED and will NOT recover on their own "
          "(oasgen #818). Each needs a HUMAN ACTION, and the action differs by kind:" % len(latched))
    for group, res, ns, name, hf in latched:
        print("  %s/%s  (%s.%s)" % (ns, name, res, group))
        if hf:
            print("      kind HAS a findby -> remove the %s annotation; the findby re-adopts the "
                  "existing object. Measured 52 s to Ready, n=1, on a findby-bearing kind." % PENDING)
        else:
            print("      kind has NO findby -> do NOT remove the annotation: the controller would "
                  "POST AGAIN, duplicating the vendor object and leaving the original untracked "
                  "(measured on 0.76.0). Instead write the vendor id into the field the `get` reads, "
                  "via the STATUS SUBRESOURCE. ~3 min to Ready, n=1, on 0.76.0.")
    return 1


def _selftest():
    cases = [
        ("no pending at all", {}, False),
        ("pending only", {PENDING: "2026-11-18T10:00:00Z"}, True),
        ("pending newer than succeeded", {PENDING: "2026-11-18T10:00:01Z",
                                          SUCCEEDED: "2026-11-18T10:00:00Z"}, True),
        ("pending older than succeeded", {PENDING: "2026-11-18T10:00:00Z",
                                          SUCCEEDED: "2026-11-18T10:00:01Z"}, False),
        ("SAME SECOND as succeeded -> complete", {PENDING: "2026-11-18T10:00:00Z",
                                                  SUCCEEDED: "2026-11-18T10:00:00Z"}, False),
        ("THE 057 CASE: same-second FAILED clears it", {PENDING: "2026-11-03T13:11:40Z",
                                                        FAILED: "2026-11-03T13:11:40Z",
                                                        SUCCEEDED: "2026-10-02T09:00:00Z"}, False),
        ("failed alone, older -> still latched", {PENDING: "2026-11-18T10:00:01Z",
                                                  FAILED: "2026-11-18T10:00:00Z"}, True),
        ("not-requested clears it", {PENDING: "2026-11-18T10:00:00Z",
                                     NOT_REQUESTED: "2026-11-18T10:00:01Z"}, False),
        ("not-requested SAME SECOND clears it", {PENDING: "2026-11-18T10:00:00Z",
                                                 NOT_REQUESTED: "2026-11-18T10:00:00Z"}, False),
        # --- REGRESSION, 2026-11-19: these were WRONG under the old lexicographic compare,
        # and SILENTLY so (bad=[]). A fractional-second value is >=20 chars and ends with `Z`,
        # so it passed the length/suffix guard, and then "...:00Z" compared GREATER than
        # "...:00.000001Z" because `Z` (0x5A) sorts after `.` (0x2E). The last three reported a
        # COMPLETED handshake as still latched. ts() now PARSES, so neither length nor offset
        # affects ordering. Found while answering core-provider's question about 2.13.116
        # writing these markers in UTC (cdc -> unstructured-runtime v1.29.0).
        ("fractional pending, truly newer", {PENDING: "2026-11-18T10:00:01.500000Z",
                                             SUCCEEDED: "2026-11-18T10:00:00Z"}, True),
        ("fractional succeeded, truly newer -> complete", {PENDING: "2026-11-18T10:00:00Z",
                                                           SUCCEEDED: "2026-11-18T10:00:00.500000Z"}, False),
        ("fractional succeeded by 1us -> complete", {PENDING: "2026-11-18T10:00:00Z",
                                                     SUCCEEDED: "2026-11-18T10:00:00.000001Z"}, False),
        # An OFFSET value is now COMPARABLE rather than discarded as unparseable. 2.13.116
        # writes UTC, but values written before it keep whatever offset they had.
        ("offset succeeded, truly newer -> complete", {PENDING: "2026-11-18T10:00:00Z",
                                                       SUCCEEDED: "2026-11-18T11:00:01+01:00"}, False),
        ("offset succeeded, truly older -> latched", {PENDING: "2026-11-18T10:00:01Z",
                                                      SUCCEEDED: "2026-11-18T10:00:00+01:00"}, True),
    ]
    fails = 0
    for label, ann, want in cases:
        got, bad = incomplete(ann)
        ok = got == want and not bad
        fails += 0 if ok else 1
        print("  %-4s %-46s -> %-5s (want %s)" % ("ok" if ok else "FAIL", label, got, want))
    # an unparseable marker must be REPORTED, never read as absent
    got, bad = incomplete({PENDING: "2026-11-18T10:00:00Z", SUCCEEDED: "not-a-timestamp"})
    ok = got is True and len(bad) == 1
    fails += 0 if ok else 1
    print("  %-4s %-46s -> latched=%s, %d unparseable reported"
          % ("ok" if ok else "FAIL", "unparseable clearer is REPORTED not ignored", got, len(bad)))
    print()
    print("  %d case(s), %d failure(s)" % (len(cases) + 1, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    sys.exit(main(sys.argv[1:]))
