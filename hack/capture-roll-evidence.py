#!/usr/bin/env python3
"""Capture the evidence a composition-version-bump render race destroys.

WHY THIS EXISTS. An installer roll that bumps any component's composition version makes the umbrella
flash `Synced=False/ReconcileError` for one to three reconciles and then heal (measured 4x on
2026-11-11, both clusters, four tags). The errors live in the PER-VERSION controller's log -- and
that pod is deleted by the next version bump, so the evidence is gone within minutes. Of four
occurrences in one afternoon I could produce verbatim text for exactly one; the other three were
summarised from a live read, and a summary was not what core-provider needed.

It also settles a caveat they could not close from their side. Correlating an error against "the last
spec write by core-provider on that CRD" only shows the write is CONSISTENT with the version being
added. Snapshotting each CRD's served versions BEFORE and AFTER turns that into a measurement: the
diff names exactly which versions were added, and managedFields gives when.

WHAT IT CAPTURES
  pre/<kind>.json    every composition.krateo.io CRD's served versions + managedFields times, before
  errors.jsonl       every log line carrying an `err`, from every installers-v* controller, VERBATIM,
                     with the pod name and the raw UTC timestamp
  post/<kind>.json   the same CRD snapshot, after
  report.txt         the version-set diff, and every error correlated to a version add

USAGE
  python3 hack/capture-roll-evidence.py --context <ctx> --out <dir> pre
  # ... run the helm upgrade ...
  python3 hack/capture-roll-evidence.py --context <ctx> --out <dir> watch --seconds 600 --interval 3
  python3 hack/capture-roll-evidence.py --context <ctx> --out <dir> post

TIMESTAMPS ARE UTC AND THAT MATTERS. The cdc's log `timestamp` field ends in Z and agrees with
`date -u` to the second; k8s managedFields times are UTC. Helm's own history on the same cluster
carries a LOCAL offset (+01:00 observed), so never correlate a cdc error against a helm history time
without converting -- an hour of skew inverts the ordering conclusion and that nearly happened.
"""
import argparse, json, os, subprocess, sys, time, datetime

GROUP = "composition.krateo.io"


def kubectl(ctx, *args, check=True):
    cmd = ["kubectl", "--context", ctx] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"::error::kubectl {' '.join(args)[:80]} failed: {(r.stderr or '').strip()[:200]}")
        sys.exit(1)
    return r.stdout


