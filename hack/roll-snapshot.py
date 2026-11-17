#!/usr/bin/env python3
"""Capture a roll baseline that can actually be DIFFED afterwards, and diff two captures.

WHY THIS EXISTS. On 2026-11-16 I rolled krateo-057 to 0.3.671 with what I thought was a thorough
pre-roll baseline: the installer's helm revision, the release COUNT, the composition count, the
condition census, pod states and the specific component versions that had to move. Every one of
those was useful. Then frontend asked me to confirm their portal-line signature:

    all 17 BuilderPublish compositions at composition.krateo.io/v1-8-129   -> I could answer
    each release's latest revision exactly ONE above its pre-roll revision -> I COULD NOT
    status.managed unchanged per composition                               -> I COULD NOT

The two I could not answer were not harder to measure. I simply had not captured the PER-OBJECT
values, only the aggregates. And the current revisions are not uniform (72, 73, 75, 76, 79, 80, 81,
83, 84, 87, 217 across the 17), so there is no shortcut and no way to reconstruct them: I would be
guessing at sixteen different previous numbers.

A BASELINE IS ONE-SHOT. The window to capture it closed when helm returned, and a predicate with no
pre-roll value cannot be checked afterwards -- not with more effort, not at all. frontend's reply
is the rule: "a predicate with no pre-roll value can't be checked afterwards." They are not counting
today's revision half as a pass, correctly.

WHAT IT CAPTURES, which is exactly what frontend asked for so their signatures become checkable:

    per helm release      name, namespace, chart, chart version, latest revision, status
    per composition       apiVersion, status.managed COUNT, Ready status + reason, Synced,
                          uid (so a recreate is distinguishable from an update)

The uid is mine, not theirs: without it "updated in place" and "deleted and recreated with the same
name" look identical in every other field, and I have already made that mistake today in the
opposite direction by reading creationTimestamp as a write time.

WHAT IT DELIBERATELY DOES NOT CAPTURE: anything inside a Secret. Secret NAMES and their data KEY
NAMES are recorded, never a value. For "was this credential touched", the answer is
`managedFields[].time` plus the fieldsV1 key set, which this tool records for Secrets it is asked
about -- and even then "written" is not "changed", because a controller re-applying identical bytes
produces the same trace.

    python3 hack/roll-snapshot.py capture --context <ctx> -o pre.json
    python3 hack/roll-snapshot.py capture --context <ctx> -o post.json
    python3 hack/roll-snapshot.py diff pre.json post.json
    python3 hack/roll-snapshot.py diff pre.json post.json --expect-release-revision-delta 1 \
        --releases-matching 'publish-'
    python3 hack/roll-snapshot.py --self-test
"""
import argparse
import json
import subprocess
import sys


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


# A composition that is CONVERGING is normal. A composition that has been converging for a long
# time is a finding, and until core-provider#498 (ChildStalled) exists nothing in the platform
# says so. Measured motivation, 2026-11-17 on demo: incident-provider's workload was OOMKilled
# every ~45s for 8.5 HOURS. core-provider confirmed the composition's own door was open the whole
# time -- an apps/v1 Deployment child goes to workloadReady, which caps at boolState(false) =
# CONVERGING because a Deployment self-progresses -- so the composition read
# Ready=False/Creating, naming the child, and NOTHING BOUNDED IT. A roll verification that treats
# converging as "keep waiting" waits for ever, and "still installing" is the single most
# reassuring thing a broken component can say.
#
# THE THRESHOLD IS NOT INVENTED. The convergence window agreed across sessions is
# [helm start, +10 min], and snowplow's seed ceiling of 480s is structural inside it. So 10
# minutes is what a legitimate convergence is allowed to take, and the default here is 900s --
# 1.5x that -- so a normal bump never trips it.
CONVERGING_BUDGET_S = 900
CONVERGED = ("True/Available", "True/AvailablePartialCoverage",
             "True/ReconcileGracefullyPaused", "True/ReconcilePaused")

# The reasons that mean a COMPOSITION has FAILED, as opposed to not having converged yet.
#
# hack/ready-reasons.yaml IS THE SOURCE OF TRUTH for this classification, and
# check-ready-reasons-current.py keeps it from drifting out of core-provider's generated table.
# It is duplicated here as a literal because this tool must stay dependency-free -- it runs under
# `python3 -I` with no PyYAML, during a roll, on whatever machine is holding the roll. The
# check-roll-snapshot workflow asserts this tuple EQUALS that file's `failed` list, so the
# duplication is gated rather than trusted.
#
# WHY NOT just reuse the definitions check, `Ready != "True/Available"`. Because compositions
# legitimately sit at other converged states and a blunt copy would fire on healthy clusters:
# measured on 057, 15 of 86 compositions read AvailablePartialCoverage and one read
# ReconcilePaused. Reporting those as problems would train the reader to ignore the check, which
# is worse than not having it.
#
# WHY IT MATTERS NOW. core-provider's #498 + #559 (decided 2026-11-17) move a crash-looping
# workload child from converging to FAILED once the Deployment gives up its own
# progressDeadlineSeconds, so the composition will read Ready=False/Unavailable with the pod
# cause in the message. There is no new reason to classify -- Unavailable is already failed here
# -- but there WAS no check that looked. A composition left failed after a roll was reported as a
# changed field and nothing more.
FAILED_REASONS = ("Unavailable", "CreateOutcomeUnknown", "HooksMayNotHaveRun")

# The COMPOSITIONDEFINITION failed set, which is a DIFFERENT namespace from the one above.
# ready-reasons.yaml carries both and check-roll-snapshot.yaml cross-checks each against its own
# list; mixing them is the mistake core-provider and I both nearly made on ValuesSchemaMissing.
#
# WHY THIS EXISTS AT ALL. The CD check below used to report every CD not reading True/Available as
# a problem. That was right while a non-Available CD meant "Observe has not caught up". From
# core-provider 2.13.108 `Available` REQUIRES A COMPLETED ROLLOUT, so a CD whose controller
# Deployment is still rolling reads Ready=False/CREATING -- a converging verdict, not a failure.
# A blanket check would then report ~70 problems during a normal roll, and an operator who sees
# that learns to ignore the list. core-provider pointed out the fix: classify by the REASON, whose
# verdict the registry already gives, rather than by reading each child Deployment.
CD_FAILED_REASONS = ("Unavailable", "RemoteChartUnreachable", "SchemaDefaultContradiction",
                     "ValuesSchemaMissing")

# compositionDefinition.converging, mirrored for the same dependency-free reason.
CD_CONVERGING_REASONS = ("Creating", "Deleting")


