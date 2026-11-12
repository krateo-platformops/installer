#!/usr/bin/env python3
"""Detect the stale-RBAC window core-provider#166 was meant to close.

WHY THIS EXISTS. Derived RBAC lives in `<release>-krateo-rbac` RoleBindings whose subjects are the
per-version controller ServiceAccounts. #166 re-subjects them in the SAME operation that retires the
old ServiceAccount. Before it, that repair ran only from a composition's own Observe -- so when the
umbrella dependency gate (installer#477) WITHDRAWS a CR there is no Observe, and the stale window had
no upper bound.

The owed measurement could not be taken on the 0.3.434 roll: the before-half found 64 bindings and
ZERO naming a retired ServiceAccount, because every recent bump's Observe had already run. The fix
only has something to exercise during a gate-out window, so the sampling has to span one.

WHAT THIS DETECTOR CAN AND CANNOT SEE, per core-provider reading retire.go. retireController calls
deploy.Undeploy (which deletes the retired controller and its ServiceAccount) and only THEN
deploy.ResubjectDerivedRBAC. Those are sequential calls in one function, so a dangling-subject
window exists in the NORMAL path by construction -- two API round-trips, sub-second. Polling at 5s
misses it essentially always. This detector therefore measures LONG windows only, and a zero is a
bound ("shorter than the interval"), never an absence. The long-window cases are a gate-out, where
no Observe runs the repair at all, and -- unverified, treat as an open question -- a
ResubjectDerivedRBAC failure after the SA is already gone.

WHY IT SAMPLES THE LIVE OBJECT AND NOT LOGS. kubectl logs on 057 retains minutes, not hours, and a
binding's subject mid-window leaves no trace afterwards. By the time you know you wanted the data it
is gone. So: arm this BEFORE the roll.

  python3 hack/check-rbac-stale-window.py --context <ctx> baseline --out <dir>
  python3 hack/check-rbac-stale-window.py --context <ctx> watch --out <dir> --seconds 900 --interval 5
  python3 hack/check-rbac-stale-window.py --out <dir> report

CADENCE. Gate duration sets it: ~90s when the dependency is a bare CRD chart, ~8 minutes when it
sits behind another composition's rollup. 5s sampling covers both; the cost is one list of
RoleBindings plus one of ServiceAccounts per poll.
"""
import argparse, datetime, json, os, subprocess, sys, time

def kubectl(ctx, *args, check=True):
    r = subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, text=True,
                       timeout=240, env={**os.environ})
    if check and r.returncode != 0:
        print(f"::error::kubectl {' '.join(args)[:70]} failed: {(r.stderr or '').strip()[:200]}")
        sys.exit(1)
    return r.stdout


def _sample(ctx):
    """One observation: every *-krateo-rbac binding's SA subjects, and which of them exist."""
    rb = json.loads(kubectl(ctx, "get", "rolebindings", "-A", "-o", "json"))["items"]
    sa = json.loads(kubectl(ctx, "get", "serviceaccounts", "-A", "-o", "json"))["items"]
    live = {(s["metadata"]["namespace"], s["metadata"]["name"]) for s in sa}
    rows = []
    for b in rb:
        if not b["metadata"]["name"].endswith("-krateo-rbac"):
            continue
        ns = b["metadata"]["namespace"]
        for sub in b.get("subjects") or []:
            if sub.get("kind") != "ServiceAccount":
                continue
            key = (sub.get("namespace") or ns, sub.get("name"))
            rows.append({"binding": f"{ns}/{b['metadata']['name']}",
                         "sa": f"{key[0]}/{key[1]}", "exists": key in live})
    # THE CONTROL, and it has to run the REAL comparison or it is worth nothing. A detector whose
    # only output has ever been zero has not been shown to work. So: push a fabricated subject and a
    # known-present subject through the SAME membership test against the SAME live set, and record
    # both outcomes. A control that returns a hardcoded True is the vacuous-reassurance failure this
    # whole script exists to avoid -- the first draft of this function did exactly that.
    bogus = ("krateo-system", "definitely-not-a-real-sa-%d" % int(time.time()))
    negative_flagged = bogus not in live          # must be True
    positive = next(iter(live)) if live else None
    positive_flagged = (positive not in live) if positive else None   # must be False
    control = {"negativeSubject": "/".join(bogus),
               "negativeCorrectlyFlagged": negative_flagged,
               "positiveSubject": "/".join(positive) if positive else None,
               "positiveWronglyFlagged": positive_flagged,
               "discriminates": bool(negative_flagged) and positive_flagged is False}
    return {"at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "bindings": len({r["binding"] for r in rows}),
            "subjects": len(rows),
            "dangling": [r for r in rows if not r["exists"]],
            "liveServiceAccounts": len(live),
            "control": control}