def snapshot(ctx, outdir, phase):
    d = os.path.join(outdir, phase)
    os.makedirs(d, exist_ok=True)
    # STAMP THE WINDOW. The watch dumps each pod's WHOLE log on purpose (the errors we want happen
    # in a pod's first seconds and we may attach late), so it also picks up errors that were already
    # there from an EARLIER roll. Measured on the 0.3.588 roll: the first two lines captured were
    # from 17:27/17:28, the previous roll, while this one started at 18:13 -- and without a window
    # the report would have correlated stale errors against this roll's version diff and read as
    # five fresh occurrences. The pre phase stamps when the window opened; report() uses it.
    if phase == "pre":
        with open(os.path.join(outdir, "window.json"), "w") as f:
            json.dump({"preAt": datetime.datetime.now(datetime.timezone.utc)
                       .isoformat().replace("+00:00", "Z")}, f)
    # --show-managed-fields IS REQUIRED. kubectl strips managedFields from `get -o json` by
    # default, so without it every snapshot carries an empty managedFields and the correlation in
    # report() silently reports "NO version add names this kind" for every error. Caught by the
    # floor assertion below on the first smoke test: 0 of 72 CRDs had managedFields.
    raw = kubectl(ctx, "get", "crd", "-o", "json", "--show-managed-fields")
    crds = json.loads(raw).get("items", [])
    kept = 0
    with_mf = 0
    for c in crds:
        if c["spec"].get("group") != GROUP:
            continue
        name = c["metadata"]["name"]
        rec = {
            "name": name,
            "kind": c["spec"]["names"]["kind"],
            "capturedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "servedVersions": sorted(v["name"] for v in c["spec"]["versions"] if v.get("served")),
            "allVersions": sorted(v["name"] for v in c["spec"]["versions"]),
            "storageVersion": next((v["name"] for v in c["spec"]["versions"] if v.get("storage")), None),
            "managedFields": [
                {"manager": m.get("manager"), "operation": m.get("operation"), "time": m.get("time")}
                for m in (c["metadata"].get("managedFields") or [])
            ],
        }
        with open(os.path.join(d, name + ".json"), "w") as f:
            json.dump(rec, f, indent=2, sort_keys=True)
        kept += 1
        if rec["managedFields"]:
            with_mf += 1
    # FLOOR: a snapshot of nothing is not a snapshot. Without this the diff later reports
    # "0 versions added" over an empty set and reads as "nothing happened".
    if kept == 0:
        print(f"::error::capture: snapshotted ZERO {GROUP} CRDs. The cluster was read but yielded "
              f"nothing, so this phase recorded no baseline. That is not a successful capture.")
        sys.exit(1)
    # SECOND FLOOR, on the field the correlation actually depends on. A snapshot can have every CRD
    # and still be useless if managedFields came back empty -- which is exactly what happened
    # before --show-managed-fields was added. "72 CRDs captured" looked like success.
    if with_mf == 0:
        print(f"::error::capture: {kept} CRDs snapshotted but NONE carry managedFields, so the "
              f"error-to-version-add correlation cannot work. Check that "
              f"--show-managed-fields is being passed.")
        sys.exit(1)
    print(f"  {phase}: {kept} {GROUP} CRDs snapshotted into {d} "
          f"({with_mf} with managedFields)")
    return kept


def _discovery(ctx, version):
    """The resource plurals the apiserver lists under ONE group version.

    THIS, NOT THE GROUP ENDPOINT, IS THE MAPPER'S PRECONDITION. core-provider first proposed
    /apis/composition.krateo.io, then found and corrected the flaw from their own side: that endpoint
    lists a version when ANY kind in the group serves it, and on 057 v1-8-109 is served by 18 page
    kinds at once (servicedeskpages, dashboardpages, searchpages, ... blueprintspages). So the
    version can appear the instant the FIRST of them is published while BuildersPage is still not
    mappable, and a group-level check would read as "no lag" for exactly the kinds that lagged.
    helm resolves a KIND AT A VERSION, so the question has to be asked per plural per version.

    Calibrated by core-provider on 057: searchpages under v1-8-109 -> listed; installers under
    v1-8-109 -> not listed. sample_state() re-runs that negative control on every poll.

    Returns None when the apiserver does not serve the version at all (`get --raw` 404s). That is
    ABSENT, not a failed probe, and the caller must keep the two apart -- collapsing them would make
    a missing version look like a lagging one.
    """
    raw = kubectl(ctx, "get", "--raw", f"/apis/{GROUP}/{version}", check=False)
    if not raw:
        return None
    try:
        rs = json.loads(raw).get("resources", [])
    except Exception:
        return None
    # Subresources arrive as "searchpages/status" and are not mappable kinds; drop them, or a
    # plural would appear "listed" on the strength of its own status subresource.
    return sorted({r["name"] for r in rs if "/" not in (r.get("name") or "")})


def _pre_served(outdir):
    """kind -> set(served versions) as of the pre snapshot, used to decide what is NEW this roll."""
    d, out = os.path.join(outdir, "pre"), {}
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        try:
            r = json.load(open(os.path.join(d, fn)))
        except Exception:
            continue
        out[r["kind"]] = set(r.get("servedVersions") or [])
    return out