def _age_seconds(ts, now=None):
    """Seconds since an RFC 3339 timestamp, or None if it cannot be parsed or is absent.

    None means "do not judge": a missing or unparseable readyAt is a gap in the capture, and
    turning that into a finding about the cluster is the failure mode this returns None to avoid.
    """
    import datetime as _dt
    try:
        t = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    ref = now or _dt.datetime.now(_dt.timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return (ref - t).total_seconds()


# A composition whose Ready has NEVER moved off its initial value is not stalled, and an age
# alone cannot tell the two apart. MEASURED 2026-11-17 on krateo-057, on this bound's first real
# run, while taking a pre-roll baseline:
#
#   internalcabundles/krateo-system/internal-ca-bundle
#     created                    2026-11-15T21:36:50Z
#     Ready Unknown/ZeroEvidence 2026-11-15T21:36:56Z   <- SIX SECONDS after creation
#     reported as                "converging longer than 900s ... (34.7 h)"
#     message: none of its 1 managed children could be health-evaluated (no readiness semantics
#              this controller knows how to read)
#
# It renders, its RBAC is applied, it is Synced, its apiRef resolved. Its single managed child has
# a kind whose readiness the cdc has no semantics for, so ZeroEvidence is the honest answer and
# will be the answer for ever. "Stuck for 34.7 h" is true as arithmetic and misleading as a
# finding: it reads as WAS FINE, NOW STUCK, when the truth is NEVER EVALUABLE.
#
# WHY THE SPLIT MATTERS MORE THAN THE WORDING. This bound exists because a crash-looping workload
# reads as converging indefinitely and hid an 8.5-hour outage on demo. A bound that ALSO fires,
# permanently, on an unevaluable composition trains its reader to skip the line -- which is how
# the real case gets missed. Same reason the FAILED check fires on the failed class only rather
# than on everything that is not True/Available. installer#1017.
NEVER_EVALUATED_GRACE_S = 60


def stuck_converging(snap, budget_s=CONVERGING_BUDGET_S, now=None):
    """Not-converged compositions, split into (stalled, never_evaluated).

    Each list holds (key, Ready, readyAt, age_seconds). A composition is NEVER EVALUATED when its
    readyAt is within NEVER_EVALUATED_GRACE_S of its createdAt.

    Without a createdAt it is classed STALLED -- the conservative side on purpose, because
    under-reporting a real stall is the failure this bound exists to prevent, and a snapshot taken
    before createdAt was captured (anything before installer#1017) has none.

    A composition with no readyAt is in NEITHER list: absence of the field is not evidence of
    staleness, and guessing would turn a gap in the capture into a false finding about the cluster.
    """
    import datetime as _dt

    def _parse(v):
        try:
            return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    now = now or _dt.datetime.now(_dt.timezone.utc)
    stalled, never = [], []
    for key, c in sorted((snap.get("compositions") or {}).items()):
        ready = c.get("Ready")
        if ready in CONVERGED or ready is None:
            continue
        t = _parse(c.get("readyAt"))
        if t is None:
            continue
        age = (now - t).total_seconds()
        if age <= budget_s:
            continue
        born = _parse(c.get("createdAt"))
        row = (key, ready, c.get("readyAt"), age)
        if born is not None and abs((t - born).total_seconds()) <= NEVER_EVALUATED_GRACE_S:
            never.append(row)
        else:
            stalled.append(row)
    return stalled, never


# COVERAGE SCALARS. ADDED 2026-11-17, and the reason is that `Ready` CANNOT carry this.
# core-provider's childhealth.go is explicit about the rollup's direction of failure:
#
#     "The rollup is itself fail-safe (unreadable / unmodeled children count healthy), so this
#      can only surface failures it can positively observe -- it never flips a working
#      composition to NotReady."
#
# So a composition whose ServiceAccount was REFUSED a read of some of its children still reads
# Ready=True/Available. childhealth.go calls that refusal "a DEFECT IN GENERATED RBAC -- ours,
# and actionable", which is exactly the class a green roll verification must not swallow. The
# count reaches consumers as a STRUCTURED FIELD rather than prose, for the reason their own
# comment gives: "because prose is not an" API.
#
# WHAT I HAD WRONG BEFORE THIS. I had been carrying a held item called `UnmodelledKeysPresent`,
# believing it a condition reason I watched nothing for. It does not exist. `unmodelled` in
# core-provider means a child KIND with no readiness model, which falls through to
# existence-is-health -- a rollup concept, nothing to do with schema keys. The thing I actually
# wanted was never a condition at all: these three scalars are.
def _coverage(st):
    """The rollup's coverage scalars, PRESERVING ABSENCE.

    ABSENT IS NOT ZERO, and this is the trap core-provider's own test guards. They assert these
    fields are NOT written when the rollup never ran, with the reason spelled out:

        "a zero here is a fact about the instrument published as a fact about the composition,
         and a consumer reading healthEvaluatedChildren == managedChildrenCount would conclude
         FULL COVERAGE from 0 == 0"

    So a missing field stays None here and is never defaulted. Reading it as 0 would make a
    never-assessed composition look fully covered -- the precise inversion of what this capture
    is for.
    """
    return {k: st.get(k) for k in ("managedChildrenCount",
                                   "healthEvaluatedChildren",
                                   "unreadableChildrenCount")}


def coverage_problems(snap):
    """Compositions the rollup could not fully see: (unreadable, unevaluated, broken).

    unreadable   children the rollup was REFUSED a read of. A FINDING: fail-open by design,
                 so the composition reads green, and the cause is our own generated RBAC.
    unevaluated  children neither evaluated nor unreadable, derived with core-provider's OWN
                 invariant, which their test states as
                 managedChildrenCount - healthEvaluatedChildren - unreadableChildrenCount.
                 REPORTED, NOT A FINDING: these are kinds with no readiness model, and they are
                 EXCLUDED from the rollup rather than counted as converging -- which I got
                 backwards earlier today and told two sessions.
    broken       the invariant goes NEGATIVE. A FINDING about THIS SCRIPT or the contract, not
                 about the cluster; the other two lists are untrustworthy until it is resolved.

    A composition missing any of the three fields appears in NONE of the lists -- the same rule
    stuck_converging() uses for a missing readyAt: absence of a field is not evidence of a state.
    """
    unreadable, unevaluated, broken = [], [], []
    for key, c in sorted((snap.get("compositions") or {}).items()):
        cov = c.get("coverage") or {}
        total = cov.get("managedChildrenCount")
        evaluated = cov.get("healthEvaluatedChildren")
        unread = cov.get("unreadableChildrenCount")
        if total is None or evaluated is None or unread is None:
            continue
        gap = total - evaluated - unread
        if gap < 0:
            broken.append((key, total, evaluated, unread))
            continue
        if unread > 0:
            unreadable.append((key, unread, total, c.get("Ready")))
        if gap > 0:
            unevaluated.append((key, gap, total, c.get("Ready")))
    return unreadable, unevaluated, broken


def capture(ctx, secrets=()):
    snap = {"context": ctx, "releases": {}, "compositions": {},
            "definitions": {}, "secrets": {}}

    for rel in json.loads(_run(["helm", "--kube-context", ctx, "list", "-A", "-o", "json"]) or "[]"):
        snap["releases"]["%s/%s" % (rel["namespace"], rel["name"])] = {
            "chart": rel.get("chart"),
            "revision": str(rel.get("revision")),
            "status": rel.get("status"),
        }

    crds = json.loads(_run(["kubectl", "--context", ctx, "get", "crd", "-o", "json"]) or '{"items":[]}')["items"]
    for c in crds:
        if c["spec"]["group"] != "composition.krateo.io":
            continue
        plural = c["spec"]["names"]["plural"]
        if not [v for v in c["spec"]["versions"] if v.get("served")]:
            continue
        out = _run(["kubectl", "--context", ctx, "get",
                    "%s.composition.krateo.io" % plural, "-A", "-o", "json"])
        if not out:
            continue
        for o in json.loads(out).get("items", []):
            st = o.get("status") or {}
            cmap = {x["type"]: x for x in (st.get("conditions") or [])}
            conds = {t: "%s/%s" % (x["status"], x.get("reason", ""))
                     for t, x in cmap.items()}
            key = "%s/%s/%s" % (plural, o["metadata"]["namespace"], o["metadata"]["name"])
            # krateo.io/hooks-unverified (an RFC 3339 time) is what HooksMayNotHaveRun is set
            # FROM, and it outlives the condition: a human or the next real helm install/upgrade
            # clears the reason, and the annotation is the only record that the hooks were never
            # verified. Captured as a value, not a boolean, so a pre/post pair shows whether it is
            # the SAME unverified episode or a new one.
            hooks_unverified = (o["metadata"].get("annotations") or {}).get(
                "krateo.io/hooks-unverified")
            snap["compositions"][key] = {
                "apiVersion": o["apiVersion"],
                "hooksUnverified": hooks_unverified,
                "coverage": _coverage(st),
                "managed": len(st.get("managed") or []) if st.get("managed") is not None else None,
                "Ready": conds.get("Ready"),
                # readyAt, ADDED 2026-11-17, for the same reason it was added to definitions on
                # 2026-11-16 and which I failed to apply here at the time: Ready status+reason says
                # what a composition reads AT SNAPSHOT TIME; lastTransitionTime says WHEN it last
                # changed, and that is the only thing that can bound how long it has been stuck.
                "readyAt": (cmap.get("Ready") or {}).get("lastTransitionTime"),
                # createdAt, so NEVER EVALUATED can be told from STALLED. See
                # stuck_converging(): a readyAt within seconds of creation means the Ready
                # condition has not moved since the object appeared, so its age measures the
                # object's lifetime and not a stall. installer#1017.
                "createdAt": o["metadata"].get("creationTimestamp"),
                "Synced": conds.get("Synced"),
                "uid": o["metadata"].get("uid"),
            }

    # CompositionDefinitions. ADDED 2026-11-16, and the reason is specific: a chart-version bump is
    # a NEW CRD version, so Observe finds the version not yet served and writes
    # Ready=False/Unavailable; Update adds it and succeeds; the runtime then requeues after the poll
    # interval (3m default, and 057's Deployment sets no override) before the next Observe can write
    # Available. core-provider is explicit that this is a READING OF 2.13.105's CODE, NOT A
    # MEASUREMENT -- they have not observed it on 057, and neither have I.
    #
    # IT MATTERS BECAUSE THE UMBRELLA READS IT. cdc childhealth.go:801 routes any *.krateo.io group
    # through krateoEval, a CD's group is core.krateo.io, and krateoEval maps Unavailable -> FAILED.
    # On 057, 69 of 76 CDs carry release-name=installer-platform, so they are children of the
    # umbrella composition. A tag that bumps 21 components therefore makes the umbrella read failed
    # children for the length of that window -- and check-ready-gate BLOCKS on
    # Ready=False/Unavailable. Without this section neither the transient nor its clearing is
    # recorded, so a verification could not tell a settling roll from a broken one.
    #
    # readyAt IS THE FIELD THAT MAKES THE WINDOW MEASURABLE. Ready status+reason alone says what a
    # CD reads at snapshot time; lastTransitionTime says WHEN it changed, which is the only way a
    # pre/post pair bounds the window rather than merely sampling it.
    for o in json.loads(_run(["kubectl", "--context", ctx, "get",
                              "compositiondefinitions.core.krateo.io", "-A",
                              "-o", "json"]) or '{"items":[]}').get("items", []):
        st = o.get("status") or {}
        conds = {x["type"]: x for x in (st.get("conditions") or [])}
        r = conds.get("Ready") or {}
        s = conds.get("Synced") or {}
        m = o["metadata"]
        ann = m.get("annotations") or {}
        lab = m.get("labels") or {}
        snap["definitions"]["%s/%s" % (m["namespace"], m["name"])] = {
            "chartVersion": ((o.get("spec") or {}).get("chart") or {}).get("version"),
            "Ready": "%s/%s" % (r.get("status"), r.get("reason", "")) if r else None,
            "readyAt": r.get("lastTransitionTime"),
            "Synced": "%s/%s" % (s.get("status"), s.get("reason", "")) if s else None,
            "uid": m.get("uid"),
            # Which parent rolls this up, so an umbrella child is distinguishable from a stray.
            "parent": ann.get("meta.helm.sh/release-name"),
            "compositionId": lab.get("krateo.io/composition-id") or ann.get("krateo.io/composition-id"),
        }

    # Secrets: metadata only. Never a value, and the data KEY NAMES only so a key appearing or
    # disappearing is visible.
    for ref in secrets:
        ns, _, name = ref.partition("/")
        out = _run(["kubectl", "--context", ctx, "get", "secret", name, "-n", ns,
                    "--show-managed-fields", "-o", "json"])
        if not out:
            snap["secrets"][ref] = {"present": False}
            continue
        s = json.loads(out)
        m = s["metadata"]
        snap["secrets"][ref] = {
            "present": True,
            "dataKeys": sorted((s.get("data") or {}).keys()),
            "resourceVersion": m.get("resourceVersion"),
            "writes": [{"manager": f.get("manager"), "op": f.get("operation"),
                        "time": f.get("time"), "fields": sorted((f.get("fieldsV1") or {}).keys())}
                       for f in (m.get("managedFields") or [])],
        }
    return snap


def diff(pre, post, expect_delta=None, matching=None):
    """Pure, so the self-test drives it without a cluster."""
    out = {"releases": {}, "compositions": {}, "definitions": {},
           "secrets": {}, "problems": []}

    pr, po = pre.get("releases", {}), post.get("releases", {})
    out["releases"]["gone"] = sorted(set(pr) - set(po))
    out["releases"]["new"] = sorted(set(po) - set(pr))
    moved = {}
    for k in sorted(set(pr) & set(po)):
        a, b = pr[k], po[k]
        if a != b:
            moved[k] = {f: [a.get(f), b.get(f)] for f in ("chart", "revision", "status")
                        if a.get(f) != b.get(f)}
    out["releases"]["moved"] = moved
    _stalled, _never = stuck_converging(post)
    for key, ready, at, age in _stalled:
        out["problems"].append(
            "STUCK CONVERGING: %s reads %s since %s (%.1f h). Converging is not converged; a "
            "crash-looping workload child caps at converging, so this never becomes a failure "
            "on its own." % (key, ready, at, age / 3600.0))
    # NEVER EVALUATED is reported, not a problem: its age is the object's lifetime. Putting it in
    # problems would fire on every roll for ever and teach the reader to skip the list.
    for key, ready, at, age in _never:
        out.setdefault("never_evaluated", []).append(
            "%s reads %s and has not moved since creation (%.1f h)" % (key, ready, age / 3600.0))
    _unreadable, _unevaluated, _broken = coverage_problems(post)
    for key, total, evaluated, unread in _broken:
        out["problems"].append(
            "COVERAGE INVARIANT BROKEN: %s has managed=%d evaluated=%d unreadable=%d, so "
            "managed-evaluated-unreadable is negative. A finding about this script or about "
            "core-provider's contract, not about the cluster." % (key, total, evaluated, unread))
    for key, n, total, ready in _unreadable:
        out["problems"].append(
            "CHILDREN UNREADABLE: %s reads %s with %d of %d children the rollup was refused a "
            "read of. The rollup is fail-safe, so this reads green; core-provider calls the "
            "refusal a defect in generated RBAC." % (key, ready, n, total))
    # GROWTH ONLY. The absolute count is a steady state on a healthy cluster (32 of 89
    # compositions on 057), so reporting it per object every time is noise. A count that GREW
    # across the window is the informative case: children appeared that the rollup cannot assess.
    # Reported, not a problem -- new children of an unmodelled kind are normal.
    _pre_gap = {}
    for key, c in (pre.get("compositions") or {}).items():
        cov = c.get("coverage") or {}
        t, e, u = (cov.get("managedChildrenCount"), cov.get("healthEvaluatedChildren"),
                   cov.get("unreadableChildrenCount"))
        if None not in (t, e, u):
            _pre_gap[key] = t - e - u
    for key, n, total, ready in _unevaluated:
        was = _pre_gap.get(key)
        if was is not None and n > was:
            out.setdefault("partial_coverage", []).append(
                "%s reads %s with %d of %d children never health-evaluated, up from %d"
                % (key, ready, n, total, was))
    if out["releases"]["gone"]:
        out["problems"].append("RELEASES DISAPPEARED: %s" % out["releases"]["gone"])
    bad_status = {k: v["status"] for k, v in po.items() if v.get("status") != "deployed"}
    if bad_status:
        out["problems"].append("RELEASES NOT deployed: %s" % json.dumps(bad_status, sort_keys=True))

    if expect_delta is not None:
        off = {}
        for k in sorted(set(pr) & set(po)):
            if matching and matching not in k:
                continue
            try:
                a, b = int(pr[k]["revision"]), int(po[k]["revision"])
            except (TypeError, ValueError):
                off[k] = ["unparseable", pr[k].get("revision"), po[k].get("revision")]
                continue
            if b - a != expect_delta:
                off[k] = [b - a, pr[k]["revision"], po[k]["revision"]]
        out["releases"]["revision_delta_not_%d" % expect_delta] = off
        if off:
            out["problems"].append(
                "REVISION DELTA != %d for %d release(s): %s"
                % (expect_delta, len(off), json.dumps(off, sort_keys=True)))

    pc, poc = pre.get("compositions", {}), post.get("compositions", {})
    out["compositions"]["gone"] = sorted(set(pc) - set(poc))
    out["compositions"]["new"] = sorted(set(poc) - set(pc))
    ch = {}
    for k in sorted(set(pc) & set(poc)):
        a, b = pc[k], poc[k]
        d = {f: [a.get(f), b.get(f)] for f in ("apiVersion", "managed", "Ready", "Synced", "uid")
             if a.get(f) != b.get(f)}
        if d:
            ch[k] = d
    out["compositions"]["changed"] = ch
    recreated = {k: v["uid"] for k, v in ch.items() if "uid" in v}
    if recreated:
        out["problems"].append(
            "COMPOSITIONS RECREATED (uid changed -- NOT an in-place update): %s"
            % json.dumps(recreated, sort_keys=True))
    if out["compositions"]["gone"]:
        out["problems"].append("COMPOSITIONS DISAPPEARED: %s" % out["compositions"]["gone"])
    # The composition counterpart of the definitions check below, which existed while this one did
    # not. A composition whose Ready reason is in the FAILED class is the state krateoEval maps to
    # failed, which the umbrella inherits.
    #
    # CORRECTED 2026-11-17, same day it was written. I first said this is "not a state that
    # settles on its own". core-provider then read the Deployment controller's source
    # (k8s.io/kubernetes v1.36.4 pkg/controller/deployment/progress.go:40-100) and one arm of it
    # DOES settle: the progress switch runs on every sync while the rollout is incomplete, and
    # DeploymentProgressing -- any gain in updated, ready or available replicas -- is evaluated
    # BEFORE DeploymentTimedOut. So ProgressDeadlineExceeded flaps back to Progressing=True on
    # the next bit of progress, even before completion. A slow rollout therefore produces a
    # TRANSIENT failed reading inside the very window a roll samples.
    #
    # AND THE TWO LAYERS DISAGREE FOR UP TO ONE RESYNC, neither being wrong -- BUT ONLY FROM A
    # VERSION NOTHING RUNS YET. core-provider corrected this after I first wrote it, and the
    # correction matters because the sentence states a behaviour BY VERSION:
    #
    #   core-provider#557 (7da7248) is on their MAIN and in NO RELEASE. 057 runs 2.13.105 and
    #   tag 1 carries 2.13.107; NEITHER has the CD Deployment watch, the rollout rule, or the pod
    #   cause. The requirement is "core-provider >= the first release containing 7da7248", and
    #   that version does not exist yet -- it is held behind this repo's roll and frontend's #457.
    #
    # FROM that release: a CD's controller watches its Deployment, so the CD heals on the
    # Deployment's own event while a COMPOSITION heals only at its next reconcile (3m default).
    # Expect a CD Available while its composition still reads failed, and do not treat it as a
    # contradiction.
    #
    # BEFORE that release, which is TODAY on both clusters, the asymmetry is WORSE and in the
    # opposite direction: a CD reads Available from DECLARED REPLICAS and only re-checks at its
    # 3m poll, so **a CD can read Available while its own controller is not up at all**. So on
    # 2.13.105 and 2.13.107 a green CD is not evidence that its controller is running, and the
    # composition is the better of the two signals -- the reverse of the advice above.
    failed = {k: v.get("Ready") for k, v in (post.get("compositions") or {}).items()
              if str(v.get("Ready", "")).split("/", 1)[-1] in FAILED_REASONS}
    if failed:
        out["problems"].append(
            "COMPOSITIONS FAILED: %d -- %s. The umbrella inherits these. RE-SNAPSHOT BEFORE "
            "CONCLUDING: after core-provider #498 one arm of Unavailable HEALS ON ITS OWN. A "
            "workload that overran its progressDeadlineSeconds (600s default) reads Unavailable "
            "until its next pod becomes ready, then Creating, then Available -- Kubernetes' "
            "Deployment controller re-evaluates Progressing on ANY gain in updated, ready or "
            "available replicas, so ProgressDeadlineExceeded does NOT latch. The other arm, "
            "availability lost after a COMPLETED rollout, is the crash loop and does not heal. "
            "Only the child message distinguishes them and this tool does not read messages, so "
            "the rule is a SECOND SNAPSHOT after the composition's resync interval (3m default) "
            "plus slack -- the 900s stuck bound above is that slack."
            % (len(failed), json.dumps(failed, sort_keys=True)))

    pd, pod = pre.get("definitions", {}), post.get("definitions", {})
    out["definitions"]["gone"] = sorted(set(pd) - set(pod))
    out["definitions"]["new"] = sorted(set(pod) - set(pd))
    dch = {}
    for k in sorted(set(pd) & set(pod)):
        a, b = pd[k], pod[k]
        d = {f: [a.get(f), b.get(f)]
             for f in ("chartVersion", "Ready", "readyAt", "Synced", "uid", "parent")
             if a.get(f) != b.get(f)}
        if d:
            dch[k] = d
    out["definitions"]["changed"] = dch
    if out["definitions"]["gone"]:
        out["problems"].append("COMPOSITIONDEFINITIONS DISAPPEARED: %s" % out["definitions"]["gone"])
    # THE ONE THAT PROTECTS THE ROLL VERIFICATION. A CD still reading False/Unavailable after the
    # roll is the state krateoEval maps to FAILED, which the umbrella inherits and
    # check-ready-gate blocks on. Not-yet-settled and genuinely broken look identical in a single
    # sample, so this is reported as a problem and the caller re-snapshots rather than treating it
    # as a verdict. readyAt is in the changed set above so the window is bounded, not just sampled.
    # CLASSIFIED BY REASON, AND CONVERGING IS BOUNDED BY TIME RATHER THAN REPORTED OUTRIGHT.
    # Changed 2026-11-17, and the previous behaviour was defensible rather than wrong: it reported
    # every CD not reading True/Available, because Creating has NO DEADLINE and a CD stuck in it
    # forever would otherwise be invisible. That is still true. What changed is the base rate --
    # from core-provider 2.13.108 `Available` requires a COMPLETED ROLLOUT, so Creating is the
    # normal state of every CD whose controller Deployment is rolling. Reporting it outright would
    # make the list ~70 long on a healthy roll, and a list that is always long is never read.
    #
    # So the three buckets differ in what they mean, not just in wording:
    #   failed       classified `failed` by the registry -> a problem NOW, do not wait it out
    #   converging   Creating/Deleting -> a problem ONLY past CONVERGING_BUDGET_S, measured from
    #                readyAt, the same bound stuck_converging() applies to compositions. This is
    #                what keeps the old concern (no deadline) without the new noise.
    #   unclassified neither Available nor either of the above -> a problem, because an unknown
    #                reason is where this file having drifted from core-provider's table shows up
    cd_failed, cd_conv_late, cd_other = {}, {}, {}
    for k, v in pod.items():
        ready = str(v.get("Ready", ""))
        if ready == "True/Available":
            continue
        reason = ready.split("/", 1)[-1]
        if reason in CD_FAILED_REASONS:
            cd_failed[k] = v.get("Ready")
        elif reason in CD_CONVERGING_REASONS:
            age = _age_seconds(v.get("readyAt"))
            # No readyAt means NEITHER list: a gap in the capture must not become a finding
            # about the cluster. Same rule as stuck_converging().
            if age is not None and age > CONVERGING_BUDGET_S:
                cd_conv_late[k] = "%s for %.0fs" % (v.get("Ready"), age)
        else:
            cd_other[k] = v.get("Ready")
    if cd_failed:
        out["problems"].append(
            "COMPOSITIONDEFINITIONS FAILED: %d of %d -- %s. These reasons are classified `failed`"
            " in ready-reasons.yaml's compositionDefinition namespace, so this is NOT a settling"
            " roll and waiting will not clear it."
            % (len(cd_failed), len(pod), json.dumps(cd_failed, sort_keys=True)))
    if cd_conv_late:
        out["problems"].append(
            "COMPOSITIONDEFINITIONS CONVERGING PAST %ds: %d of %d -- %s. Creating/Deleting is"
            " EXPECTED during a roll (from 2.13.108 `Available` requires a completed rollout), so"
            " this is reported only past the budget. Creating has no deadline of its own, which is"
            " why the bound exists at all."
            % (CONVERGING_BUDGET_S, len(cd_conv_late), len(pod),
               json.dumps(cd_conv_late, sort_keys=True)))
    if cd_other:
        out["problems"].append(
            "COMPOSITIONDEFINITIONS with an UNCLASSIFIED reason: %d of %d -- %s. Not Available,"
            " not a known failure, not a known converging state -- so check it against"
            " core-provider's docs/cd-ready-reasons.md rather than assuming it is benign. This is"
            " where a drift between that table and ready-reasons.yaml surfaces."
            % (len(cd_other), len(pod), json.dumps(cd_other, sort_keys=True)))

    ps, pos = pre.get("secrets", {}), post.get("secrets", {})
    for k in sorted(set(ps) | set(pos)):
        a, b = ps.get(k, {}), pos.get(k, {})
        if a.get("present") and not b.get("present"):
            out["problems"].append("SECRET DISAPPEARED: %s" % k)
            continue
        if a.get("dataKeys") != b.get("dataKeys"):
            out["problems"].append("SECRET KEY SET CHANGED: %s %s -> %s"
                                   % (k, a.get("dataKeys"), b.get("dataKeys")))
        if a.get("resourceVersion") != b.get("resourceVersion"):
            out["secrets"][k] = {
                "written": True,
                "note": ("resourceVersion moved, so it WAS written. This does NOT establish that "
                         "any VALUE changed -- a controller re-applying identical bytes produces "
                         "the same trace. Only a functional test with the credential can answer "
                         "that, and this tool never reads one."),
                "writes": b.get("writes"),
            }
    return out


def _ago(seconds):
    """An RFC3339 timestamp `seconds` in the past. Self-test timestamps are RELATIVE so the cases
    exercise the real `stuck_converging` against the real clock rather than a frozen one -- a
    fixed date would silently stop testing the budget as it receded into the past, which is the
    same class of rot as a gate whose fixture outlives the thing it describes."""
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _self_test():
    PRE = {
        "releases": {
            "krateo-system/publish-a": {"chart": "builder-publish-1.8.128", "revision": "72", "status": "deployed"},
            "krateo-system/publish-b": {"chart": "builder-publish-1.8.128", "revision": "80", "status": "deployed"},
            "krateo-system/installer": {"chart": "installer-0.3.670", "revision": "579", "status": "deployed"},
        },
        "compositions": {
            "portals/krateo-system/portal": {"apiVersion": "composition.krateo.io/v1-8-128",
                                             "managed": 64, "Ready": "True/Available",
                                             "coverage": {"managedChildrenCount": 64,
                                                          "healthEvaluatedChildren": 64,
                                                          "unreadableChildrenCount": 0},
                                             "Synced": "True/ReconcileSuccess", "uid": "u-portal"},
        },
        "definitions": {
            "krateo-system/portal": {"chartVersion": "1.8.129", "Ready": "True/Available",
                                     "readyAt": "2026-11-16T10:00:00Z",
                                     "Synced": "True/ReconcileSuccess", "uid": "d-portal",
                                     "parent": "installer-platform",
                                     "compositionId": "58e2c59a"},
            "krateo-system/authn": {"chartVersion": "0.52.0", "Ready": "True/Available",
                                    "readyAt": "2026-11-16T10:00:00Z",
                                    "Synced": "True/ReconcileSuccess", "uid": "d-authn",
                                    "parent": "installer-platform",
                                    "compositionId": "58e2c59a"},
        },
        "secrets": {"krateo-system/cj": {"present": True, "dataKeys": ["password"],
                                         "resourceVersion": "100", "writes": []}},
    }
    # The fixture's composition must be CONVERGED and carry a readyAt, or every case below
    # inherits a missing field and the new falsifiers would pass for the wrong reason.
    PRE["compositions"]["portals/krateo-system/portal"]["Ready"] = "True/Available"
    PRE["compositions"]["portals/krateo-system/portal"]["readyAt"] = _ago(3600)

    def post(**kw):
        import copy
        p = copy.deepcopy(PRE)
        for k, v in kw.items():
            if k == "rev_a": p["releases"]["krateo-system/publish-a"]["revision"] = v
            if k == "rev_b": p["releases"]["krateo-system/publish-b"]["revision"] = v
            if k == "drop_a": del p["releases"]["krateo-system/publish-a"]
            if k == "uid": p["compositions"]["portals/krateo-system/portal"]["uid"] = v
            if k == "av": p["compositions"]["portals/krateo-system/portal"]["apiVersion"] = v
            if k == "managed": p["compositions"]["portals/krateo-system/portal"]["managed"] = v
            if k == "cov": p["compositions"]["portals/krateo-system/portal"]["coverage"] = v
            if k == "cd_ready": p["definitions"]["krateo-system/portal"]["Ready"] = v
            if k == "cd_readyat": p["definitions"]["krateo-system/portal"]["readyAt"] = v
            if k == "cd_chart": p["definitions"]["krateo-system/portal"]["chartVersion"] = v
            if k == "cd_drop": del p["definitions"]["krateo-system/portal"]
            if k == "comp_ready": p["compositions"]["portals/krateo-system/portal"]["Ready"] = v
            if k == "comp_readyat":
                p["compositions"]["portals/krateo-system/portal"]["readyAt"] = v
            if k == "comp_createdat":
                p["compositions"]["portals/krateo-system/portal"]["createdAt"] = v
            if k == "secret_rv": p["secrets"]["krateo-system/cj"]["resourceVersion"] = v
            if k == "secret_keys": p["secrets"]["krateo-system/cj"]["dataKeys"] = v
        return p

    cases = [
        ("identical snapshots -> no problems", PRE, None, None, 0),
        # COVERAGE CASES, 2026-11-17. `coverage` is deliberately NOT in the compared field list
        # above, so these exercise the coverage checks and nothing else -- a case that passed
        # because the dict differed would prove nothing about the checks.
        #
        # The checks are ABSOLUTE, not deltas: unreadable>0 is a finding whatever the pre-state
        # said. A delta would attribute the condition to the window, and children legitimately
        # come and go across a roll.
        ("coverage: 3 of 64 children UNREADABLE while Ready reads True/Available -> problem, "
         "because the rollup is fail-open and this is the state it hides",
         post(cov={"managedChildrenCount": 64, "healthEvaluatedChildren": 61,
                   "unreadableChildrenCount": 3}), None, None, 1),
        ("coverage: 5 of 64 never health-evaluated -> NO problem; reported, not a finding, "
         "because a kind with no readiness model is a permanent state",
         post(cov={"managedChildrenCount": 64, "healthEvaluatedChildren": 59,
                   "unreadableChildrenCount": 0}), None, None, 0),
        ("coverage: full (64 = 64 + 0) -> no problem",
         post(cov={"managedChildrenCount": 64, "healthEvaluatedChildren": 64,
                   "unreadableChildrenCount": 0}), None, None, 0),
        # unread is 0 DELIBERATELY. My first version of this case used unreadable=5, which
        # reported via the unreadable path instead -- so deleting the invariant branch entirely
        # left the case passing. Measured, not reasoned: mutation testing caught it.
        ("coverage: invariant NEGATIVE (5 - 8 - 0) with NO unreadable children -> problem ABOUT "
         "THIS SCRIPT or about core-provider's contract, not about the cluster",
         post(cov={"managedChildrenCount": 5, "healthEvaluatedChildren": 8,
                   "unreadableChildrenCount": 0}), None, None, 1),
        ("coverage: all three fields ABSENT (the rollup never ran) -> NO problem",
         post(cov={}), None, None, 0),
        # THE CASE THAT DISCRIMINATES. The all-absent case above passes identically whether
        # absence is preserved or defaulted to 0 (0-0-0 is a clean full-coverage read), so on its
        # own it proves nothing. Here unreadable=3 is present while managedChildrenCount is NOT:
        # preserving absence skips the object, while defaulting to 0 computes 0-0-3 = -3 and
        # reports a FALSE broken invariant. This is the case that fails if _coverage() ever grows
        # a `or 0`.
        ("CONTROL: unreadableChildrenCount present but managedChildrenCount ABSENT -> NO "
         "problem. Defaulting absence to 0 would compute 0-0-3 and report a FALSE broken "
         "invariant, so this case is what makes ABSENT-IS-NOT-ZERO testable",
         post(cov={"healthEvaluatedChildren": 0, "unreadableChildrenCount": 3}),
         None, None, 0),
        ("both releases +1, expect 1 -> PASS", post(rev_a="73", rev_b="81"), 1, "publish-", 0),
        ("one release +2, expect 1 -> REFUSED", post(rev_a="74", rev_b="81"), 1, "publish-", 1),
        ("one release unmoved, expect 1 -> REFUSED", post(rev_a="72", rev_b="81"), 1, "publish-", 1),
        ("FALSIFIER: the installer release is EXCLUDED by the matching filter, so its own "
         "revision not moving by 1 is not a finding",
         post(rev_a="73", rev_b="81"), 1, "publish-", 0),
        ("a release DISAPPEARING is always a problem, with no expectation set",
         post(drop_a=True), None, None, 1),
        ("a composition's uid changing = RECREATED, not updated in place",
         post(uid="u-NEW"), None, None, 1),
        ("FALSIFIER: an apiVersion move with the SAME uid is an in-place update, not a problem",
         post(av="composition.krateo.io/v1-8-129"), None, None, 0),
        ("FALSIFIER: managed count changing is REPORTED but is not by itself a problem",
         post(managed=65), None, None, 0),
        ("a Secret's resourceVersion moving is reported as WRITTEN, not as a problem",
         post(secret_rv="200"), None, None, 0),
        ("a Secret's KEY SET changing IS a problem", post(secret_keys=["password", "token"]), None, None, 1),
        # CompositionDefinitions. The first case is the one that protects a roll verification:
        # a CD left at False/Unavailable is what krateoEval maps to FAILED and the umbrella
        # inherits, so it must be REPORTED rather than passed over.
        ("a composition left at False/Unavailable IS a problem -- the post-#498 crash-loop signal",
         post(comp_ready="False/Unavailable"), None, None, 1),
        ("a composition at False/CreateOutcomeUnknown IS a problem too",
         post(comp_ready="False/CreateOutcomeUnknown"), None, None, 1),
        ("FALSIFIER: AvailablePartialCoverage is CONVERGED, not failed -- 15 of 86 on 057 read "
         "this, and firing on them would train the reader to ignore the check",
         post(comp_ready="True/AvailablePartialCoverage"), None, None, 0),
        ("FALSIFIER: ReconcilePaused is CONVERGED too -- healthy-but-paused is not failed",
         post(comp_ready="True/ReconcilePaused"), None, None, 0),
        ("a composition converging for 2h IS a problem -- the demo OOM case",
         post(comp_ready="False/Creating", comp_readyat=_ago(7200)), None, None, 1),
        ("a composition converging 2h but born 2h ago is NEVER EVALUATED, not a problem -- the "
         "internal-ca-bundle case, installer#1017",
         post(comp_ready="Unknown/ZeroEvidence", comp_readyat=_ago(7200),
              comp_createdat=_ago(7206)), None, None, 0),
        ("the SAME age, but born long before Ready moved, IS a problem -- it converged once and "
         "regressed",
         post(comp_ready="False/Creating", comp_readyat=_ago(7200),
              comp_createdat=_ago(600000)), None, None, 1),
        ("FALSIFIER: no createdAt -> classed STALLED, the conservative side, so a pre-#1017 "
         "snapshot still reports real stalls",
         post(comp_ready="False/Creating", comp_readyat=_ago(7200)), None, None, 1),
        ("FALSIFIER: never-evaluated but WITHIN the budget is in neither list",
         post(comp_ready="Unknown/ZeroEvidence", comp_readyat=_ago(60),
              comp_createdat=_ago(66)), None, None, 0),
        ("FALSIFIER: converging for 60s is a NORMAL bump and must NOT trip",
         post(comp_ready="False/Creating", comp_readyat=_ago(60)), None, None, 0),
        ("FALSIFIER: an OLD readyAt while CONVERGED is not a problem -- age alone proves nothing",
         post(comp_ready="True/Available", comp_readyat=_ago(4000000)), None, None, 0),
        ("FALSIFIER: converging with NO readyAt is NOT reported -- a gap in the capture must not "
         "become a false finding about the cluster",
         post(comp_ready="False/Creating", comp_readyat=None), None, None, 0),
        ("a CD left at False/Unavailable IS a problem",
         post(cd_ready="False/Unavailable"), None, None, 1),
        # The four cases below are a SET. Creating alone no longer decides anything: from
        # core-provider 2.13.108 `Available` requires a completed rollout, so Creating is the
        # normal state of a CD whose controller is rolling. What decides is the reason's verdict
        # and, for converging, how long it has been there.
        ("a CD at False/Creating PAST the budget is a problem -- the fixture's readyAt is a day\n          old, so this is the time bound catching it, not Creating by itself",
         post(cd_ready="False/Creating"), None, None, 1),
        ("a CD at False/Creating WITHIN the budget is NOT a problem -- without this case the one\n          above would pass equally if Creating were still reported outright, which is the noise\n          this change exists to remove",
         post(cd_ready="False/Creating", cd_readyat=_ago(60)), None, None, 0),
        ("a CD at False/ValuesSchemaMissing is a problem IMMEDIATELY, with no budget -- it is\n          classified `failed`, and waiting cannot clear a chart that ships no root schema",
         post(cd_ready="False/ValuesSchemaMissing", cd_readyat=_ago(5)), None, None, 1),
        ("a CD at an UNCLASSIFIED reason is a problem even when fresh -- an unknown reason is\n          where drift from core-provider's table shows up, so it must not be read as benign",
         post(cd_ready="False/SomeReasonNobodyClassified", cd_readyat=_ago(5)), None, None, 1),
        ("a CD DISAPPEARING is always a problem", post(cd_drop=True), None, None, 1),
        ("FALSIFIER: a CD chartVersion moving is REPORTED but is not by itself a problem --\n          that is exactly what a pin bump is supposed to do",
         post(cd_chart="1.8.130"), None, None, 0),
        ("FALSIFIER: readyAt moving while Ready stays True/Available is a re-transition, not a\n          failure -- it is the field that BOUNDS the bump window, so it must not raise on its own",
         post(cd_readyat="2026-11-16T21:00:00Z"), None, None, 0),
        ("FALSIFIER: identical definitions raise nothing, so the check above is not raising\n          unconditionally",
         post(), None, None, 0),
    ]
    # _coverage() IS NOT REACHED BY THE CASES ABOVE. It runs only inside capture(), against live
    # status; the fixtures set `coverage` directly. I discovered that by mutating _coverage() to
    # default absence to 0 and watching all 38 cases still pass -- the mutation could not reach
    # them. So the layer where an `or 0` would actually be written needs its own assertion.
    cov_unit = [
        ("absent stays None, never 0",
         {"unreadableChildrenCount": 3},
         {"managedChildrenCount": None, "healthEvaluatedChildren": None,
          "unreadableChildrenCount": 3}),
        ("a REAL zero is preserved as 0, not confused with absence",
         {"managedChildrenCount": 7, "healthEvaluatedChildren": 7, "unreadableChildrenCount": 0},
         {"managedChildrenCount": 7, "healthEvaluatedChildren": 7, "unreadableChildrenCount": 0}),
        ("an empty status yields all three as None",
         {},
         {"managedChildrenCount": None, "healthEvaluatedChildren": None,
          "unreadableChildrenCount": None}),
    ]
    bad_unit = 0
    for label, st, want in cov_unit:
        got = _coverage(st)
        ok = got == want
        bad_unit += 0 if ok else 1
        print("  %-4s _coverage: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%s got=%s" % (want, got))
    bad = 0
    for label, po, delta, match, want in cases:
        d = diff(PRE, po, expect_delta=delta, matching=match)
        got = len(d["problems"])
        ok = (got > 0) == (want > 0)
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want_problems=%s got=%s" % (want, d["problems"]))
    print("self-test: %d case(s), %d failure(s)"
          % (len(cases) + len(cov_unit), bad + bad_unit))
    return 1 if (bad or bad_unit) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("capture")
    c.add_argument("--context", required=True)
    c.add_argument("--secret", action="append", default=[],
                   metavar="NS/NAME", help="metadata only; never a value")
    c.add_argument("-o", "--out", required=True)
    g = sub.add_parser("diff")
    g.add_argument("pre"); g.add_argument("post")
    g.add_argument("--expect-release-revision-delta", type=int, default=None)
    g.add_argument("--releases-matching", default=None)
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if a.cmd == "capture":
        snap = capture(a.context, a.secret)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1, sort_keys=True)
        print("captured %d release(s), %d composition(s), %d definition(s), %d secret(s) -> %s"
              % (len(snap["releases"]), len(snap["compositions"]),
                 len(snap["definitions"]), len(snap["secrets"]), a.out))
        # Definitions join the emptiness refusal: a snapshot with no CDs cannot bound the
        # bump window, and reporting it as a baseline is how a check that never ran looks
        # exactly like one that passed.
        if not snap["releases"] or not snap["compositions"] or not snap["definitions"]:
            print("REFUSED: a snapshot with no releases or no compositions is not a baseline. "
                  "Check the context and try again -- an empty capture read as a baseline is "
                  "worse than none, because the diff will later report everything as new.")
            return 2
        # REPORTED ON A SINGLE READ, not only in the diff. demo's 8.5-hour outage needed no
        # "before": one capture carrying readyAt would have shown it outright. A finding that
        # only a diff can surface is a finding you cannot make during an incident.
        stalled, never = stuck_converging(snap)
        unreadable, unevaluated, broken = coverage_problems(snap)
        # THE INSTRUMENT MUST SAY WHEN IT SAW NOTHING. coverage_problems() skips any composition
        # missing the three scalars, which is correct -- absence is not zero -- but it means a
        # FIELD RENAME upstream turns every object into a skip and the coverage lines fall silent.
        # Silence would then read as full coverage, which is the exact inversion this capture
        # exists to prevent. So the denominator is printed whenever it is zero: a fleet on an
        # older core-provider legitimately has none, and that is worth knowing rather than
        # assuming.
        _with_cov = sum(1 for c in (snap.get("compositions") or {}).values()
                        if any(v is not None for v in (c.get("coverage") or {}).values()))
        if not _with_cov and snap.get("compositions"):
            print("\nCOVERAGE NOT READABLE: 0 of %d composition(s) carry any of "
                  "managedChildrenCount / healthEvaluatedChildren / unreadableChildrenCount, so "
                  "the coverage checks below examined NOTHING. Either this fleet predates the "
                  "fields or core-provider renamed them -- do not read the absence of coverage "
                  "findings as full coverage." % len(snap["compositions"]))
        if never:
            print("\nNEVER EVALUATED: %d composition(s) whose Ready has not moved since the "
                  "object was created. NOT a finding -- the age measures their lifetime, not a "
                  "stall, and this state can be permanent (e.g. a managed child whose kind has "
                  "no readiness semantics the cdc can read)." % len(never))
            for key, ready, at, age in never:
                print("  %-56s %-20s since creation %s  (%.1f h)" % (key, ready, at, age / 3600.0))
        # REPORTED, NOT A FINDING, for the same reason as NEVER EVALUATED: a kind with no
        # readiness model is a permanent state, so this would fire on every roll for ever. It is
        # printed because its ABSENCE is the informative case -- 76 of 81 children unevaluated is
        # what AvailablePartialCoverage looks like from the inside, and I described that
        # population as "converging" to two sessions today when core-provider EXCLUDES it.
        # AGGREGATE, NOT ONE LINE PER COMPOSITION. Measured on 057 2026-11-17: 32 of 89
        # compositions carry at least one unevaluated child and 939 of 1914 named children are
        # unevaluated fleet-wide. A per-object list is therefore 32 lines on EVERY capture of a
        # healthy cluster -- which is precisely the "fires for ever and teaches the reader to
        # skip the line" failure this script already warns about for NEVER EVALUATED. The
        # per-object detail is in the JSON; the printed form is the denominator.
        #
        # Nor is a per-object list informative here: core-provider's reason already names the
        # compositions where it matters most. childhealth.go availableReason() is
        # `unevaluated*2 > total`, so AvailablePartialCoverage means the rollup assessed a
        # MINORITY. Verified against all 88 compositions on the Available arm: 0 mismatches,
        # with the predicate inverted giving 88 of 88, so the comparison can distinguish.
        if unevaluated:
            _u = sum(n for _, n, _, _ in unevaluated)
            _t = sum(t for _, _, t, _ in unevaluated)
            # THE DENOMINATOR NAMES ITS POPULATION. _t sums only the compositions that HAVE a
            # gap, so it is not the fleet total: on 057 that is 939 of 1400 (67%) among the 32
            # affected, where the fleet figure is 939 of 1914 (49%). Two different claims, and
            # "939 of 1400 named children" reads as the second one.
            print("\nPARTIAL COVERAGE: %d composition(s) carry %d of THEIR OWN %d named "
                  "children unevaluated (%.0f%% of those, not of the fleet). NOT a finding -- "
                  "a kind with no "
                  "readiness model is existence-is-health and is EXCLUDED from the rollup, not "
                  "counted as converging. Per-composition detail is in the JSON; a GROWING count "
                  "is what `diff` reports." % (len(unevaluated), _u, _t, 100.0 * _u / _t))
        if stalled:
            print("\nFINDINGS: %d composition(s) converging longer than %ds, having previously "
                  "moved. CONVERGING IS NOT CONVERGED, and nothing in the platform bounds it yet "
                  "(core-provider#498)." % (len(stalled), CONVERGING_BUDGET_S))
            for key, ready, at, age in stalled:
                print("  %-56s %-20s since %s  (%.1f h)" % (key, ready, at, age / 3600.0))
            print("A workload crash loop reads as Creating, not failed, so this is what an "
                  "8.5-hour OOM outage looked like from the conditions. Decide before rolling "
                  "on top of it: a pin bump onto an unhealthy dependency is not safe.")
        # FINDINGS ACCUMULATE. Before 2026-11-17 the stalled block returned 3 directly, so a
        # coverage finding printed after it could never be reached -- the same shape of bug as a
        # wait loop that treats an empty list as settled.
        if broken:
            print("\nFINDINGS: %d composition(s) whose coverage scalars break core-provider's "
                  "OWN invariant, managed - evaluated - unreadable < 0. This is a finding about "
                  "THIS SCRIPT or about their contract, not about the cluster: do not trust the "
                  "coverage lines above until it is resolved." % len(broken))
            for key, total, evaluated, unread in broken:
                print("  %-56s managed=%d evaluated=%d unreadable=%d"
                      % (key, total, evaluated, unread))
        if unreadable:
            print("\nFINDINGS: %d composition(s) the rollup was REFUSED a read of children for. "
                  "THESE READ GREEN: core-provider's childhealth.go says the rollup is "
                  "\"fail-safe (unreadable / unmodeled children count healthy)\" and \"never "
                  "flips a working composition to NotReady\", and it calls the refusal \"a "
                  "DEFECT IN GENERATED RBAC -- ours, and actionable\"." % len(unreadable))
            for key, n, total, ready in unreadable:
                print("  %-56s %-20s %d of %d UNREADABLE" % (key, ready, n, total))
        return 3 if (stalled or unreadable or broken) else 0
    if a.cmd == "diff":
        pre = json.load(open(a.pre, encoding="utf-8"))
        post = json.load(open(a.post, encoding="utf-8"))
        d = diff(pre, post, a.expect_release_revision_delta, a.releases_matching)
        print(json.dumps(d, indent=1, sort_keys=True))
        if d["problems"]:
            print("\n%d PROBLEM(S) -- see above." % len(d["problems"]))
            return 1
        print("\nno problems.")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
