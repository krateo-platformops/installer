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
    per composition       apiVersion AND the krateo.io/composition-version label (they are
                          different things -- see the capture comment), status.managed COUNT,
                          Ready status + reason, Synced,
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
import datetime
import io
import os
import hashlib
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
# CreateWithheldByPolicy / RecoveryWithheldByPolicy: core-provider#598, vendored 2026-11-21.
# A management policy withheld the operation, so the desired state was NEVER APPLIED and
# nothing retries it on its own -- a roll that leaves a composition here has not landed, and
# a time-bounded wait on it never succeeds. Both ship in core-provider 2.13.120, NOT CUT YET,
# so these two branches are UNEXERCISED and a clean run proves nothing about them.
FAILED_REASONS = ("Unavailable", "CreateOutcomeUnknown", "HooksMayNotHaveRun",
                  "CreateWithheldByPolicy", "RecoveryWithheldByPolicy",
                  # core-provider#636, mirrored 2026-11-24. The 3rd consecutive failed attempt of a
                  # FIRST install; kept while a retry is in flight. core-provider asked for this
                  # reason to be reported on sight during the 0.3.705 roll and this tool could not
                  # have named it -- check-ready-reasons-current caught that it was unclassified.
                  "InstallFailing")

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
                     "ValuesSchemaMissing", "DerivedNameTooLong",
                     # core-provider#598, same reasoning as FAILED_REASONS above. A CD left
                     # here had its create withheld, so the definition exists and the chart
                     # was never applied.
                     "CreateWithheldByPolicy",
                     # oasgen-provider >= 0.85.0. MEASURED: 26 occurrences at 0.88.0 against 0 at
                     # 0.84.0, with NoCreateVerbDeclared 20 -> 24 as the positive control in the
                     # same probe. It reports that rest-dynamic-controller has SPENT its retry
                     # budget for an object and dropped it: --max-error-retries defaults to 5,
                     # Forget() runs on SUCCESS ONLY, so the count never resets and the budget is
                     # spent once per OBJECT LIFETIME. Every later resync is dropped at DEBUG with
                     # no condition, which is why the reason had to be added upstream at all --
                     # before it, a stopped-retrying object and a still-retrying one were
                     # indistinguishable from `kubectl get`.
                     #
                     # WHY IT GOES IN BEFORE THE 0.88.0 PIN AND NOT AFTER: without it, a resource
                     # finally reporting that it stopped retrying is classified as UNRECOGNISED
                     # condition rather than a failure. A new signal arriving and being silently
                     # dropped by the instrument watching for it is strictly worse than not having
                     # the signal, because the roll would read as clean. Found because
                     # aruba-provider-kog asked an unrelated question about a pin I do not carry.
                     "CreateRefusedPersistently")

# compositionDefinition.converging, mirrored for the same dependency-free reason.
CD_CONVERGING_REASONS = ("Creating", "Deleting")

# THE COMPOSITION Ready CLASSIFICATION, all four buckets, mirrored from hack/ready-reasons.yaml
# which is the source of truth. Duplicated as literals because this tool must stay dependency-free
# (`python3 -I`, no PyYAML, run during a roll on whatever machine holds it); check-roll-snapshot
# asserts each tuple EQUALS its list in that file, so the duplication is gated rather than trusted.
#
# WHY ALL FOUR AND NOT JUST `failed`. Until 2026-11-24 only the failed set existed here and the
# classifier was a membership filter with no else, so an unrecognised reason was silently dropped.
# Adding an unclassified bucket on top of the failed set ALONE was wrong in the other direction and
# the self-test caught it immediately: AvailablePartialCoverage (15 of 86 compositions on 057),
# ReconcilePaused and ZeroEvidence all landed in it. Reporting those would train the reader to
# ignore the check, which is worse than the gap it closes. So the bucket means "in none of the
# four", not "not failed".
COMP_CONVERGED_REASONS = ("Available", "AvailablePartialCoverage", "ReconcileGracefullyPaused",
                          "ReconcilePaused", "UpdateWithheldByPolicy")
COMP_CONVERGING_REASONS = ("Creating", "HelmOperationInProgress", "PendingHelmOperation",
                           "ChildrenForbidden", "InventoryNotRecorded", "Deleting")
# ZeroEvidence / ObserveFailed are owned by never_evaluated() and stuck_converging(), which read
# the coverage SCALARS rather than the reason -- see the block at the top of never_evaluated().
COMP_UNEVALUATED_REASONS = ("ZeroEvidence", "ObserveFailed")

# MigratedValuesValid, core-provider#614 (PR #622, 2.13.125). A SEPARATE condition type, not a
# Ready reason -- core-provider confirmed it never touches Ready, so the Ready classification above
# is unaffected and no existing gate sees a new Ready reason.
#
# THE THREE STATES ARE NOT A TRUE/FALSE PAIR plus noise. From the vendored rows:
#   True/AllValid                   every instance passed a dry-run create through the served version
#   False/ValuesRefusedByNewVersion some instances hold values the served version REFUSES; ratcheting
#                                   let them migrate, so they are stored and the failure lands on the
#                                   next write that touches a refused field
#   Unknown/ValidationIncomplete    no instance was refused, but some could not be JUDGED
#
# Unknown is NOT a milder False: nothing was refused. It means the check did not complete, which is
# the same distinction as unsynced children counting as UNKNOWN rather than unhealthy.
MV_FAILED_REASONS = ("ValuesRefusedByNewVersion",)
MV_UNVERIFIED_REASONS = ("ValidationIncomplete",)


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


# CHILDREN'S OWN `Synced`, BECAUSE THE PARENT DOES NOT CARRY IT. The gap this closes was found
# the expensive way on krateo-demo 2026-11-17: KeycloakServiceAccountRoleMapping
# krateo-identity-lifecycle-roles sat at Ready=Unknown/ObserveFailed AND
# Synced=False/ReconcileError -- it could not reconcile at all -- while its parent composition read
# Ready=True/AvailablePartialCoverage. The child was one of 8 the rollup never health-evaluated, so
# it was EXCLUDED from the verdict rather than counted against it. Every instrument I had read that
# cluster as healthy. acmp found it by reading the child directly.
#
# core-provider's #575 (merged, not yet in a tag) will name unobservable children in the parent's
# message and count them in status.unevaluatedChildren.notObserved. That covers ObserveFailed and
# NotObserved. It does NOT cover a child failing for any other reason, and demo's child was ALSO
# Synced=False/ReconcileError -- a signal its own controller wrote and nothing aggregated. So this
# stays mine.
#
# ONLY KRATEO-GROUP CHILDREN ARE CHECKED. A Deployment or a Role has no `Synced` condition, so
# asking for one would produce noise, not findings; the children that carry it are the krateo CRs
# a composition owns. On demo's realm composition that is 13 of 15 managed children.
#
# ONE LIST PER KIND, NOT ONE GET PER CHILD. The distinct (group, resource) pairs across every
# composition are collected first and listed once each -- on 057 that is tens of LISTs rather than
# ~1800 GETs. Children are then attributed back by namespace and name.
#
# ONLY THE FAILING CHILDREN ARE STORED, with the denominator beside them. Capturing all ~1800 would
# bloat every snapshot for no gain, and a bare list of failures with no denominator cannot
# distinguish "none failing" from "none checked" -- the distinction this file keeps making.
KRATEO_GROUP_SUFFIX = ".krateo.io"

# A child that EXISTS and carries no `Synced` condition. Distinct from None, which means the
# capture could not resolve the child at all.
NO_SYNCED = "<no-synced-condition>"


def _child_kinds(compositions):
    """The distinct (group, resource) pairs among krateo-group managed children."""
    kinds = set()
    for c in compositions.values():
        for m in (c.get("managedRefs") or []):
            av = m.get("apiVersion") or ""
            if "/" not in av:
                continue                      # a core-group child (v1) has no Synced condition
            group = av.split("/", 1)[0]
            if not group.endswith(KRATEO_GROUP_SUFFIX):
                continue
            res = m.get("resource")
            if res:
                kinds.add((group, res))
    return sorted(kinds)


def unsynced_children(snap):
    """Compositions with a krateo-group child whose own `Synced` is not True.

    Returns (findings, checked, no_cond, unknown). `checked` is the DENOMINATOR: children that
    exist AND carry a `Synced` condition. The other two are reported separately and never folded
    into it, because they mean different things:

      no_cond   the child exists and its KIND has no `Synced` condition -- portal widgets
                (flexes, cards, buttons, tables, ...) and similar rendered objects. A FACT about
                the kind. On demo this is the large majority.
      unknown   the capture could not resolve the child at all. A GAP in the capture. A child
                that could not be read is not healthy and is not a finding either.

    Collapsing those two made this check report 80% blind on its first run against demo when it
    was fully sighted on every kind that actually carries the condition.
    """
    findings, checked, no_cond, unknown = [], 0, 0, 0
    for key, c in sorted((snap.get("compositions") or {}).items()):
        for ch in (c.get("childrenSynced") or []):
            st = ch.get("synced")
            if st is None:
                unknown += 1            # the capture could not resolve it: a GAP, not a state
                continue
            if st == NO_SYNCED:
                no_cond += 1            # the kind carries no such condition: a FACT, not a gap
                continue
            checked += 1
            if not str(st).startswith("True/"):
                findings.append((key, ch.get("child"), st))
    return findings, checked, no_cond, unknown