def sample_state(ctx, outdir, pre_served):
    """Append a timestamped sample for every composition.krateo.io CRD.

    WHY PER-POLL AND NOT JUST pre/post. A pre-snapshot bounds absence at ONE instant, and I asserted
    a conclusion it could not carry: that an error naming v1-8-109 at 18:15:20 proved the version
    was not yet added, when all the pre-snapshot showed was absence at 18:12:19. core-provider had
    already warned me that managedFields keeps only the NEWEST write per manager, so it cannot date
    a specific event -- and I used it as if it could, twice.

    What settles it is the served set AT the error's instant. compositions.yaml:37 does a LIVE
    lookup of all CRDs in the same helm action whose post-render later fails, so the two are seconds
    apart; sampling every poll brackets any error within one interval.

    AND THE APISERVER SIDE, IN THE SAME SAMPLE. The CRD's served list is what core-provider WROTE;
    discovery is what the apiserver SERVES. A poll where the CRD serves V for a plural but that
    version's resource list omits the plural is apiserver lag, measured directly -- and if discovery
    always lists the plural by the first poll its CRD does, lag is ruled out at this resolution and
    the misses point back at the cdc's own cache instead. Both reads go in ONE record so they cannot
    be paired across different instants.
    """
    path = os.path.join(outdir, "served-timeline.jsonl")
    raw = kubectl(ctx, "get", "crd", "-o", "json", check=False)
    if not raw:
        return 0
    try:
        items = [c for c in json.loads(raw).get("items", []) if c["spec"].get("group") == GROUP]
    except Exception:
        return 0
    if not items:
        return 0
    served = {c["spec"]["names"]["kind"]: sorted(v["name"] for v in c["spec"]["versions"]
                                                 if v.get("served")) for c in items}
    plural = {c["spec"]["names"]["kind"]: c["spec"]["names"]["plural"] for c in items}

    # Only the versions this roll CHANGES get a discovery GET -- one per version, which is
    # core-provider's stated budget. Probing every served version would be hundreds of GETs a poll.
    #
    # BOTH DIRECTIONS, and the removed side is the one that matters for the prune race. The first
    # version of this probed only versions being ADDED, so when core-provider asked whether a PRUNED
    # version was still in discovery at the moment a render failed on it, I had no data at all --
    # the interesting versions were precisely the ones I had excluded. `added` answers "is a new
    # version mappable yet"; `removed` answers "is a vacated version still mappable", which is the
    # precondition of the emit-then-prune failure.
    added = {v for k, vs in served.items() for v in vs if v not in (pre_served.get(k) or set())}
    removed = {v for k, vs in pre_served.items() for v in vs if v not in set(served.get(k) or [])}
    interest = sorted(added | removed)
    kindOf = {v: ("both" if v in added and v in removed
                  else "added" if v in added else "removed") for v in interest}
    disc = {v: _discovery(ctx, v) for v in interest}

    # NEGATIVE CONTROL, free -- it reuses the GET already made. For each probed version, a plural
    # whose own CRD does NOT serve that version must be ABSENT from the list. If it turns up
    # present, the probe cannot discriminate and every "listed" verdict this poll is vacuous.
    #
    # DELIBERATELY NOT RUN FOR A REMOVED-ONLY VERSION. Once a version is vacated, NO CRD serves it,
    # so "a plural whose CRD does not serve this version is absent from the list" is satisfied by
    # every plural and by an empty or 404 list -- it would pass without testing anything. A control
    # that cannot fail is worse than no control, because it is reported as if it had discriminated.
    # Those versions are marked controlNotApplicable so the report never counts them as passing.
    control = {}
    for v, lst in disc.items():
        if lst is None:
            continue
        if kindOf.get(v) == "removed":
            control[v] = {"controlNotApplicable": True,
                          "why": "no CRD serves a vacated version, so a negative control is vacuous"}
            continue
        neg = next((plural[k] for k, vs in sorted(served.items()) if v not in vs and plural.get(k)),
                   None)
        if neg:
            control[v] = {"negativePlural": neg, "absentAsExpected": neg not in lst}
    with open(path, "a") as f:
        f.write(json.dumps({
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "served": served, "plurals": plural, "probed": interest,
            "probedKind": kindOf, "discovery": disc, "control": control,
        }) + "\n")
    return len(served)


