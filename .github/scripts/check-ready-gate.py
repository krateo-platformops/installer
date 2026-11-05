#!/usr/bin/env python3
"""CI guard: exercise inst.readyFromConditions — the ONE place this chart decides whether a
dependency counts as ready.

WHY IT NEEDS A TEST AT ALL. `inst.ready` has to `lookup` the live object, and `helm template`
DISABLES lookup, so for as long as the decision lived inside `inst.ready` it could not be
exercised without a cluster. It was a single line reading `status == "True"` and nothing ever
checked it — which is how it went unexamined until core-provider#165 asked what it does with
`Unknown`. The decision is now split into inst.readyFromConditions, which takes a conditions
list and nothing else, so it is testable; this is that test.

WHAT IT MUST DISCRIMINATE, and the middle two are the point:

    Ready=True                            -> ready
    Ready=Unknown reason=ZeroEvidence     -> ready      the cdc evaluated NONE of the children
    Ready=Unknown reason=ChildrenForbidden -> ready     core-provider#318: a 403 reading a child.
                                                        A STEADY state -- it persists until an
                                                        operator changes a Role -- so blocking a
                                                        dependant on it would block forever.
    Ready=Unknown reason=NotObserved      -> NOT ready   the TRANSIENT kind: an Observe failed and
                                                        the next may succeed. Refused on purpose.
    Ready=Unknown reason=Creating         -> NOT ready   a genuinely converging dep MUST block
    Ready=False  reason=Creating          -> NOT ready   core-provider#230's new emitter: a
                                                         composition whose render shrank
    Ready=False  reason=Unavailable       -> NOT ready
    Ready=False                           -> NOT ready

Accepting bare `Unknown` would turn every mid-creation dependency into "ready" and let
dependants render against children that do not exist yet. Rejecting ZeroEvidence would gate
the 8 components that dep on model-configs, permanently, since zero-evidence is a steady
state. Both failure directions are live, which is why both arms are asserted.

It renders the REAL _helpers.tpl in a throwaway chart rather than reimplementing the logic:
a test that restates the rule in Python would agree with itself.
"""
import json
import shutil
import subprocess
import pathlib
import sys
import tempfile
from pathlib import Path

HELPERS = Path("chart/templates/_helpers.tpl")

# Synced=True, attached to every case that is SUPPOSED to be accepted. Those cases exist to test
# REASON discrimination on Ready; without a Synced condition they would now all block and the gate
# would be testing the Synced rule over and over instead of the thing it is named for.
#
# WHY SYNCED AND NOT A READY REASON: unstructured-runtime declares two constants,
# ReasonReconcilePausedReady and ReasonReconcilePaused, whose string VALUE is the same
# "ReconcilePaused" -- written on Ready and on Synced respectively. The reason is ambiguous by
# construction, so telling "Ready, paused" from "Synced, paused" means reading the condition TYPE
# anyway. Synced is the typed condition that says it once.
READY_PROBE = """{{- $allCRDs := list (dict "spec" (dict "group" "composition.krateo.io" \
"names" (dict "kind" "Portal") "versions" (list (dict "name" "v1-8-88" "served" true) \
(dict "name" "v1-8-91" "served" true)))) }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: ready
data:
  servedTags: {{ (include "inst.servedTags" (list "Portal" $allCRDs)) | quote }}
  ready: {{ (include "inst.ready" (list $ "Portal" "portal" "1.8.91" $allCRDs)) | quote }}
"""