def baseline(ctx, out):
    os.makedirs(out, exist_ok=True)
    s = _sample(ctx)
    if not s["control"]["discriminates"]:
        print("::error::the existence test does not discriminate: a fabricated ServiceAccount was "
              "not flagged, or a live one was. A zero from this detector would be meaningless.")
        sys.exit(1)
    if s["bindings"] == 0:
        print("::error::0 *-krateo-rbac RoleBindings found. That is not a clean baseline, it is a "
              "failed read -- a cluster running compositions always has some.")
        sys.exit(1)
    with open(os.path.join(out, "baseline.json"), "w") as f:
        json.dump(s, f, indent=2)
    print(f"  baseline: {s['bindings']} *-krateo-rbac bindings, {s['subjects']} SA subjects, "
          f"{len(s['dangling'])} DANGLING, against {s['liveServiceAccounts']} live ServiceAccounts")
    for d in s["dangling"][:10]:
        print(f"    dangling: {d['binding']} -> {d['sa']}")
    return 0


def watch(ctx, out, seconds, interval):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "rbac-timeline.jsonl")
    base = os.path.join(out, "baseline.json")
    if not os.path.exists(base):
        print("::error::no baseline.json -- run the baseline phase BEFORE the roll. A window that "
              "has already closed leaves no trace in the live objects.")
        sys.exit(1)
    deadline = time.time() + seconds
    n = 0
    print(f"  watching for {seconds}s at {interval}s intervals -> {path}")
    with open(path, "a") as f:
        while time.time() < deadline:
            try:
                s = _sample(ctx)
                f.write(json.dumps(s) + "\n"); f.flush(); n += 1
            except Exception as e:
                f.write(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc)
                                    .isoformat().replace("+00:00", "Z"), "error": str(e)[:200]}) + "\n")
                f.flush()
            time.sleep(interval)
    print(f"  {n} sample(s)")
    return 0