def watch(ctx, outdir, seconds, interval):
    """Follow every installers-v* controller and append any line carrying an `err`, verbatim."""
    path = os.path.join(outdir, "errors.jsonl")
    seen_pods, n = set(), 0
    emitted = set()   # (pod, timestamp, err-prefix) -> written already
    # FLOOR BEFORE THE FIRST POLL. Without pre/ there is no baseline, so EVERY served version counts
    # as new and the discovery probe would fire one GET per version per poll -- hundreds. Refuse
    # rather than quietly probe the wrong set.
    pre_served = _pre_served(outdir)
    if not pre_served:
        print("::error::capture: no pre/ snapshot in this out dir, so there is no baseline for "
              "'which versions are new'. Run the pre phase before the helm upgrade.")
        sys.exit(1)
    deadline = time.time() + seconds
    print(f"  watching for {seconds}s at {interval}s intervals -> {path}")
    print(f"  baseline: {len(pre_served)} kinds from pre/")
    with open(path, "a") as out:
        while time.time() < deadline:
            pods = [l.split("/", 1)[1] for l in
                    kubectl(ctx, "get", "pods", "-n", "krateo-system", "-o", "name", check=False).split()
                    if "installers-v" in l]
            for p in pods:
                # RE-READ EVERY POLL, not once per pod. The first version read a pod's log when it
                # was first DISCOVERED and never again -- so an error appearing after that first
                # read was never captured. Measured on the 0.3.588 roll: the tool reported 0
                # in-window errors while the Installer CR was visibly Synced=False, and the two real
                # errors (18:15:20 and 18:16:02) had to be pulled by hand. The contradiction between
                # the CR and the tool was the only tell. Dedup below makes re-reading cheap and
                # idempotent.
                seen_pods.add(p)
                # --since-time is deliberately omitted: we want the pod's WHOLE log, because the
                # errors we care about happen in its first seconds and we may attach late.
                log = kubectl(ctx, "logs", "-n", "krateo-system", p, check=False)
                for ln in log.split("\n"):
                    if '"err"' not in ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue
                    if not d.get("err"):
                        continue
                    key = (p, d.get("timestamp"), str(d.get("err"))[:200])
                    if key in emitted:
                        continue
                    emitted.add(key)
                    out.write(json.dumps({"pod": p, "timestamp": d.get("timestamp"),
                                          "msg": d.get("msg"), "err": d.get("err")}) + "\n")
                    out.flush()
                    n += 1
            sample_state(ctx, outdir, pre_served)
            time.sleep(interval)
    tl = os.path.join(outdir, "served-timeline.jsonl")
    samples = [json.loads(l) for l in open(tl)] if os.path.exists(tl) else []
    probed = sorted({v for s in samples for v in (s.get("probed") or [])})
    bad = [(s["at"], v, c) for s in samples for v, c in (s.get("control") or {}).items()
           if not c.get("controlNotApplicable") and not c.get("absentAsExpected")]
    print(f"  captured {n} error line(s) from {len(seen_pods)} pod(s); {len(samples)} sample(s)")
    kinds = {}
    for s_ in samples:
        for v, kv in (s_.get("probedKind") or {}).items(): kinds[v] = kv
    print("  versions probed in discovery: " + (", ".join(f"{v}({kinds.get(v,'?')})" for v in probed)
                                                if probed else "(none -- no version was added or vacated)"))
    if bad:
        print(f"::error::capture: the discovery negative control FAILED on {len(bad)} sample(s) "
              f"(first: {bad[0][0]} version={bad[0][1]} {bad[0][2]}). A plural whose CRD does not "
              f"serve that version was listed under it, so the probe cannot discriminate and the "
              f"lag table must not be believed.")
        return n
    # DO NOT CLAIM A CONTROL PASSED WHEN THERE WAS NONE TO RUN. A vacated version 404s, so the
    # control loop never produces an entry for it, and the old wording printed "passed on every
    # sample" over an EMPTY control set -- the exact vacuous reassurance this control exists to
    # prevent, in my own output. Count the applicable entries and say which case it is.
    applicable = sum(1 for s_ in samples
                     for c in (s_.get("control") or {}).values()
                     if not c.get("controlNotApplicable") and "absentAsExpected" in c)
    na = sum(1 for s_ in samples
             for c in (s_.get("control") or {}).values() if c.get("controlNotApplicable"))
    if applicable:
        print(f"  discovery negative control passed on all {applicable} applicable sample-checks"
              + (f"; {na} skipped as not applicable (vacated versions)" if na else ""))
    elif probed:
        print(f"  discovery negative control: NO applicable checks ran"
              f"{' (' + str(na) + ' skipped: vacated versions, where a negative control is vacuous)' if na else ''}"
              f" -- the 'listed' verdicts rest on prior calibration, not on a per-poll control")
    return n