READY_SUITE = """suite: inst.ready finds a composition at an OLDER served tag
templates:
  - ready.yaml
tests:
  - it: with NO composition anywhere, inst.ready is empty
    asserts:
      - equal: {path: data.servedTags, value: "v1-8-88 v1-8-91"}
      - equal: {path: data.ready, value: ""}

  - it: THE FIX -- the live object sits at the OLDER served tag and must be found and read
    kubernetesProvider:
      scheme:
        "composition.krateo.io/v1-8-88/Portal":
          gvr: {group: composition.krateo.io, version: v1-8-88, resource: portals}
          namespaced: true
      objects:
        - apiVersion: composition.krateo.io/v1-8-88
          kind: Portal
          metadata: {name: portal, namespace: krateo-system}
          status:
            conditions:
              - {type: Ready, status: "True", reason: Available}
              - {type: Synced, status: "True", reason: ReconcileSuccess}
    asserts:
      - equal: {path: data.ready, value: "true"}

  - it: and a PAUSED object at that older tag must NOT satisfy readiness
    kubernetesProvider:
      scheme:
        "composition.krateo.io/v1-8-88/Portal":
          gvr: {group: composition.krateo.io, version: v1-8-88, resource: portals}
          namespaced: true
      objects:
        - apiVersion: composition.krateo.io/v1-8-88
          kind: Portal
          metadata: {name: portal, namespace: krateo-system}
          status:
            conditions:
              - {type: Ready, status: "True", reason: ReconcilePaused}
              - {type: Synced, status: "False", reason: ReconcilePaused}
    asserts:
      - equal: {path: data.ready, value: ""}
"""

SYNCED_OK = {"type": "Synced", "status": "True", "reason": "ReconcileSuccess"}

CASES = [
    ("Ready=True + Synced=True",          [{"type": "Ready", "status": "True"}, SYNCED_OK],                       "true"),
    # THIS CASE REVERSED, and deliberately: it used to assert that a Synced=False condition is
    # NOISE which must not affect the Ready reading, and expected "true". That is exactly the
    # PAUSED composition -- Ready=True/ReconcilePaused with Synced=False -- which was healthy when
    # it was paused and which nothing is maintaining now. A dependant emitted against it is
    # emitted against a component that is not converging and will not. Synced=True is now
    # REQUIRED as well as an accepted Ready reason.
    # Measured before changing it: across 166 live composition instances on three clusters,
    # exactly ONE object is newly withheld -- acme-feedback-controller on 057, the paused one this
    # exists to catch. The 9 Unknown/ZeroEvidence instances were unaffected (their Synced is
    # already True) and the one False/Creating instance was already withheld.
    ("PAUSED: Ready=True + Synced=False must BLOCK",
                                          [{"type": "Synced", "status": "False", "reason": "ReconcilePaused"},
                                           {"type": "Ready", "status": "True", "reason": "ReconcilePaused"}], ""),
    ("Ready=True + Synced ABSENT must BLOCK",
                                          [{"type": "Ready", "status": "True"}],                              ""),
    ("Ready=True + Synced=Unknown must BLOCK",
                                          [{"type": "Ready", "status": "True"}, {"type": "Synced", "status": "Unknown"}], ""),
    ("Unknown/ZeroEvidence + Synced=True",[{"type": "Ready", "status": "Unknown", "reason": "ZeroEvidence"}, SYNCED_OK], "true"),
    ("Unknown/Creating must BLOCK",       [{"type": "Ready", "status": "Unknown", "reason": "Creating"}],          ""),
    ("Unknown with NO reason must BLOCK", [{"type": "Ready", "status": "Unknown"}],                                ""),
    ("Ready=False",                       [{"type": "Ready", "status": "False"}],                                 ""),
    # core-provider#230 ships a NEW emitter of Ready=False: a composition whose render produced
    # FEWER children than it was managing now reports condition.Creating(), which is
    # Type=Ready / Status=False / Reason=Creating (unstructured-runtime v1.6.0), rather than
    # Available. The bare Ready=False case above already covers the BRANCH, so these two add no
    # new code path — they pin the CONTRACT, named for the states that will actually arrive.
    # Why that is worth two lines: #230's whole purpose is that a dep mid-shrink must not be
    # treated as converged, and the tempting "fix" when rolls start converging more slowly is to
    # accept False/Creating here as "it is only converging". That would restore exactly the false
    # convergence #230 removes, and without these cases nothing would fail.
    ("False/Creating must BLOCK",         [{"type": "Ready", "status": "False", "reason": "Creating"}],           ""),
    ("False/Unavailable must BLOCK",      [{"type": "Ready", "status": "False", "reason": "Unavailable"}],        ""),
    ("no Ready condition at all",         [{"type": "Synced", "status": "True"}],                                 ""),
    ("no conditions",                     [],                                                                     ""),
    # A non-Ready condition carrying the magic reason must not satisfy the gate.
    ("Synced=Unknown/ZeroEvidence only",  [{"type": "Synced", "status": "Unknown", "reason": "ZeroEvidence"}],     ""),
    # core-provider#318: a 403 reading a managed child. Accepted for the SAME reason as
    # ZeroEvidence -- a 403 from the authorization layer is a steady state, not a stage.
    ("Unknown/ChildrenForbidden + Synced=True",[{"type": "Ready", "status": "Unknown", "reason": "ChildrenForbidden"}, SYNCED_OK], "true"),
    # and the contrast that makes the allowlist meaningful rather than permissive:
    ("Unknown/NotObserved must BLOCK",    [{"type": "Ready", "status": "Unknown", "reason": "NotObserved"}],       ""),
    ("Unknown/Forbidden must BLOCK",      [{"type": "Ready", "status": "Unknown", "reason": "Forbidden"}],         ""),
    # core-provider#328's reason for "status.managed was never recorded". It is NOT allowlisted, and
    # today it blocks for the weakest possible reason: there is no else-arm, so anything unmatched
    # falls through. That is correct BY OMISSION. This case makes it correct BY INTENT.
    #
    # Why the distinction is worth a line of test: when #328 ships, the seven compositions on
    # krateo-057 that currently report Unknown/ZeroEvidence will move to this reason and START
    # BLOCKING their dependants, until each one's next Update writes its inventory and clears it.
    # The obvious way to "fix" that noise is to allowlist this reason next to ZeroEvidence -- which
    # would re-admit exactly the state the split was created to separate: a composition whose
    # children were never recorded because of a defect, passing a readiness gate on a bug's output.
    # With this case present, that change fails here and asks whoever makes it whether they meant it.
    #
    # The measurement behind it, 2026-11-04: 057 had 7 of 85 compositions with no status.managed key
    # at all, every one reporting Unknown/ZeroEvidence with a deployed helm release and
    # digest != managedDigest. demo had 0 of 43, with observability-page at the SAME chart version
    # publishing 75 managed children there -- so it is not the chart and not the chart version.
    ("Unknown/InventoryNotRecorded must BLOCK",
                                          [{"type": "Ready", "status": "Unknown", "reason": "InventoryNotRecorded"}], ""),
]