def never_evaluated_by_coverage(c):
    """True / False / None: did the rollup assess NOTHING for this composition?

    Returns None when the coverage scalars cannot answer, so the caller falls back.

    THE PREDICATE, from core-provider (cdc childhealth.go:1473, coverageScalars):
        healthEvaluatedChildren = managedChildrenCount - unevaluated - unreadableChildrenCount
    every child lands in EXACTLY ONE bucket. So managed > 0 with evaluated == 0 and
    unreadable == 0 means every named child was unevaluated -- a kind with no readiness model,
    or asserted-ready. No timestamps involved.

    WHY THIS REPLACES A TIME WINDOW. The old test compared readyAt to createdAt within
    NEVER_EVALUATED_GRACE_S. Measured on demo 2026-11-17: selfservice-status took 111 s from
    creation to its first and only Ready write, fell outside the window, and was reported as
    STALLED FOR 226 HOURS -- a nine-day stall that never happened, which would have gone into a
    roll report as a reason not to roll. The window was always a stand-in for the thing I could
    not measure. Now I can measure it.

    THREE WAYS TO GET THIS WRONG, each named by core-provider and each guarded here:

    1. evaluated > 0 does NOT mean any child is healthy. A child recorded in status.managed but
       MISSING from the cluster counts as EVALUATED -- absent is a subset of converging
       (childhealth.go:178). So this returns False for an all-absent composition, which is
       correct: it is genuinely converging, not unevaluable.
    2. Do NOT key on the Ready reason. evaluated == 0 does not fix Ready to ZeroEvidence: a
       blueprint projecting its own status.health ready=true reads Available on that projection
       with zero rollup evidence. unreadable > 0 with evaluated == 0 is ChildrenForbidden, and
       managed == 0 is InventoryNotRecorded. The scalars decide, not the reason.
    3. Do NOT pair these scalars with a timestamp from the same pass. They are written only on
       passes where the rollup actually ran (support.go:455, `if rollupRan`) and otherwise keep
       their previous values, so they describe the LAST rollup rather than this moment. This
       predicate therefore uses them ALONE and never alongside readyAt.

    managed == 0 returns None rather than True: with no inventory recorded there is no coverage
    evidence in either direction, and calling that "never evaluated" would state a fact about the
    composition from the absence of one.
    """
    cov = c.get("coverage") or {}
    total = cov.get("managedChildrenCount")
    evaluated = cov.get("healthEvaluatedChildren")
    unread = cov.get("unreadableChildrenCount")
    if total is None or evaluated is None or unread is None:
        return None
    if total <= 0:
        return None
    return evaluated == 0 and unread == 0


def stuck_converging(snap, budget_s=CONVERGING_BUDGET_S, now=None, prior=None):
    """Not-converged compositions, split into (stalled, never_evaluated).

    Each list holds (key, Ready, readyAt, age_seconds). A composition is NEVER EVALUATED when
    never_evaluated_by_coverage() says so from core-provider's coverage scalars; only when those
    cannot answer does it fall back to the old heuristic, readyAt within NEVER_EVALUATED_GRACE_S
    of createdAt. The scalars are evidence, the window was a guess.

    Without a createdAt it is classed STALLED -- the conservative side on purpose, because
    under-reporting a real stall is the failure this bound exists to prevent, and a snapshot taken
    before createdAt was captured (anything before installer#1017) has none.

    A composition with no readyAt is in NEITHER list: absence of the field is not evidence of
    staleness, and guessing would turn a gap in the capture into a false finding about the cluster.

    PRIOR carries the age forward. Pass the pre-roll snapshot's compositions and, for any
    composition whose Ready VALUE is unchanged, the EARLIER readyAt is used. See the block in the
    body for why; the direction is deliberate -- it can only ever lengthen a measured age.
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
        at = c.get("readyAt")
        t = _parse(at)
        # CARRY THE AGE FORWARD when Ready's VALUE is unchanged since the prior snapshot.
        #
        # Measured on 057's 0.3.673 roll: 52 of 90 compositions had readyAt MOVE while Ready stayed
        # byte-identical. agentiko-mcp-server spotted it; I measured the scale and concluded "the
        # cdc rewrites lastTransitionTime", which was WRONG. core-provider traced it (#577):
        # chart-inspector becomes unreachable during a core-provider roll, the cdc's Observe fails
        # and writes Ready=False/Unavailable (cdc assessedfailure.go:30), and it recovers ~35 s
        # later. unstructured-runtime's condition.Upsert (v1.27.1 condition.go:297-305) PRESERVES
        # lastTransitionTime on a same-status write, so a moved LTT is proof of a real transition,
        # not of a rewrite -- I had that inference exactly backwards.
        #
        # Either way the consequence for THIS bound is the same and is why the fix stays: a brief
        # dip RESETS the age without the composition having made any progress. A nine-day stall
        # reads as four minutes old immediately after the roll most likely to have wedged it --
        # the check is blind at precisely the moment it exists for.
        #
        # Take the EARLIER of the two timestamps, so this can only ever LENGTHEN a measured age.
        # Under-reporting a real stall is the failure this bound exists to prevent.
        if prior is not None and ready is not None:
            p = (prior or {}).get(key) or {}
            if p.get("Ready") == ready:
                pt = _parse(p.get("readyAt"))
                if pt is not None and (t is None or pt < t):
                    t, at = pt, p.get("readyAt")
        if t is None:
            continue
        age = (now - t).total_seconds()
        if age <= budget_s:
            continue
        row = (key, ready, at, age)
        # THE SCALARS DECIDE WHEN THEY CAN. They are direct evidence about whether the rollup
        # assessed anything; the timestamp comparison below is a heuristic that stood in for them
        # and got selfservice-status wrong by nine days. Fallback order, not a blend.
        by_cov = never_evaluated_by_coverage(c)
        if by_cov is True:
            never.append(row)
            continue
        if by_cov is False:
            stalled.append(row)
            continue
        born = _parse(c.get("createdAt"))
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


TOOL_SHA_LEN = 16


def tool_identity():
    """This script's own sha256, so a snapshot names the instrument that produced it.

    WHY A SNAPSHOT MUST CARRY THIS. Measured 2026-11-25: three snapshot files in one scratchpad
    were byte-identical (sha256 881a39bf...), and I concluded they were copies of one capture. Then
    I measured the cluster for the window they spanned -- 0 helm releases updated, 0 objects created
    -- so a deterministic capture at ANY two times inside it would be byte-identical. The matching
    bytes were therefore expected from a WORKING instrument on a frozen cluster, and a real capture
    could not be distinguished from a copy IN EITHER DIRECTION. A filename and an mtime are not
    provenance: `cp` resets both.

    AND THE TOOL ITSELF MOVES. installer#1165 added observedGeneration rows and changed this file's
    sha from 3183c8cb to fcbfb7fe the same morning a peer was holding a baseline taken with the old
    one. Diffing across that boundary would have shown dozens of "new" fields that are the tool
    changing, not the cluster. The peer caught it only because they named the sha they expected.
    """
    try:
        with io.open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:TOOL_SHA_LEN]
    except (IOError, OSError, NameError):
        return "unknown"


def provenance(ctx):
    return {
        "captured_at": datetime.datetime.now(datetime.timezone.utc)
                               .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool_sha256": tool_identity(),
        "context": ctx,
    }


def capture(ctx, secrets=()):
    snap = {"context": ctx, "releases": {}, "compositions": {},
            "definitions": {}, "secrets": {},
            # STAMPED FIRST, before any cluster read, so the time is the capture's START. A time
            # written afterwards would date a snapshot by when its slowest query returned, which on
            # a large cluster is minutes later and on a failed query is never.
            "provenance": provenance(ctx)}

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
            # apiVersion IS NOT THE GVK-ARRIVAL FIELD, and this capture stored only it until
            # 2026-11-21. In a response `apiVersion` is whatever SERVED version the client
            # negotiated, not a property of the stored object -- so it can move with no change to
            # the composition at all, purely because the set of served versions changed under a
            # roll. The field that tracks arrival is the `krateo.io/composition-version` LABEL,
            # stamped at admission and rewritten when the parent re-renders the CR at the new
            # apiVersion (core-provider, measured read-only on 057 2026-11-21).
            #
            # MEASURED CONSEQUENCE, which is why both are now stored. On 0.3.697 I read
            # `apiVersion` by hand four minutes after the charts had migrated, got v1-8-141 while
            # the CDs served v1-8-154, and reported to another session that the instances had not
            # arrived. Two errors at once: the wrong field, AND read during the handover window
            # while both versions were still served. The portal CRDs now serve only v1-8-154 and
            # all 17 read label == apiVersion == v1-8-154, so nothing was wrong with the cluster.
            #
            # With both stored the diff can tell three cases apart instead of two:
            #   label moved                  the GVK genuinely arrived
            #   apiVersion moved, label not  a READ ARTIFACT -- negotiation, or a served-version
            #                                prune between the two captures
            #   both moved, same uid         an in-place update (the existing falsifier)
            snap["compositions"][key] = {
                "apiVersion": o["apiVersion"],
                # The composition controller writes its own status.observedGeneration on a
                # successful pass (it predates core-provider#644), so unlike a definition this is
                # expected to be present. A PAUSED composition legitimately stays behind.
                "generation": o["metadata"].get("generation"),
                "observedGeneration": st.get("observedGeneration"),
                "compositionVersion": (o["metadata"].get("labels") or {}).get(
                    "krateo.io/composition-version"),
                "hooksUnverified": hooks_unverified,
                "coverage": _coverage(st),
                # Only the KRATEO-GROUP refs, which are the ones carrying a `Synced`
                # condition. A Deployment or Role has none, so storing them would add rows that
                # can never produce a finding. On demo's realm composition this is 13 of 15.
                "managedRefs": [
                    {"apiVersion": m.get("apiVersion"), "resource": m.get("resource"),
                     "namespace": m.get("namespace"), "name": m.get("name")}
                    for m in (st.get("managed") or [])
                    if "/" in (m.get("apiVersion") or "")
                    and (m["apiVersion"].split("/", 1)[0]).endswith(KRATEO_GROUP_SUFFIX)
                ],
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
                # syncedAt + syncedMessage, ADDED 2026-11-18 for a measurement that cannot be taken
                # without them. core-provider#579 keeps a composition's Ready at its last value for
                # up to 5 minutes while chart-inspector gives no answer, and signals the grace on
                # SYNCED: status False, reason ReconcileError, and a MESSAGE ending
                # "Ready is kept as last assessed until <RFC3339>".
                #
                # Status+reason alone cannot distinguish that grace from an ordinary
                # False/ReconcileError -- the reason string is deliberately unchanged, because
                # core-provider did not want a new reason consumers would have to learn. The
                # DEADLINE in the message is the only thing that separates "deliberately holding
                # the last known Ready until T" from "this reconcile failed". So a capture that
                # keeps reason but drops message can see the grace and not know it is the grace.
                #
                # syncedAt matters for the same reason readyAt does on the line above: it tells a
                # fresh dip from a stale error. A composition sitting at False/ReconcileError for
                # nine days and one that entered it 30 seconds ago read identically without it --
                # and during a roll that difference is the whole question.
                "syncedAt": (cmap.get("Synced") or {}).get("lastTransitionTime"),
                "syncedMessage": (cmap.get("Synced") or {}).get("message"),
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
            # core-provider#614 / PR #622, 2.13.125. After a version migration core-provider
            # dry-run-creates each instance through the NEW version and names those it refuses.
            # EVERY INSTALLER PIN BUMP IS ONE OF THOSE MIGRATIONS, so this condition reports a
            # thing this tool's own rolls cause -- which is why it is captured rather than left to
            # a later sweep.
            #
            # ABSENT IS NOT VALID. core-provider's words: the condition appears only once a
            # migration has moved at least one instance. So None here means "no migration since the
            # release", and a reader treating it as healthy would credit every un-migrated
            # definition with a check that never ran. Captured as None rather than defaulted, and
            # classify_migrated() below refuses to call None a pass.
            "MigratedValuesValid": ("%s/%s" % (mv.get("status"), mv.get("reason", ""))
                                    if (mv := conds.get("MigratedValuesValid")) else None),
            # generation vs status.observedGeneration. FOUND BY ACCIDENT 2026-11-25: I used
            # `observedGeneration` as the POSITIVE CONTROL for an unrelated grep and got ZERO hits
            # across hack/ and .github/scripts/ -- this tool had never read it, while keycloak's
            # rows report "58/58 observedGeneration == generation" and I had been relaying that
            # figure as part of what cleared a core-provider release.
            #
            # BOTH ARE CAPTURED AS-IS, INCLUDING None. core-provider: the field exists on a
            # CompositionDefinition only from 2.13.130, and 057 runs 2.13.129 -- so ABSENT MEANS
            # UNKNOWN, NOT BEHIND. A check that read None as 0 would report every definition on
            # 057 as a finding on its first run.
            "generation": m.get("generation"),
            "observedGeneration": st.get("observedGeneration"),
        }

    # Secrets: metadata only. Never a value, and the data KEY NAMES only so a key appearing or
    # disappearing is visible.
    # CHILDREN'S OWN `Synced`. One LIST per distinct (group, resource), then attribute by
    # namespace+name -- tens of calls rather than ~1800. A kind that cannot be listed leaves its
    # children with synced=None, which unsynced_children() counts as UNKNOWN and never as healthy:
    # a read that failed is not a fact about the cluster.
    _index = {}
    for group, resource in _child_kinds(snap["compositions"]):
        out = _run(["kubectl", "--context", ctx, "get",
                    "%s.%s" % (resource, group), "-A", "-o", "json"])
        if out is None:
            continue
        try:
            items = json.loads(out).get("items", [])
        except ValueError:
            continue
        for o in items:
            conds = {c["type"]: "%s/%s" % (c["status"], c.get("reason", ""))
                     for c in ((o.get("status") or {}).get("conditions") or [])}
            # NO_SYNCED is NOT the same as unresolved. Measured on demo 2026-11-17: 768 of 953
            # krateo-group children came back "unknown", and almost all were portal WIDGET kinds
            # -- flexes, cards, buttons, paragraphs, listies, tables, pageheaders -- which are
            # rendered objects carrying no `Synced` condition at all. Folding those into "unknown"
            # made the check read 80% blind when it was in fact fully sighted on every kind that
            # has the condition. A kind without the condition is a FACT about the kind; a kind that
            # could not be listed is a gap in the capture. They must not share a bucket.
            _index[(group, resource, o["metadata"].get("namespace"), o["metadata"]["name"])] = \
                conds.get("Synced") or NO_SYNCED
    for key, c in snap["compositions"].items():
        rows = []
        for m in (c.get("managedRefs") or []):
            av = m.get("apiVersion") or ""
            group = av.split("/", 1)[0] if "/" in av else ""
            k = (group, m.get("resource"), m.get("namespace"), m.get("name"))
            rows.append({"child": "%s/%s/%s" % (m.get("resource"), m.get("namespace"),
                                                m.get("name")),
                         "synced": _index.get(k)})
        c["childrenSynced"] = rows
        # managedRefs served their purpose; dropping them keeps the snapshot small and leaves
        # exactly one place where a child's state lives.
        c.pop("managedRefs", None)

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


DEFINITION_DIFF_EXCLUDED = frozenset()
"""Captured definition keys that `diff` deliberately does NOT compare.