def report(outdir):
    pre, post = os.path.join(outdir, "pre"), os.path.join(outdir, "post")
    if not (os.path.isdir(pre) and os.path.isdir(post)):
        print("::error::capture: need both pre/ and post/ snapshots to report")
        return 1
    lines, added_index, removed_index = [], {}, {}
    for fn in sorted(os.listdir(pre)):
        if not fn.endswith(".json") or not os.path.exists(os.path.join(post, fn)):
            continue
        a = json.load(open(os.path.join(pre, fn)))
        b = json.load(open(os.path.join(post, fn)))
        add = sorted(set(b["servedVersions"]) - set(a["servedVersions"]))
        rem = sorted(set(a["servedVersions"]) - set(b["servedVersions"]))
        if not (add or rem):
            continue
        # DO NOT filter on a manager NAME. core-provider's client writes with the DEFAULT field
        # manager, which appears literally as "manager" -- filtering on "core-provider" matched
        # nothing and the report said "?" for every CRD. Measured on 057: incidentagents carries
        # managers [kube-apiserver, manager, manager], and the newest `manager` write at
        # 2026-11-11T17:27:27Z is the version add. So: take the newest write by anything that is
        # not the apiserver, and label it as what it is -- a write by a controller, not provably
        # the version add.
        t = sorted(m["time"] for m in b["managedFields"]
                   if m.get("manager") != "kube-apiserver" and m.get("time"))
        lines.append(f"{b['kind']:26s} added={add or '-'} removed={rem or '-'} "
                     f"newest non-apiserver write={t[-1] if t else '?'}")
        for v in add:
            added_index[(b["kind"], v)] = t[-1] if t else None  # newest controller write
        for v in rem:
            removed_index[(b["kind"], v)] = t[-1] if t else None
    errf = os.path.join(outdir, "errors.jsonl")
    errs = [json.loads(l) for l in open(errf)] if os.path.exists(errf) else []
    win = None
    wf = os.path.join(outdir, "window.json")
    if os.path.exists(wf):
        win = json.load(open(wf)).get("preAt")
    fresh = [e for e in errs if win and str(e.get("timestamp") or "") >= win]
    stale = [e for e in errs if not (win and str(e.get("timestamp") or "") >= win)]
    out = ["VERSION-SET DIFF (pre -> post)", ""]
    out += ["  " + l for l in lines] or ["  (no served-version changes)"]
    out += ["", f"ROLL WINDOW OPENED: {win or 'UNKNOWN -- run the pre phase to stamp it'}"]
    out += [f"ERRORS IN THIS WINDOW: {len(fresh)}   PRE-EXISTING IN POD LOGS: {len(stale)}"]
    if stale:
        out += ["", "  (pre-existing, from an earlier roll -- NOT evidence for this one:)"]
        for e in stale:
            out.append(f"    {e['timestamp']}  {str(e['err'])[:100]}")
    tl = os.path.join(outdir, "served-timeline.jsonl")
    timeline = [json.loads(l) for l in open(tl)] if os.path.exists(tl) else []

    def _t(s):
        try:
            return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    # APISERVER LAG. Two instruments that must agree: what core-provider WROTE on the CRD, and what
    # the apiserver SERVES under that version. Per plural per version, because the group endpoint
    # lists a version as soon as any ONE of its 18 page kinds is published -- see _discovery().
    out += ["", "APISERVER LAG: CRD-served vs that version's own discovery list", ""]
    ctl_fail = [(s["at"], v) for s in timeline
                for v, c in (s.get("control") or {}).items()
                if not c.get("controlNotApplicable") and not c.get("absentAsExpected")]
    if not timeline:
        out += ["  (no samples -- run the watch phase across the roll)"]
    elif ctl_fail:
        # WITHHOLD, do not caveat. A table whose probe cannot tell listed from unlisted would be
        # read as evidence of no lag, which is the reassuring direction.
        out += [f"  NEGATIVE CONTROL FAILED on {len(ctl_fail)} sample(s); first {ctl_fail[0]}.",
                "  A plural whose own CRD does not serve that version was listed under it, so the",
                "  probe cannot discriminate. Table withheld deliberately -- fix the probe first."]
    elif not added_index:
        out += ["  (no version was added, so there was nothing to probe)"]
    else:
        probed_any = {v for s in timeline for v in (s.get("probed") or [])}
        for (kind, ver) in sorted(added_index):
            plural = next((s["plurals"].get(kind) for s in timeline
                           if (s.get("plurals") or {}).get(kind)), None)
            crd_at = next((s["at"] for s in timeline
                           if ver in ((s.get("served") or {}).get(kind) or [])), None)
            disc_at = next((s["at"] for s in timeline
                            if plural and isinstance((s.get("discovery") or {}).get(ver), list)
                            and plural in s["discovery"][ver]), None)
            if ver not in probed_any:
                verdict = "NOT PROBED (version was not new relative to pre/)"
            elif crd_at is None:
                verdict = "CRD never observed serving it in-window (add fell outside the samples)"
            elif disc_at is None:
                verdict = "CRD served it but discovery NEVER listed the plural in-window -> LAG"
            else:
                d = (_t(disc_at) - _t(crd_at)).total_seconds() if _t(disc_at) and _t(crd_at) else None
                if d is None:
                    verdict = "timestamps unparseable"
                elif d <= 0:
                    verdict = "no lag at this resolution (listed by the first sample the CRD served it)"
                else:
                    verdict = f"LAG {d:.0f}s (CRD {crd_at} -> discovery {disc_at})"
            out.append(f"  {kind}/{ver}  plural={plural or '?'}")
            out.append(f"      {verdict}")
        out += ["", f"  samples: {len(timeline)}; negative control passed on all of them"]

    # PRUNE LAG, the mirror of the table above and the one that bears on the emit-then-prune
    # failure: once core-provider vacates a version, is it still listed in discovery? A render whose
    # CRD snapshot still showed the version will try to map it, and whether that succeeds depends on
    # discovery, not on the CRD. A gap here is the window.
    out += ["", "PRUNE LAG: CRD stopped serving vs discovery stopped listing", ""]
    if not timeline:
        out += ["  (no samples -- run the watch phase across the roll)"]
    elif not removed_index:
        out += ["  (no version was vacated in this window)"]
    else:
        for (kind, ver) in sorted(removed_index):
            plural = next((s["plurals"].get(kind) for s in timeline
                           if (s.get("plurals") or {}).get(kind)), None)
            probed = any(ver in (s.get("probed") or []) for s in timeline)
            crd_gone = next((s["at"] for s in timeline
                             if ver not in ((s.get("served") or {}).get(kind) or [])), None)
            # discovery "stopped listing" = the version 404s (None) or the plural left its list
            disc_gone = next((s["at"] for s in timeline
                              if ver in (s.get("probed") or [])
                              and (not isinstance((s.get("discovery") or {}).get(ver), list)
                                   or (plural and plural not in s["discovery"][ver]))), None)
            if not probed:
                verdict = "NOT PROBED in discovery (it was not new or vacated relative to pre/)"
            elif crd_gone is None:
                verdict = "the CRD never stopped serving it in-window"
            elif disc_gone is None:
                verdict = ("CRD stopped serving it but discovery STILL listed the plural for the "
                           "rest of the window -> a render could still map a vacated version")
            else:
                d = (_t(disc_gone) - _t(crd_gone)).total_seconds() if _t(disc_gone) and _t(crd_gone) else None
                if d is None:
                    verdict = "timestamps unparseable"
                elif d > 0:
                    verdict = f"discovery lagged the CRD by {d:.0f}s (CRD {crd_gone} -> discovery {disc_gone})"
                elif d < 0:
                    verdict = (f"discovery dropped it {abs(d):.0f}s BEFORE the CRD did -- a render "
                               f"holding the older CRD snapshot would fail to map it")
                else:
                    verdict = "same sample; no lag at this resolution"
            out.append(f"  {kind}/{ver}  plural={plural or '?'}")
            out.append(f"      {verdict}")
        na = sorted({v for s in timeline for v, c in (s.get("control") or {}).items()
                     if c.get("controlNotApplicable")})
        if na:
            out += ["", "  NB the negative control is NOT APPLICABLE for a vacated version (%s):" % ", ".join(na),
                    "  no CRD serves it, so the control would pass without testing anything. These",
                    "  verdicts rest on the probe being calibrated elsewhere, not on a per-poll control."]

    out += ["", "ERRORS IN THIS WINDOW, in full:", ""]
    import re as _re

    def bracket(err_ts, err_text):
        """The served set for each kind/version the error names, just before and just after."""
        pairs = _re.findall(r'no matches for kind "([^"]+)" in version "composition\.krateo\.io/([^"]+)"',
                            str(err_text))
        if not pairs or not timeline:
            return ["  (no served-version samples, or the error names no kind/version)"]
        before = [s for s in timeline if s["at"] <= err_ts]
        after = [s for s in timeline if s["at"] > err_ts]
        out = []
        for kind, ver in sorted(set(pairs)):
            b = (f"{before[-1]['at']}: {ver in (before[-1]['served'].get(kind) or [])}"
                 if before else "no sample before")
            a = (f"{after[0]['at']}: {ver in (after[0]['served'].get(kind) or [])}"
                 if after else "no sample after")
            out.append(f"  {kind}/{ver} served? before={b}  after={a}")
        return out

    for e in fresh:
        # NB the label. This is the NEWEST controller write on that CRD, which is NOT the moment
        # the version was added -- managedFields keeps only the latest entry per manager. I read it
        # as the add time twice and sent a peer a wrong conclusion built on it. The SERVED AT ERROR
        # TIME block below is the authoritative one; if the two disagree, believe the samples.
        hit = [f"{k}/{v} in the version diff (newest controller write on that CRD: {t} "
               f"-- NOT the add time)" for (k, v), t in added_index.items()
               if k in str(e["err"]) or v in str(e["err"])]
        out.append(f"  {e['timestamp']}  pod={e['pod']}")
        out.append(f"    msg: {e['msg']}")
        out.append(f"    err: {e['err']}")
        out.append(f"    in the version diff: {hit or 'NO version add names this kind/version'}")
        out.append("    SERVED AT ERROR TIME (this is what decides ordering vs lag):")
        for l in bracket(str(e.get("timestamp") or ""), e.get("err")):
            out.append("    " + l)
        out.append("")
    txt = "\n".join(out)
    open(os.path.join(outdir, "report.txt"), "w").write(txt)
    print(txt)
    print(f"  written to {os.path.join(outdir, 'report.txt')}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["pre", "watch", "post", "report"])
    ap.add_argument("--context", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=int, default=600)
    # 2-3s, not 10s. core-provider measured the misses 6-13s from the CRD write, so a 10s
    # grid can land the add and the miss in the SAME sample and show no lag where there was
    # some. 3s is their recommended floor for the minutes around a version add.
    ap.add_argument("--interval", type=int, default=3)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.phase in ("pre", "post"):
        snapshot(a.context, a.out, a.phase)
        if a.phase == "post":
            return report(a.out)
    elif a.phase == "watch":
        watch(a.context, a.out, a.seconds, a.interval)
    else:
        return report(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