def report(out):
    path = os.path.join(out, "rbac-timeline.jsonl")
    if not os.path.exists(path):
        print("::error::no rbac-timeline.jsonl -- run the watch phase"); return 1
    rows = [json.loads(l) for l in open(path)]

    def t(x):
        """Defined up here on purpose: both the zero branch and the window branch use it, and the
        first draft defined it only after the zero branch -- which raised UnboundLocalError on
        exactly the path that matters most, the one that reports a clean result."""
        try:
            return datetime.datetime.fromisoformat(x.replace("Z", "+00:00"))
        except Exception:
            return None
    ok = [r for r in rows if "error" not in r]
    print("STALE-RBAC WINDOW (core-provider#166)")
    print()
    print(f"  samples: {len(rows)} ({len(rows)-len(ok)} failed reads)")
    if not ok:
        print("  nothing measured."); return 1
    nondisc = [r for r in ok if not (r.get("control") or {}).get("discriminates")]
    if nondisc:
        print(f"  *** {len(nondisc)} of {len(ok)} samples had a NON-DISCRIMINATING existence test "
              f"(first {nondisc[0]['at']}). Those samples prove nothing; a zero from them is not a "
              f"clean result. Withholding the verdict. ***")
        return 1
    print(f"  control: the existence test discriminated on all {len(ok)} samples")
    bad = [r for r in ok if r["dangling"]]
    print(f"  samples with a DANGLING subject: {len(bad)} of {len(ok)}")
    if not bad:
        print("  NO DANGLING SUBJECT SEEN IN ANY SAMPLE.")
        print()
        print("  READ THIS AS A BOUND, NOT AS AN ABSENCE. There is a dangling window in the normal")
        print("  path BY CONSTRUCTION, and this detector cannot resolve it. core-provider's")
        print("  retireController (compositiondefinitions/retire.go) calls deploy.Undeploy -- which")
        print("  deletes the retired controller AND its ServiceAccount -- and only THEN")
        print("  deploy.ResubjectDerivedRBAC. Two sequential API round-trips, so expect sub-second.")
        gaps = []
        for i in range(len(ok) - 1):
            x, y = t(ok[i]["at"]), t(ok[i + 1]["at"])
            if x and y:
                gaps.append((y - x).total_seconds())
        gaps.sort()
        med = gaps[len(gaps) // 2] if gaps else None
        shown = ("%.1fs" % med) if med is not None else "unknown"
        print("  The MEASURED median gap between samples here was " + shown + ", so a sub-second")
        print("  window is missed essentially always. A zero therefore means \"shorter than that")
        print("  gap\", never \"no window\".")
        print()
        print("  WHAT A ZERO DOES RULE OUT: a LONG window. Two cases would produce one --")
        print("    (a) a gate-out, where no Observe runs the repair at all. That was unbounded")
        print("        before #166 and is the case this measurement exists for; it needs a")
        print("        WITHHELD component, so a roll that withdrew no CR exercises nothing. That is")
        print("        exactly why the 0.3.434 attempt measured zero and proved nothing.")
        print("    (b) ResubjectDerivedRBAC FAILING after Undeploy has already removed the SA.")
        print("        TRACED by core-provider at kp/main and largely CLOSED: retireController")
        print("        returns the error, Update propagates it, the definition requeues with")
        print("        backoff, and on the retry Undeploy finds the objects already gone and the")
        print("        re-subject runs again. So it is BOUNDED BY THE RECONCILE BACKOFF, not")
        print("        unbounded. One step is inference rather than traced: that the recorded old")
        print("        version survives the failed pass. #457 helps incidentally -- VersionInfo")
        print("        mirrors the served versions, which now linger >= 5m, so the branch stays")
        print("        reachable that long.")
        print("  So (a) is effectively the ONLY long-window case left: the cdc's own Observe")
        print("  re-subjects its binding (#167), which means a long window needs the composition CR")
        print("  to be ABSENT -- i.e. withheld by the dependency gate.")
        print("  If no gate-out occurred in this window, report #166 as STILL OUTSTANDING -- not as")
        print("  a pass.")
        return 0
    first, last = bad[0]["at"], bad[-1]["at"]
    dur = (t(last) - t(first)).total_seconds() if t(first) and t(last) else None
    print(f"  WINDOW OBSERVED: {first} .. {last}"
          + (f"  ({dur:.0f}s, plus up to one interval either side)" if dur is not None else ""))
    seen = {}
    for r in bad:
        for d in r["dangling"]:
            seen.setdefault((d["binding"], d["sa"]), 0)
            seen[(d["binding"], d["sa"])] += 1
    for (b, sa), c in sorted(seen.items(), key=lambda x: -x[1]):
        print(f"    {b} -> {sa}   in {c} sample(s)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["baseline", "watch", "report"])
    ap.add_argument("--context")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=int, default=900)
    ap.add_argument("--interval", type=int, default=5)
    a = ap.parse_args()
    if a.phase in ("baseline", "watch") and not a.context:
        print("::error::--context is required for that phase"); sys.exit(2)
    if a.phase == "baseline": sys.exit(baseline(a.context, a.out))
    if a.phase == "watch": sys.exit(watch(a.context, a.out, a.seconds, a.interval))
    sys.exit(report(a.out))


if __name__ == "__main__":
    main()