def main() -> int:
    if not HELPERS.is_file():
        print(f"::error::{HELPERS} not found — run from the repo root")
        return 1

    work = Path(tempfile.mkdtemp(prefix="ready-gate-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        # The real helpers, verbatim. Any drift between the shipped rule and the tested rule
        # is impossible by construction.
        shutil.copy(HELPERS, chart / "templates" / "_helpers.tpl")
        (chart / "templates" / "probe.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\ndata:\n'
            '{{- range $i, $c := .Values.cases }}\n'
            '  case{{ $i }}: {{ (include "inst.readyFromConditions" (list $c.conditions)) | quote }}\n'
            '{{- end }}\n')
        (chart / "values.yaml").write_text("cases: []\n")

        vals = work / "cases.yaml"
        import yaml
        vals.write_text(yaml.safe_dump(
            {"cases": [{"conditions": c} for _, c, _ in CASES]}, default_flow_style=False))

        p = subprocess.run(
            ["helm", "template", "probe", str(chart), "-f", str(vals)],
            capture_output=True, text=True)
        if p.returncode != 0:
            print("::error::render failed")
            print(p.stderr.strip()[:1500])
            return 1

        doc = next(d for d in yaml.safe_load_all(p.stdout) if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        data = doc.get("data") or {}

        failures = []
        for i, (name, _conds, want) in enumerate(CASES):
            got = data.get(f"case{i}")
            if got is None:
                failures.append(f"{name}: no output rendered (helper missing?)")
                continue
            verdict = "READY    " if got == "true" else "NOT READY"
            print(f"  {verdict}  {name}" + ("" if got == want else f"   <-- WANT {want!r}, GOT {got!r}"))
            if got != want:
                failures.append(f"{name}: want {want!r}, got {got!r}")

        if failures:
            print("::error::ready-gate: inst.readyFromConditions does not discriminate as required "
                  "(core-provider#165):")
            for f in failures:
                print(f"  {f}")
            return 1
        # ---- inst.ready: the version-agnostic lookup, and the structural half ----
        # inst.readyFromConditions decides from conditions it is HANDED; inst.ready is what fetches
        # the object. They are one decision in two functions, so they are gated together rather
        # than in two places -- a second gate over the same helper is how a reporter came to
        # contradict the gate it reported on earlier today.
        src_h = HELPERS.read_text()
        i = src_h.index('{{- define "inst.ready" -}}')
        body = src_h[i:src_h.index('{{- define "', i + 10)]
        if 'include "inst.servedTags"' in body:
            print("  PASS  inst.ready iterates the SERVED tags rather than one derived version")
        else:
            failures.append("inst.ready no longer iterates inst.servedTags. A dep may legitimately "
                            "live at an older served tag across a bump (inst.emitDecision keeps it "
                            "there on purpose), so a lookup keyed on the pinned version misses an "
                            "object that exists and reports a healthy dep as not-Ready.")
        if 'include "inst.apiVersion" (list $ver)' in body:
            failures.append("inst.ready builds an apiVersion from the pinned $ver again, which is "
                            "the #477 defect in its last remaining site.")
        else:
            print("  PASS  inst.ready does not build an apiVersion from the pinned version")

        # THE LOOKUP ITSELF, which `helm template` cannot reach because it DISABLES lookup. A
        # missing plugin FAILS rather than skips: a skipped test that reports success is how a gate
        # comes to vouch for an arm it never ran. Verified by running with HELM_PLUGINS pointed at
        # an empty directory -- exit 1, and no OK line.
        have = subprocess.run(["helm", "plugin", "list"], capture_output=True, text=True)
        if "unittest" not in (have.stdout or ""):
            print("::error::helm-unittest is not installed, so inst.ready's LOOKUP cannot be "
                  "exercised. Install it: helm plugin install "
                  "https://github.com/helm-unittest/helm-unittest")
            return 1
        ut = pathlib.Path(tempfile.mkdtemp(prefix="ready-lookup-"))
        try:
            uc = ut / "probe"
            (uc / "templates").mkdir(parents=True)
            (uc / "tests").mkdir(parents=True)
            (uc / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
            (uc / "values.yaml").write_text("namespaces:\n  krateo: krateo-system\n")
            (uc / "templates" / "_helpers.tpl").write_text(src_h)
            (uc / "templates" / "ready.yaml").write_text(READY_PROBE)
            (uc / "tests" / "ready_test.yaml").write_text(READY_SUITE)
            r = subprocess.run(["helm", "unittest", "probe"], cwd=str(ut),
                               capture_output=True, text=True)
            if r.returncode != 0:
                failures.append("the faked-cluster suite FAILED:\n" + (r.stdout or r.stderr)[:1200])
            else:
                nline = [l.strip() for l in (r.stdout or "").split("\n") if "Tests:" in l]
                print("  PASS  faked cluster: inst.ready finds the object at an OLDER served tag "
                      "and refuses a paused one   (%s)" % (nline[0] if nline else "ok"))
        finally:
            shutil.rmtree(ut, ignore_errors=True)

        if failures:
            print("::error::ready-gate: %d check(s) failed:" % len(failures))
            for f in failures:
                print("  " + f)
            return 1

        print(f"ready-gate: OK — all {len(CASES)} cases discriminate, "
              f"incl. Unknown/ZeroEvidence READY and Unknown/Creating BLOCKED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
