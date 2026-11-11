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
  python3 hack/capture-roll-evidence.py --context <ctx> --out <dir> watch --seconds 600
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


def sample_served(ctx, outdir):
    """Append a timestamped served-version sample for every composition.krateo.io CRD.

    WHY PER-POLL AND NOT JUST pre/post. A pre-snapshot bounds absence at ONE instant, and I asserted
    a conclusion it could not carry: that an error naming v1-8-109 at 18:15:20 proved the version
    was not yet added, when all the pre-snapshot showed was absence at 18:12:19. core-provider had
    already warned me that managedFields keeps only the NEWEST write per manager, so it cannot date
    a specific event -- and I used it as if it could, twice.

    What settles it is the served set AT the error's instant. compositions.yaml:37 does a LIVE
    lookup of all CRDs in the same helm action whose post-render later fails, so the two are seconds
    apart; sampling every poll brackets any error within one interval.
    """
    path = os.path.join(outdir, "served-timeline.jsonl")
    raw = kubectl(ctx, "get", "crd", "-o", "json", check=False)
    if not raw:
        return 0
    try:
        items = json.loads(raw).get("items", [])
    except Exception:
        return 0
    snap = {c["spec"]["names"]["kind"]: sorted(v["name"] for v in c["spec"]["versions"]
                                               if v.get("served"))
            for c in items if c["spec"].get("group") == GROUP}
    if not snap:
        return 0
    with open(path, "a") as f:
        f.write(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc)
                            .isoformat().replace("+00:00", "Z"), "served": snap}) + "\n")
    return len(snap)


def watch(ctx, outdir, seconds):
    """Follow every installers-v* controller and append any line carrying an `err`, verbatim."""
    path = os.path.join(outdir, "errors.jsonl")
    seen_pods, n = set(), 0
    emitted = set()   # (pod, timestamp, err-prefix) -> written already
    deadline = time.time() + seconds
    print(f"  watching for {seconds}s -> {path}")
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
            sample_served(ctx, outdir)
            time.sleep(10)
    tl = os.path.join(outdir, "served-timeline.jsonl")
    samples = sum(1 for _ in open(tl)) if os.path.exists(tl) else 0
    print(f"  captured {n} error line(s) from {len(seen_pods)} pod(s); "
          f"{samples} served-version sample(s)")
    return n


def report(outdir):
    pre, post = os.path.join(outdir, "pre"), os.path.join(outdir, "post")
    if not (os.path.isdir(pre) and os.path.isdir(post)):
        print("::error::capture: need both pre/ and post/ snapshots to report")
        return 1
    lines, added_index = [], {}
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
    out += ["", "ERRORS IN THIS WINDOW, in full:", ""]
    tl = os.path.join(outdir, "served-timeline.jsonl")
    timeline = [json.loads(l) for l in open(tl)] if os.path.exists(tl) else []
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
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.phase in ("pre", "post"):
        snapshot(a.context, a.out, a.phase)
        if a.phase == "post":
            return report(a.out)
    elif a.phase == "watch":
        watch(a.context, a.out, a.seconds)
    else:
        return report(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