EMPTY ON PURPOSE, and it stays empty until there is a measured reason. Every name added here must
carry why, because an entry makes the diff blind to that key -- the exact failure this replaced.
`assert_diff_compares_every_captured_key()` in the self-test fails if a captured key is neither
compared nor listed here, so a future key cannot be forgotten the way MigratedValuesValid was.
"""


def provenance_problems(pre, post):
    """Reasons this PAIR cannot be diffed. Empty list means the pair is auditable.

    These are REFUSALS, not warnings, and there is deliberately no flag to bypass them. Every one
    describes a diff whose output would look plausible and mean something other than what the reader
    takes it to mean -- which is worse than no diff at all, because a gap is visible and a wrong
    attribution is not.
    """
    bad = []
    a, b = (pre or {}).get("provenance"), (post or {}).get("provenance")
    for label, prov in (("pre", a), ("post", b)):
        if not isinstance(prov, dict):
            bad.append(
                "the %s snapshot carries NO provenance block, so it cannot be dated or attributed "
                "to a tool version. Written by a roll-snapshot older than the provenance stamp; "
                "re-capture rather than diff it. An undatable snapshot is what let three identical "
                "files pass for three observations." % label)
    if bad:
        return bad
    if a.get("tool_sha256") != b.get("tool_sha256"):
        bad.append(
            "TOOL VERSIONS DIFFER: pre was captured by %s, post by %s. A field this tool started "
            "recording between those versions appears in the post snapshot only, and reads as a "
            "cluster change. Re-capture BOTH ends with one version." % (
                a.get("tool_sha256"), b.get("tool_sha256")))
    if a.get("context") != b.get("context"):
        bad.append(
            "DIFFERENT CLUSTERS: pre is %s, post is %s. Every row would differ and the diff would "
            "describe nothing." % (a.get("context"), b.get("context")))
    pa, pb = a.get("captured_at"), b.get("captured_at")
    if pa and pb and pb < pa:
        bad.append(
            "THE PAIR IS TRANSPOSED: post (%s) is EARLIER than pre (%s). Reversed, every bump reads "
            "as a rollback and every new object as a deletion." % (pb, pa))
    if pa and pb and pa == pb:
        bad.append(
            "BOTH ENDS CARRY THE SAME capture time (%s), so they are one observation used twice. "
            "The diff would be empty whatever the cluster did." % pa)
    return bad


def diff(pre, post, expect_delta=None, matching=None):
    """Pure, so the self-test drives it without a cluster."""
    out = {"releases": {}, "compositions": {}, "definitions": {},
           "secrets": {}, "problems": []}

    # BEFORE anything is compared. A provenance refusal is not one problem among many: it says the
    # rows below cannot be attributed, so reporting them next to it would invite reading them anyway.
    _prov = provenance_problems(pre, post)
    if _prov:
        out["problems"].extend("UNAUDITABLE PAIR: %s" % p for p in _prov)
        out["refused"] = True
        return out

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
    _stalled, _never = stuck_converging(post, prior=pre.get("compositions"))
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
    _unsynced, _ch_checked, _ch_nocond, _ch_unknown = unsynced_children(post)
    # ABSOLUTE, not a delta: a child that cannot reconcile is a problem whatever the pre-state
    # said. And the denominator goes in the output so a zero can be told from a blind run.
    out["child_synced"] = {"checked": _ch_checked, "no_condition": _ch_nocond,
                           "unresolved": _ch_unknown}
    for key, child, st in _unsynced:
        out["problems"].append(
            "CHILD NOT SYNCED: %s reads %s, under composition %s which reads %s. The parent's "
            "rollup EXCLUDES a child it never health-evaluated, so this does not surface there."
            % (child, st, key, (post.get("compositions", {}).get(key) or {}).get("Ready")))
    if _ch_unknown:
        out["problems"].append(
            "CHILD SYNCED INCOMPLETE: %d managed child(ren) could not be resolved, so the "
            "child-synced result above is not complete." % _ch_unknown)
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
        # syncedAt AND syncedMessage ARE CAPTURED BUT DELIBERATELY NOT DIFFED. Both move on every
        # real transition, and core-provider#579's grace message embeds an RFC3339 deadline -- so a
        # message that advances by one reconcile would read as a CHANGE on every affected
        # composition. That is the 52-of-90 noise the agentiko-mcp-server session had to filter out
        # of this very diff: lastTransitionTime-keyed rows swamping three real state changes.
        #
        # So the split is by purpose rather than by availability. The diff's verdict stays keyed on
        # status+reason, which is the granularity a roll is pass/fail at. The two new fields are
        # FORENSICS, read out of the JSON when a specific question needs them -- "is this
        # False/ReconcileError the #579 grace, and until when?" cannot be answered from the reason,
        # because core-provider deliberately did not mint a new reason string for it.
        d = {f: [a.get(f), b.get(f)]
             for f in ("apiVersion", "compositionVersion", "managed", "Ready", "Synced", "uid")
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
    # THREE-WAY, not a membership filter. Until 2026-11-24 this was a bare comprehension over
    # FAILED_REASONS with NO else, so a composition reading an UNRECOGNISED reason was neither
    # reported as failed nor reported at all -- silently dropped. The definitions classifier below
    # has had an `else -> unclassified` bucket all along; compositions did not, and the asymmetry
    # was invisible because every reason core-provider had shipped was already in the tuple.
    # Found 2026-11-23 when core-provider announced `InstallFailing` BEFORE merging it: a component
    # whose install hooks never pass would have produced NO FINDING AT ALL on this path.
    # Fixing the CLASS, not the instance: adding one string to the tuple would leave the next new
    # reason equally silent. Same shape as the blind diff shipped in #1136 and fixed in #1139.
    comps = (post.get("compositions") or {})
    failed, comp_other = {}, {}
    for k, v in comps.items():
        ready = str(v.get("Ready", ""))
        if ready == "True/Available":
            continue
        reason = ready.split("/", 1)[-1]
        if reason in FAILED_REASONS:
            failed[k] = v.get("Ready")
        elif reason in COMP_CONVERGING_REASONS or reason in COMP_CONVERGED_REASONS \
                or reason in COMP_UNEVALUATED_REASONS:
            continue            # owned by stuck_converging() / never_evaluated(), or healthy
        elif ready in ("", "None") or reason in ("", "None"):
            continue            # a gap in the capture is not a finding about the cluster
        else:
            comp_other[k] = v.get("Ready")
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

    if comp_other:
        out["problems"].append(
            "COMPOSITIONS with an UNCLASSIFIED reason: %d of %d -- %s. Not True/Available, not a "
            "known failed reason, and not a known converging one. This bucket exists because a "
            "membership filter silently DROPS a reason nobody has taught it: core-provider ships "
            "new Ready reasons (InstallFailing is one), and an unrecognised reason must be LOUD "
            "rather than absent. Classify it -- if it is a failure add it to FAILED_REASONS and to "
            "hack/ready-reasons.yaml, if it is transient add it to COMP_CONVERGING_REASONS."
            % (len(comp_other), len(comps), json.dumps(comp_other, sort_keys=True)))

    pd, pod = pre.get("definitions", {}), post.get("definitions", {})
    out["definitions"]["gone"] = sorted(set(pd) - set(pod))
    out["definitions"]["new"] = sorted(set(pod) - set(pd))
    dch = {}
    for k in sorted(set(pd) & set(pod)):
        a, b = pd[k], pod[k]
        # EVERY captured key is compared, MINUS an explicit exclusion list. It was an INCLUDE
        # list of six names until 2026-11-23, and that shape fails silently: #1136 added
        # `MigratedValuesValid` to the capture and to the classifier, the self-test went 61 -> 73
        # cases, CI was green, and the diff did not compare the new key at all. Found by planting
        # None -> "False/MigrationFailed" on cert-manager/cert-manager and getting an EMPTY diff.
        # `compositionId` had been uncompared the same way, with no comment claiming it was meant to
        # be -- a changed compositionId on the same definition key means it now belongs to a
        # DIFFERENT composition, which is exactly the silent re-parenting a roll can cause.
        #
        # An include list fails CLOSED-BLIND (a new key is silently uncompared); an exclusion list
        # fails OPEN-NOISY (a new key is compared, perhaps spuriously). For an instrument whose job
        # is to report change, noisy is strictly safer than blind: noise is visible and gets fixed,
        # blindness reads exactly like "nothing happened".
        #
        # Measured before switching, on a quiet pre/post pair from 057 (79 definitions, nothing
        # between the two captures): 0 of 79 rows would differ on compositionId and 0 of 79 on
        # MigratedValuesValid. So neither addition brings noise with it.
        d = {f: [a.get(f), b.get(f)]
             for f in sorted(set(a) | set(b))
             if f not in DEFINITION_DIFF_EXCLUDED and a.get(f) != b.get(f)}
        if d:
            dch[k] = d
    out["definitions"]["changed"] = dch
    if out["definitions"]["gone"]:
        out["problems"].append("COMPOSITIONDEFINITIONS DISAPPEARED: %s" % out["definitions"]["gone"])

    # observedGeneration LAGGING generation, at the END of the window. Added 2026-11-25 after a
    # reader control exposed that this tool had never read the field at all: I used
    # `observedGeneration` as the POSITIVE CONTROL for an unrelated grep, got ZERO hits across
    # hack/ and .github/scripts/, and found that keycloak's rows report "58/58
    # observedGeneration == generation" while my own converged/not-converged verdict ignored it.
    #
    # THREE WAYS THIS WOULD HAVE MISFIRED, all named by core-provider BEFORE I wrote it:
    #   (1) ON A CompositionDefinition THE FIELD EXISTS ONLY FROM core-provider 2.13.130. 057 runs
    #       2.13.129, so it is ABSENT there -- and ABSENT MEANS UNKNOWN, NOT BEHIND. Gating on
    #       presence is why this does not report all 79 definitions on 057 on its first run.
    #   (2) IT CAN BE BEHIND LEGITIMATELY: during a version bump's update (Ready kept from the old
    #       version), while paused, under a withheld update, or while Observe errors
    #       (NotObserved). So "any pair disagrees" would fire at EVERY chart bump -- which is what
    #       a roll IS. This runs in `diff`, which the caller invokes AFTER the window closes, and
    #       it reports the Ready reason beside each lag so a legitimate one is recognisable.
    #   (3) ON A COMPOSITION the composition controller writes its own observedGeneration on a
    #       successful pass (predating core-provider#644), and a PAUSED composition legitimately
    #       stays behind -- so the converged/paused reasons are excluded here too.
    #
    # A check that fires on every roll is worse than no check, because it trains the reader to
    # skip the line. That is the same reason AvailablePartialCoverage is not in FAILED_REASONS.
    lag = {}
    for scope, rows in (("definition", pod), ("composition", post.get("compositions", {}))):
        for k, v in sorted(rows.items()):
            g, og = v.get("generation"), v.get("observedGeneration")
            if g is None or og is None:
                continue  # (1) absent means unknown
            if og >= g:
                continue
            ready = v.get("Ready") or "<none>"
            reason = str(ready).split("/", 1)[-1]
            # (2)/(3) a lag with a converged-or-paused reason is the legitimate case
            if reason in ("ReconcilePaused", "ReconcileGracefullyPaused", "UpdateWithheldByPolicy",
                          "NotObserved"):
                continue
            lag["%s %s" % (scope, k)] = {"generation": g, "observedGeneration": og, "Ready": ready}
    if lag:
        out["problems"].append(
            "observedGeneration BEHIND generation on %d object(s) at the END of the window -- %s. "
            "The controller has not yet acknowledged the spec it was given, so a Ready condition "
            "here may describe the PREVIOUS generation. Absent observedGeneration is NOT reported "
            "(on a CompositionDefinition the field exists only from core-provider 2.13.130), and a "
            "lag whose Ready reason is paused/withheld/NotObserved is NOT reported either, because "
            "those lag legitimately."
            % (len(lag), json.dumps(lag, sort_keys=True)))
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
    # MigratedValuesValid, reported SEPARATELY from the Ready classification because it is a
    # different condition type and core-provider confirmed it never touches Ready. A definition can
    # be Ready=True/Available and still hold instances the served version refuses.
    mv_failed, mv_unver = {}, {}
    for k, v in pod.items():
        raw = v.get("MigratedValuesValid")
        if not raw:
            continue  # ABSENT means no migration since the release -- NOT a pass. Never counted.
        reason = str(raw).split("/", 1)[-1]
        if reason in MV_FAILED_REASONS:
            mv_failed[k] = raw
        elif reason in MV_UNVERIFIED_REASONS:
            mv_unver[k] = raw
    if mv_failed:
        out["problems"].append(
            "MIGRATED VALUES REFUSED: %d definition(s) hold instances the served version REFUSES"
            " -- %s. Validation ratcheting let those instances migrate, so they are STORED and"
            " nothing is broken yet: the failure lands on the next write that touches a refused"
            " field, which may be a controller's and may be days later. A pin bump is one of these"
            " migrations, so this is attributable to the roll."
            % (len(mv_failed), json.dumps(mv_failed, sort_keys=True)))
    if mv_unver:
        out["problems"].append(
            "MIGRATED VALUES NOT JUDGED: %d definition(s) read Unknown/ValidationIncomplete -- %s."
            " NOTHING WAS REFUSED; the dry run could not complete for some instances. That is a gap"
            " in the check, not a finding about the values, and it must not be read either way."
            % (len(mv_unver), json.dumps(mv_unver, sort_keys=True)))

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
    # THE WATCHED SET IS THE KEY SET, and an absent key means NEVER WATCHED, not deleted.
    # capture() writes an entry for EVERY ref passed in --secret, including {"present": False}
    # for one that does not exist. So a ref missing from a side's `secrets` map was never asked
    # for on that side, and the two sides are then answering different questions.
    #
    # WITHOUT THIS CHECK BOTH DIRECTIONS INVENT A PROBLEM, which is how I found it: a pre
    # captured with --secret A and a post with --secret A --secret B reported
    # "SECRET KEY SET CHANGED: B None -> [...]", and the reverse reported
    # "SECRET DISAPPEARED: B" for a Secret that was alive and simply unwatched. Both read as a
    # roll having destroyed a credential.
    #
    # IT REFUSES RATHER THAN SKIPPING THE ASYMMETRIC REFS. Skipping would compare fewer Secrets
    # than the operator asked about and report success, which is the failure this whole tool
    # exists to avoid -- a narrowed comparison that says nothing about having narrowed. A diff
    # whose two sides watched different sets is not a weaker answer, it is a different question.
    if set(ps) != set(pos):
        only_pre, only_post = sorted(set(ps) - set(pos)), sorted(set(pos) - set(ps))
        out["problems"].append(
            "REFUSING THE SECRET DIFF: the two snapshots watched DIFFERENT --secret sets, so no"
            " comparison between them is meaningful. pre watched %d, post watched %d;"
            " only in pre: %s; only in post: %s. Re-capture both sides with the same --secret"
            " arguments. Nothing about these Secrets is being asserted either way."
            % (len(ps), len(pos), json.dumps(only_pre), json.dumps(only_post)))
    else:
        for k in sorted(ps):
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
                    "note": ("resourceVersion moved, so it WAS written. This does NOT establish "
                             "that any VALUE changed -- a controller re-applying identical bytes "
                             "produces the same trace. Only a functional test with the credential "
                             "can answer that, and this tool never reads one."),
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


def _excl_probe():
    """With an EMPTY exclusion list, prove the mechanism anyway by excluding one key locally.

    The list is empty on purpose, so there is no real entry to test. Rather than skip the case --
    a skipped case prints as nothing and reads as a pass -- this temporarily excludes `Ready`,
    asserts the diff goes quiet on a Ready change, and restores it. If the mechanism ever stops
    working, an author adding a genuine exclusion would otherwise find it silently ignored.
    """
    global DEFINITION_DIFF_EXCLUDED
    keep = DEFINITION_DIFF_EXCLUDED
    try:
        DEFINITION_DIFF_EXCLUDED = frozenset({"Ready"})
        # PROVENANCE ON BOTH ENDS, with a later time on the post. Without it diff() refuses the
        # pair and `changed` is absent -- which this probe would read as "the exclusion worked",
        # since an absent row and an excluded row look identical from here. That is the shape of
        # false pass this probe exists to prevent, so it must not be vulnerable to it itself.
        _pv = lambda t: {"captured_at": t, "tool_sha256": "exclprobe000000", "context": "probe"}
        pre = {"definitions": {"x/y": {"Ready": "a"}}, "compositions": {}, "releases": {},
               "secrets": {}, "provenance": _pv("2026-11-25T10:00:00Z")}
        post = {"definitions": {"x/y": {"Ready": "b"}}, "compositions": {}, "releases": {},
                "secrets": {}, "provenance": _pv("2026-11-25T11:00:00Z")}
        quiet = not (diff(pre, post).get("definitions", {}).get("changed", {}) or {})
        DEFINITION_DIFF_EXCLUDED = frozenset()
        loud = bool(diff(pre, post).get("definitions", {}).get("changed", {}) or {})
        return quiet and loud
    finally:
        DEFINITION_DIFF_EXCLUDED = keep


def _self_test():
    import datetime as _dt_mod

    # THE FIXTURES CARRY PROVENANCE BECAUSE REAL SNAPSHOTS DO. When _diff() began refusing a pair it
    # cannot attribute, all 95 cases here failed -- they modelled snapshots with no capture time and
    # no tool identity, which is exactly the artifact that caused installer#1165's baseline
    # confusion. The fix is the fixtures, not an exemption: a test corpus that cannot reach the new
    # refusal would leave it unexercised, and a gate nothing exercises is a gate nobody can trust.
    _TOOL = "testtool00000000"

    def _prov(when, tool=_TOOL, ctx="test-ctx"):
        return {"captured_at": when, "tool_sha256": tool, "context": ctx}

    def _stamp(snap, when, **kw):
        """Return snap with a provenance block, so a case can build a POST from a dict literal."""
        out = dict(snap)
        out["provenance"] = _prov(when, **kw)
        return out

    _real_diff = diff

    def _diff(pre, post, *a, **kw):
        """_diff() with the fixtures stamped as capture() would stamp them.

        The cases hold POST snapshots as dict literals, which real snapshots never are. This gives
        an unstamped one the provenance capture() would have written -- same tool, a LATER time --
        so the cases exercise the comparison logic they were written for. The refusal itself is
        exercised by the dedicated prov_unit cases below, which call the real _diff() directly, so
        nothing here hides it.
        """
        if isinstance(pre, dict) and "provenance" not in pre:
            pre = _stamp(pre, "2026-11-25T10:00:00Z")
        if isinstance(post, dict) and "provenance" not in post:
            post = _stamp(post, "2026-11-25T11:00:00Z")
        return _real_diff(pre, post, *a, **kw)

    PRE = {
        "provenance": _prov("2026-11-25T10:00:00Z"),
        "releases": {
            "krateo-system/publish-a": {"chart": "builder-publish-1.8.128", "revision": "72", "status": "deployed"},
            "krateo-system/publish-b": {"chart": "builder-publish-1.8.128", "revision": "80", "status": "deployed"},
            "krateo-system/installer": {"chart": "installer-0.3.670", "revision": "579", "status": "deployed"},
        },
        "compositions": {
            "portals/krateo-system/portal": {"apiVersion": "composition.krateo.io/v1-8-128",
                                             "managed": 64, "Ready": "True/Available",
                                             "childrenSynced": [
                                                 {"child": "keycloakclients/krateo-system/a",
                                                  "synced": "True/ReconcileSuccess"},
                                                 {"child": "flexes/krateo-system/w",
                                                  "synced": "<no-synced-condition>"}],
                                             "coverage": {"managedChildrenCount": 64,
                                                          "healthEvaluatedChildren": 64,
                                                          "unreadableChildrenCount": 0},
                                             "Synced": "True/ReconcileSuccess", "uid": "u-portal",
                                             "compositionVersion": "v1-8-128"},
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
        # A SECOND OBSERVATION HAS A LATER TIME. deepcopy(PRE) inherits PRE's provenance verbatim,
        # which diff() refuses as "one observation used twice" -- correctly, because that is what it
        # would be on a real cluster. Advancing the clock here is what makes the fixture a POST
        # rather than a relabelled PRE, and it is the one line that was missing from every snapshot
        # taken before 2026-11-25.
        p["provenance"] = _prov("2026-11-25T11:00:00Z")
        for k, v in kw.items():
            if k == "rev_a": p["releases"]["krateo-system/publish-a"]["revision"] = v
            if k == "rev_b": p["releases"]["krateo-system/publish-b"]["revision"] = v
            if k == "drop_a": del p["releases"]["krateo-system/publish-a"]
            # the three --secret asymmetry knobs. drop_secret = that ref was never watched in the
            # POST capture (key absent); extra_secret = watched only in the post; secret_gone =
            # watched on BOTH sides and genuinely deleted, which must still report DISAPPEARED.
            # observedGeneration knobs. cd_* act on the definition row, comp_* on the composition.
            if k == "cd_gen": p["definitions"]["krateo-system/portal"]["generation"] = v
            if k == "cd_og": p["definitions"]["krateo-system/portal"]["observedGeneration"] = v
            if k == "comp_gen": p["compositions"]["portals/krateo-system/portal"]["generation"] = v
            if k == "comp_og": p["compositions"]["portals/krateo-system/portal"]["observedGeneration"] = v
            if k == "drop_secret": del p["secrets"]["krateo-system/cj"]
            if k == "extra_secret": p["secrets"]["krateo-system/other"] = {
                "present": True, "dataKeys": ["k"], "resourceVersion": "9", "writes": []}
            if k == "secret_gone": p["secrets"]["krateo-system/cj"] = {"present": False}
            if k == "uid": p["compositions"]["portals/krateo-system/portal"]["uid"] = v
            if k == "av": p["compositions"]["portals/krateo-system/portal"]["apiVersion"] = v
            if k == "cv": p["compositions"]["portals/krateo-system/portal"]["compositionVersion"] = v
            if k == "managed": p["compositions"]["portals/krateo-system/portal"]["managed"] = v
            if k == "cov": p["compositions"]["portals/krateo-system/portal"]["coverage"] = v
            if k == "kids": p["compositions"]["portals/krateo-system/portal"]["childrenSynced"] = v
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
        # SPLIT 2026-11-25. This case used to pass PRE as its own POST and assert "no problems",
        # which conflated two different properties: that the comparison logic is quiet on equal
        # CONTENT, and that a snapshot diffed against ITSELF is acceptable. The second is false and
        # is exactly the degenerate case that let three copies of one capture pass for three
        # observations -- an empty diff from one observation used twice is indistinguishable from an
        # empty diff from a cluster that did not move. `post()` now advances the capture time, so
        # this case tests ONLY the first property: a real second observation of an unchanged cluster
        # reports nothing. The second property is tested as a REFUSAL in prov_unit below.
        ("a SECOND observation of an unchanged cluster -> no problems", post(), None, None, 0),
        # CHILD-SYNCED CASES, 2026-11-17. The gap: demo's KeycloakServiceAccountRoleMapping sat
        # at Synced=False/ReconcileError under a parent reading Ready=True/AvailablePartialCoverage,
        # because the rollup EXCLUDES a child it never health-evaluated. Validated end-to-end
        # against that live object before these were written.
        ("a managed child at Synced=False/ReconcileError IS a problem, whatever the parent reads",
         post(kids=[{"child": "keycloakserviceaccountrolemappings/krateo-system/r",
                     "synced": "False/ReconcileError"}]), None, None, 1),
        ("a child at Synced=True/ReconcileSuccess is NOT a problem",
         post(kids=[{"child": "keycloakclients/krateo-system/a",
                     "synced": "True/ReconcileSuccess"}]), None, None, 0),
        # THE LOAD-BEARING DISTINCTION. A kind with no `Synced` condition and a child that could
        # not be READ are both "not True", and treating them alike made the first run against demo
        # report 768 of 953 children as unknown -- 80% blind -- when every kind that actually
        # carries the condition had resolved. Without BOTH cases, one bucket could silently absorb
        # the other.
        ("a child of a kind with NO `Synced` condition is NOT a problem -- portal widgets are "
         "rendered objects and never carry one",
         post(kids=[{"child": "flexes/krateo-system/w", "synced": "<no-synced-condition>"}]),
         None, None, 0),
        ("a child the capture could NOT RESOLVE is a problem -- not because the child is broken "
         "but because the result is INCOMPLETE and must not read as a clean pass",
         post(kids=[{"child": "keycloakclients/krateo-system/a", "synced": None}]),
         None, None, 1),
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
        # THE THREE-WAY DISTINCTION, added 2026-11-21 with compositionVersion. apiVersion in a
        # response is whatever SERVED version the client negotiated; the arrival field is the
        # krateo.io/composition-version label. Storing only apiVersion cannot tell a real GVK
        # arrival from a read artifact, and on 0.3.697 I read apiVersion by hand and reported that
        # 17 compositions had not arrived when they had. None of these three is a "problem" -- the
        # point is that the diff now REPORTS them distinguishably instead of collapsing them.
        ("apiVersion moved AND the label moved: a real GVK arrival",
         post(av="composition.krateo.io/v1-8-154", cv="v1-8-154"), None, None, 0),
        ("apiVersion moved but the LABEL did NOT: a read artifact, not an arrival",
         post(av="composition.krateo.io/v1-8-154"), None, None, 0),
        ("FALSIFIER: the label moving with apiVersion UNCHANGED is also reported, so the field is "
         "compared and not merely captured",
         post(cv="v1-8-154"), None, None, 0),
        ("FALSIFIER: managed count changing is REPORTED but is not by itself a problem",
         post(managed=65), None, None, 0),
        ("a Secret's resourceVersion moving is reported as WRITTEN, not as a problem",
         post(secret_rv="200"), None, None, 0),
        ("a Secret's KEY SET changing IS a problem", post(secret_keys=["password", "token"]), None, None, 1),
        # --- observedGeneration lag. The FOUR cases that matter are the three misfire guards and
        # the real one; without the guards this check would fire on every roll.
        ("observedGeneration BEHIND generation on a definition IS a problem",
         post(cd_gen=7, cd_og=6), None, None, 1, "observedGeneration BEHIND generation"),
        ("GUARD 1: observedGeneration ABSENT is NOT reported -- 057 runs core-provider 2.13.129 "
         "where the field does not exist on a CompositionDefinition, and absent means UNKNOWN",
         post(cd_gen=7), None, None, 0),
        # want=1 because changing Ready also trips the CD-not-Available check; the SIXTH field
        # asserts the observedGeneration problem specifically did NOT fire.
        ("GUARD 2: a lag whose Ready reason is ReconcilePaused does not raise an "
         "observedGeneration problem -- a paused object legitimately stays behind",
         post(cd_gen=7, cd_og=6, cd_ready="True/ReconcilePaused"), None, None, 1,
         "!observedGeneration BEHIND generation"),
        ("GUARD 3: observedGeneration AHEAD of or EQUAL to generation is NOT reported",
         post(cd_gen=7, cd_og=7), None, None, 0),
        ("FALSIFIER: a lag on a COMPOSITION is reported too, so the check is not definition-only",
         post(comp_gen=4, comp_og=3), None, None, 1, "observedGeneration BEHIND generation"),
        # --- asymmetric --secret sets. Both directions used to INVENT a problem: an unwatched
        # Secret read as deleted one way and as a key-set change the other. The watched set is
        # the key set, because capture() writes an entry for every --secret including an absent
        # one as {"present": False}.
        # BOTH of these name the expected TEXT, not just a count, and the first one is why.
        # Written with `want=1` alone it PASSED with the fix disabled: the old code reported
        # "SECRET DISAPPEARED" for the unwatched ref, which is also exactly one problem. A case
        # that cannot fail is not a test, and only running the falsifier revealed it.
        ("a Secret watched only in PRE is REFUSED, not reported as DISAPPEARED",
         post(drop_secret=True), None, None, 1, "REFUSING THE SECRET DIFF"),
        ("a Secret watched only in POST is REFUSED, not reported as a KEY SET change",
         post(extra_secret=True), None, None, 1, "REFUSING THE SECRET DIFF"),
        ("FALSIFIER: the refusal names the ASYMMETRY and NOT a disappearance, so the two causes "
         "stay distinguishable in the output an operator reads",
         post(drop_secret=True), None, None, 1, "REFUSING THE SECRET DIFF"),
        ("FALSIFIER: a genuinely DELETED Secret -- watched on BOTH sides, present on one -- is "
         "still reported as DISAPPEARED, so the refusal did not swallow the real finding",
         post(secret_gone=True), None, None, 1, "SECRET DISAPPEARED"),
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
        # THE FIXTURE CARRIED FULL COVERAGE while describing a never-evaluated composition, so
        # once the scalars took precedence it failed. The fixture was incoherent, not the code:
        # a composition the rollup never assessed reads evaluated == 0, not 64 of 64.
        ("a composition converging 2h but born 2h ago is NEVER EVALUATED, not a problem -- the "
         "internal-ca-bundle case, installer#1017",
         post(comp_ready="Unknown/ZeroEvidence", comp_readyat=_ago(7200),
              comp_createdat=_ago(7206),
              cov={"managedChildrenCount": 1, "healthEvaluatedChildren": 0, "unreadableChildrenCount": 0}), None, None, 0),
        # THE TWO CASES THAT PROVE THE PRECEDENCE, one per direction. Without them the fallback
        # order is untested and either signal could silently become the only one consulted.
        ("PRECEDENCE: timestamps say STALLED (born 2h before Ready moved) but the scalars say the "
         "rollup assessed NOTHING -> NEVER EVALUATED, not a problem. This is selfservice-status "
         "on demo: 111 s outside a 60 s window, reported as a 226-hour stall that never happened",
         post(comp_ready="Unknown/ZeroEvidence", comp_readyat=_ago(7200),
              comp_createdat=_ago(14400),
              cov={"managedChildrenCount": 1, "healthEvaluatedChildren": 0, "unreadableChildrenCount": 0}), None, None, 0),
        ("PRECEDENCE, the other way: timestamps say NEVER EVALUATED (born with Ready) but the "
         "scalars say children WERE evaluated -> a problem. Evidence beats the heuristic in both "
         "directions, and per core-provider evaluated>0 can be all-ABSENT children, since absent "
         "is a subset of converging -- which is a real stall, not an unevaluable composition",
         post(comp_ready="False/Creating", comp_readyat=_ago(7200),
              comp_createdat=_ago(7206),
              cov={"managedChildrenCount": 64, "healthEvaluatedChildren": 64, "unreadableChildrenCount": 0}), None, None, 1),
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
    # never_evaluated_by_coverage() NEEDS ITS OWN ASSERTIONS. Mutation testing showed the case
    # list cannot reach its `unread == 0` guard: dropping that guard left all 43 cases passing,
    # because a composition with unreadable children raises the CHILDREN UNREADABLE problem
    # independently, and the runner judges only problems>0 vs ==0. So the misclassification was
    # invisible at that level. These assert the predicate directly.
    ne_unit = [
        ("managed>0, evaluated 0, unreadable 0 -> True (the rollup assessed nothing)",
         {"managedChildrenCount": 1, "healthEvaluatedChildren": 0, "unreadableChildrenCount": 0},
         True),
        ("evaluated 0 but unreadable 5 -> FALSE. core-provider: that is ChildrenForbidden, an "
         "actionable RBAC defect, NOT an unevaluable composition. THE GUARD.",
         {"managedChildrenCount": 5, "healthEvaluatedChildren": 0, "unreadableChildrenCount": 5},
         False),
        ("evaluated 64 of 64 -> False",
         {"managedChildrenCount": 64, "healthEvaluatedChildren": 64, "unreadableChildrenCount": 0},
         False),
        ("evaluated>0 may be ALL-ABSENT children (absent is a subset of converging) -> still "
         "False: genuinely converging, not unevaluable",
         {"managedChildrenCount": 3, "healthEvaluatedChildren": 3, "unreadableChildrenCount": 0},
         False),
        ("managed 0 -> None, not True. InventoryNotRecorded carries no coverage evidence either "
         "way, and calling it never-evaluated would state a fact from the absence of one.",
         {"managedChildrenCount": 0, "healthEvaluatedChildren": 0, "unreadableChildrenCount": 0},
         None),
        ("a missing scalar -> None (absent is not zero, same rule as _coverage)",
         {"managedChildrenCount": 5, "healthEvaluatedChildren": 0},
         None),
    ]
    bad_ne = 0
    # THE CARRY-FORWARD NEEDS DIRECT ASSERTIONS, for the reason the ne_unit block above records:
    # the case runner judges only problems>0 vs ==0, so a composition can be misclassified
    # invisibly. Each case below is a separate claim about stuck_converging(prior=...), and each was
    # mutation-tested by reverting the carry-forward -- case 1 FAILS without it, which is the point.
    _T0 = "2026-11-08T10:00:00Z"   # nine days before the frozen clock: a real stall
    _T1 = "2026-11-17T23:14:51Z"   # the dip: readyAt reset to the roll moment
    _NOW = _dt_mod.datetime(2026, 11, 17, 23, 24, 51, tzinfo=_dt_mod.timezone.utc)

    def _snap(ready, at):
        return {"compositions": {"krateo-system/x": {"Ready": ready, "readyAt": at,
                                                     "coverage": {"managedChildrenCount": 4,
                                                                  "healthEvaluatedChildren": 4,
                                                                  "unreadableChildrenCount": 0}}}}
    cf_unit = [
        ("Ready value UNCHANGED and readyAt reset by a dip -> the NINE-DAY age survives",
         _snap("False/Creating", _T1), _snap("False/Creating", _T0)["compositions"], True),
        ("Ready value CHANGED -> prior is ignored, the new age stands (600 s, under the 900 s budget)",
         _snap("False/Creating", _T1), _snap("False/Unavailable", _T0)["compositions"], False),
        ("prior readyAt is NEWER -> the EARLIER post value is kept; never shortens an age",
         _snap("False/Creating", _T0), _snap("False/Creating", _T1)["compositions"], True),
        ("key absent from prior -> post value used, so a NEW composition is not aged falsely",
         _snap("False/Creating", _T1), {"krateo-system/other": {"Ready": "False/Creating",
                                                                "readyAt": _T0}}, False),
        ("prior=None -> the single-snapshot behaviour is unchanged (regression guard)",
         _snap("False/Creating", _T1), None, False),
    ]
    # cv_unit, 2026-11-22. THE `cases` LIST ABOVE CANNOT TEST FIELD COMPARISON AT ALL. Its runner
    # asserts only `(len(problems) > 0) == (want > 0)`, so every case expecting 0 problems passes
    # whether or not a field is compared -- including the three added with compositionVersion and
    # the one of them labelled FALSIFIER, which claimed the field was "compared and not merely
    # captured" while asserting nothing of the kind. Measured: deleting "compositionVersion" from
    # the compared tuple left all 61 cases GREEN. These assert on the `changed` ROW, which is the
    # observable the field actually moves, and the last one is the real falsifier.
    _CVK = "portals/krateo-system/portal"
    _CVA = "composition.krateo.io/v1-8-128"
    _CVB = "composition.krateo.io/v1-8-154"
    cv_unit = [
        ("the LABEL moving alone puts compositionVersion in the changed row",
         post(cv="v1-8-154"), {"compositionVersion": ["v1-8-128", "v1-8-154"]}),
        ("apiVersion moving alone puts ONLY apiVersion there -- a read artifact stays "
         "distinguishable from an arrival",
         post(av=_CVB), {"apiVersion": [_CVA, _CVB]}),
        ("both moving puts BOTH there, which is a real GVK arrival",
         post(av=_CVB, cv="v1-8-154"),
         {"apiVersion": [_CVA, _CVB], "compositionVersion": ["v1-8-128", "v1-8-154"]}),
        ("FALSIFIER: with nothing moved there is NO row, so the three above are not rows the "
         "diff emits unconditionally",
         post(), None),
    ]
    # cdr_unit, 2026-11-22. THE CONSTANT ALONE IS NOT GUARDED BY ANYTHING ABOVE. Adding
    # "CreateRefusedPersistently" to CD_FAILED_REASONS changes how a CD's condition is CLASSIFIED,
    # and no case in `cases`, cov_unit, ne_unit, cf_unit or cv_unit reads that classification --
    # so deleting the reason again would leave all 65 of them green while a real failure silently
    # became "unclassified". Same shape as the compositionVersion mistake cv_unit exists for.
    #
    # These assert the OBSERVABLE: which bucket the reason lands in, via the problem text. A reason
    # in CD_FAILED_REASONS produces "COMPOSITIONDEFINITIONS FAILED"; an unknown one produces the
    # unclassified bucket instead. The last case is the falsifier -- an invented reason MUST NOT be
    # classified as failed, or the check would pass for any string at all.
    def _cd_snap(reason):
        return {"compositions": {}, "definitions": {"krateo-system/x": {
            "Ready": "False/%s" % reason, "readyAt": "2026-11-22T10:00:00Z"}},
            "releases": {}, "secrets": {}}

    def _cd_problem_kind(reason):
        probs = " ".join(_diff(_cd_snap(reason), _cd_snap(reason)).get("problems", []))
        if "COMPOSITIONDEFINITIONS FAILED" in probs:
            return "failed"
        if "COMPOSITIONDEFINITIONS" in probs and "classif" in probs.lower():
            return "unclassified"
        return "other:%s" % (probs[:40] or "<none>")

    cdr_unit = [
        ("CreateRefusedPersistently is classified FAILED (oasgen >= 0.85.0; 26 at 0.88.0 vs 0 at 0.84.0)",
         "CreateRefusedPersistently", "failed"),
        ("CONTROL: a pre-existing reason is still classified failed",
         "CreateWithheldByPolicy", "failed"),
        ("FALSIFIER: an INVENTED reason must NOT be classified failed",
         "NotARealReasonAtAll", "unclassified"),
    ]
    bad_cdr = 0
    for label, reason, want in cdr_unit:
        got = _cd_problem_kind(reason)
        ok = got == want
        bad_cdr += 0 if ok else 1
        print("  %-4s cd-reason: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%r got=%r" % (want, got))

    # prov_unit, 2026-11-25. THE REFUSAL HAS TO BE TESTED AGAINST THE REAL diff(), NOT _diff():
    # _diff stamps an unstamped fixture, which is right for the comparison cases and would hide
    # every case here. These call diff() directly.
    #
    # WHY THIS EXISTS AT ALL. Three snapshot files in one scratchpad were byte-identical, and the
    # cluster they described had not changed in the window they spanned -- 0 helm releases updated,
    # 0 objects created. So a working capture and a `cp` produce the same bytes, and neither the
    # filename nor the mtime distinguishes them, because `cp` resets both. Every refusal below
    # describes a diff whose output would look plausible and mean something else.
    def _pvx(t, tool="testtool00000000", ctx="test-ctx"):
        return {"captured_at": t, "tool_sha256": tool, "context": ctx}

    _ok_pre = {"releases": {}, "compositions": {}, "definitions": {}, "secrets": {},
               "provenance": _pvx("2026-11-25T10:00:00Z")}

    def _with(prov):
        d = dict(_ok_pre)
        if prov is None:
            d.pop("provenance", None)
        else:
            d["provenance"] = prov
        return d

    prov_unit = [
        ("a well-formed pair is NOT refused -- so the refusals below are not unconditional",
         _ok_pre, _with(_pvx("2026-11-25T11:00:00Z")), False),
        ("a snapshot diffed against ITSELF is refused: one observation used twice",
         _ok_pre, _ok_pre, True),
        ("the SAME capture time on both ends is refused even for different content",
         _ok_pre, _with(_pvx("2026-11-25T10:00:00Z")), True),
        ("a TRANSPOSED pair is refused -- reversed, every bump reads as a rollback",
         _with(_pvx("2026-11-25T12:00:00Z")), _with(_pvx("2026-11-25T11:00:00Z")), True),
        ("DIFFERENT TOOL VERSIONS are refused: a field one version added reads as a cluster change",
         _ok_pre, _with(_pvx("2026-11-25T11:00:00Z", tool="otherversion0000")), True),
        ("DIFFERENT CLUSTERS are refused",
         _ok_pre, _with(_pvx("2026-11-25T11:00:00Z", ctx="other-ctx")), True),
        ("a PRE with no provenance at all is refused -- an old snapshot cannot be dated",
         _with(None), _with(_pvx("2026-11-25T11:00:00Z")), True),
        ("a POST with no provenance at all is refused",
         _ok_pre, _with(None), True),
    ]
    bad_prov = 0
    for label, _a, _b, want_refused in prov_unit:
        got = diff(_a, _b)
        refused = bool(got.get("refused"))
        # A refusal must also SAY why, and must not report rows alongside it: rows printed next to
        # "unauditable" get read anyway.
        said = any(str(x).startswith("UNAUDITABLE PAIR:") for x in got.get("problems", []))
        ok = (refused == want_refused) and (said == want_refused)
        bad_prov += 0 if ok else 1
        print("  %-4s provenance: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want_refused=%s got refused=%s explained=%s"
                  % (want_refused, refused, said))

    bad_cv = 0
    for label, _po, want_row in cv_unit:
        got_row = _diff(PRE, _po)["compositions"]["changed"].get(_CVK)
        ok = got_row == want_row
        bad_cv += 0 if ok else 1
        print("  %-4s composition-version: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%s got=%s" % (want_row, got_row))

    bad_cf = 0
    for label, snap, prior, want_stalled in cf_unit:
        st, _nv = stuck_converging(snap, now=_NOW, prior=prior)
        got = bool(st)
        ok = got is want_stalled
        bad_cf += 0 if ok else 1
        print("  %-4s carry-forward: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want stalled=%r got=%r rows=%r" % (want_stalled, got, st))

    for label, cov, want in ne_unit:
        got = never_evaluated_by_coverage({"coverage": cov})
        ok = got is want
        bad_ne += 0 if ok else 1
        print("  %-4s never_evaluated: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%r got=%r" % (want, got))
    bad_unit = 0
    for label, st, want in cov_unit:
        got = _coverage(st)
        ok = got == want
        bad_unit += 0 if ok else 1
        print("  %-4s _coverage: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%s got=%s" % (want, got))
    bad = 0
    # A case may carry a SIXTH field: a substring the problem text MUST contain. Counting problems
    # alone cannot tell two different causes apart -- an asymmetric --secret set and a genuinely
    # deleted Secret both produce exactly one problem, so without this the pair of falsifiers below
    # them would pass whichever message fired. The field is optional so the other cases are
    # untouched.
    for case in cases:
        label, po, delta, match, want = case[:5]
        want_text = case[5] if len(case) > 5 else None
        d = _diff(PRE, po, expect_delta=delta, matching=match)
        got = len(d["problems"])
        ok = (got > 0) == (want > 0)
        if ok and want_text is not None:
            # A leading "!" asserts the text is ABSENT. Added 2026-11-25: the observedGeneration
            # guards need "no observedGeneration problem fired", which is NOT the same as "no
            # problems at all" -- setting a Ready reason to prove a guard also trips the
            # unrelated CD-not-Available check, so a total count of 0 is the wrong assertion and
            # would have made me weaken the guard instead of the harness.
            if want_text.startswith("!"):
                ok = not any(want_text[1:] in str(x) for x in d["problems"])
            else:
                ok = any(want_text in str(x) for x in d["problems"])
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want_problems=%s want_text=%r got=%s" % (want, want_text, d["problems"]))
    # mv_unit, 2026-11-22. The constants and the classification are otherwise UNGUARDED: nothing
    # above reads MigratedValuesValid, so deleting the whole block would leave all 68 cases green --
    # the same mistake cv_unit and cdr_unit exist for. These assert the OBSERVABLE: which problem
    # text a definition's MigratedValuesValid produces.
    #
    # THE THIRD CASE IS THE ONE THAT EARNS THE UNIT. Absent must produce NOTHING, because absent
    # means "no migration since the release" and a reader crediting it as a pass would report every
    # un-migrated definition as having passed a check that never ran.
    def _mv_snap(raw):
        d = {"compositions": {}, "releases": {}, "secrets": {},
             "definitions": {"krateo-system/x": {"Ready": "True/Available",
                                                 "readyAt": "2026-11-22T10:00:00Z"}}}
        if raw is not None:
            d["definitions"]["krateo-system/x"]["MigratedValuesValid"] = raw
        return d

    def _mv_kind(raw):
        probs = " ".join(_diff(_mv_snap(raw), _mv_snap(raw)).get("problems", []))
        if "MIGRATED VALUES REFUSED" in probs:
            return "refused"
        if "MIGRATED VALUES NOT JUDGED" in probs:
            return "not-judged"
        return "silent"

    mv_unit = [
        ("False/ValuesRefusedByNewVersion -> REFUSED", "False/ValuesRefusedByNewVersion", "refused"),
        ("Unknown/ValidationIncomplete -> NOT JUDGED", "Unknown/ValidationIncomplete", "not-judged"),
        ("ABSENT -> silent, and NOT a pass (the case that earns this unit)", None, "silent"),
        ("True/AllValid -> silent", "True/AllValid", "silent"),
        ("FALSIFIER: an invented reason is silent, not refused", "False/ZZMadeUp", "silent"),
    ]
    bad_mv = 0
    for label, raw, want in mv_unit:
        got = _mv_kind(raw)
        ok = got == want
        bad_mv += 0 if ok else 1
        print("  %-4s migrated-values: %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%r got=%r" % (want, got))

    # ------------------------------------------------------------------ dk_unit
    # THE UNIT THAT WOULD HAVE CAUGHT 2026-11-23's BLIND DIFF.
    #
    # #1136 added `MigratedValuesValid` to the capture and to the classifier. The self-test went
    # 61 -> 73 cases and all 73 passed -- every one of the twelve new cases exercised the
    # CLASSIFIER, and none exercised `diff`. The diff's field list was a hardcoded tuple of six
    # names, so it never compared the new key: a definition going from absent to
    # "False/MigrationFailed" produced an EMPTY definitions.changed.
    #
    # Found by planting that exact transition on cert-manager/cert-manager in a real capture and
    # getting nothing back. Not by CI, and not by reading the diff code -- by falsifying a result
    # that had looked like a pass.
    #
    # So this unit asserts the PROPERTY, not the six names: a key nobody has thought of yet is
    # still compared. That is what makes it survive the next added field.
    def _defs_diff(a_row, b_row):
        pre = {"definitions": {"x/y": a_row}, "compositions": {}, "releases": {}, "secrets": {}}
        post = {"definitions": {"x/y": b_row}, "compositions": {}, "releases": {}, "secrets": {}}
        return (_diff(pre, post).get("definitions", {}).get("changed", {}) or {}).get("x/y", {})

    _BASE = {"Ready": "True/Available", "Synced": "True/", "chartVersion": "1.0.0",
             "compositionId": "abc123", "parent": "p", "readyAt": "t", "uid": "u",
             "MigratedValuesValid": None}

    def _moved(field, new):
        a = dict(_BASE)
        b = dict(_BASE)
        b[field] = new
        return field in _defs_diff(a, b)

    dk_unit = [
        ("MigratedValuesValid absent -> False/MigrationFailed is REPORTED (the 11-23 blind spot)",
         lambda: _moved("MigratedValuesValid", "False/MigrationFailed")),
        ("MigratedValuesValid True/AllValid -> False/... is REPORTED",
         lambda: (lambda a, b: "MigratedValuesValid" in _defs_diff(a, b))(
             dict(_BASE, MigratedValuesValid="True/AllValid"),
             dict(_BASE, MigratedValuesValid="False/ValuesRefusedByNewVersion"))),
        ("compositionId change is REPORTED -- silent re-parenting under the same key",
         lambda: _moved("compositionId", "zzz999")),
        ("A KEY THAT DOES NOT EXIST YET is compared: the property, not a name list",
         lambda: _moved("someFieldAddedNextMonth", "v")),
        ("an EXCLUDED key is NOT reported, so the exclusion list does what an author expects",
         lambda: not (lambda: (DEFINITION_DIFF_EXCLUDED
                               and "Ready" in _defs_diff(dict(_BASE), dict(_BASE, Ready="x"))))()
                 if DEFINITION_DIFF_EXCLUDED else _excl_probe()),
        ("FALSIFIER: an IDENTICAL pair reports nothing, so the above are not vacuous",
         lambda: _defs_diff(dict(_BASE), dict(_BASE)) == {}),
    ]
    # co_unit, 2026-11-24. THE COMPOSITIONS UNCLASSIFIED BUCKET. Until today the compositions
    # classifier was a bare membership filter over FAILED_REASONS with no else, so a reason nobody
    # had taught it was silently dropped -- not failed, not reported. core-provider announced
    # `InstallFailing` before merging it, which is how the gap was found rather than discovered on
    # a cluster. These assert the OBSERVABLE (which bucket a reason lands in, via the problem text)
    # and the last two are the falsifiers: a KNOWN failed reason must not land in the unclassified
    # bucket, and a healthy composition must produce neither.
    def _co(ready):
        pre = {"compositions": {"k": {"Ready": "True/Available"}}, "definitions": {}, "releases": {}, "secrets": {}}
        post = {"compositions": {"k": {"Ready": ready}}, "definitions": {}, "releases": {}, "secrets": {}}
        probs = " ".join(_diff(pre, post).get("problems") or [])
        return ("UNCLASSIFIED" in probs, "COMPOSITIONS FAILED" in probs)
    co_unit = [
        # WAS `False/InstallFailing` UNTIL 2026-11-24, when that reason got CLASSIFIED (it is
        # core-provider#636 and now sits in FAILED_REASONS). The case then failed, correctly: its
        # subject had stopped being unknown. A real-but-unclassified reason makes a realistic
        # fixture and a self-invalidating test, so the unclassified case now uses a name nothing
        # will ever ship, and the reason that moved gets its own assertion below.
        ("an UNKNOWN reason lands in the unclassified bucket",
         lambda: _co("False/ReasonThatWillNeverBeShipped") == (True, False)),
        ("InstallFailing is now FAILED, not unclassified (core-provider#636, classified 11-24)",
         lambda: _co("False/InstallFailing") == (False, True)),
        ("a second unknown reason behaves the same (not special-cased)",
         lambda: _co("False/SomeReasonNobodyShippedYet") == (True, False)),
        ("FALSIFIER: a KNOWN failed reason is FAILED, not unclassified",
         lambda: _co("False/Unavailable") == (False, True)),
        ("FALSIFIER: a KNOWN converging reason is NEITHER",
         lambda: _co("False/Creating") == (False, False)),
        ("HelmOperationInProgress is converging, not unclassified",
         lambda: _co("False/HelmOperationInProgress") == (False, False)),
        ("FALSIFIER: True/Available produces NEITHER, so the bucket is not vacuous",
         lambda: _co("True/Available") == (False, False)),
    ]
    bad_co = 0
    for label, fn in co_unit:
        try:
            ok = bool(fn())
        except Exception as e:
            ok, label = False, "%s  [raised %s]" % (label, e)
        bad_co += 0 if ok else 1
        print("  %-4s comp-unclassified: %s" % ("ok" if ok else "FAIL", label))

    bad_dk = 0
    for label, fn in dk_unit:
        try:
            ok = bool(fn())
        except Exception as e:
            ok, label = False, "%s  [raised %s]" % (label, e)
        bad_dk += 0 if ok else 1
        print("  %-4s diff-keys: %s" % ("ok" if ok else "FAIL", label))

    print("self-test: %d case(s), %d failure(s)"
          % (len(cases) + len(cov_unit) + len(ne_unit) + len(cf_unit) + len(cv_unit) + len(cdr_unit) + len(mv_unit) + len(dk_unit) + len(co_unit) + len(prov_unit),
             bad + bad_unit + bad_ne + bad_cf + bad_cv + bad_cdr + bad_mv + bad_dk + bad_co
             + bad_prov))
    # THE PRINTED COUNT AND THE EXIT CODE ARE BUILT FROM THE SAME TUPLE, 2026-11-25, because they
    # had drifted twice. `bad_cf` and `bad_cv` were once both missing from the return: a failing
    # carry-forward or composition-version unit printed FAIL and still exited 0, so CI passed it.
    # `bad_co` was then added to the printed sum and NOT to the return, so the same hole was open
    # again -- found while adding prov_unit, which would have been the third. Enumerating the
    # accumulators in one place is what stops a fourth: a tally that disagrees with its own exit
    # code is a gate that reports success while failing, which is worse than no gate.
    _ACCUMULATORS = (bad, bad_unit, bad_ne, bad_cf, bad_cv, bad_cdr, bad_mv, bad_dk, bad_co,
                     bad_prov)
    return 1 if any(_ACCUMULATORS) else 0


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
        unsynced, ch_checked, ch_nocond, ch_unknown = unsynced_children(snap)
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
        # THE DENOMINATOR IS PRINTED EVEN WHEN THERE ARE NO FINDINGS, because "no child is
        # out of sync" and "no child was checked" are different claims and this is the only
        # place that can tell them apart.
        print("\nCHILD SYNCED: %d krateo-group child(ren) checked, %d of a kind with no "
              "`Synced` condition, %d unresolved."
              % (ch_checked, ch_nocond, ch_unknown))
        if ch_unknown:
            print("  %d child(ren) could NOT be resolved. That is a gap in this capture, not a "
                  "statement about the cluster -- do not read the findings below as complete."
                  % ch_unknown)
        if unsynced:
            print("\nFINDINGS: %d managed child(ren) whose OWN `Synced` is not True. THE PARENT "
                  "DOES NOT CARRY THIS: a child the rollup never health-evaluated is EXCLUDED "
                  "from the verdict, so its composition can read Ready=True while the child "
                  "cannot reconcile at all." % len(unsynced))
            for key, child, st in unsynced:
                print("  %-52s %s" % (child, st))
                print("      parent %s reads %s" % (key, (snap["compositions"].get(key) or {}).get("Ready")))
        return 3 if (stalled or unreadable or broken or unsynced) else 0
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
