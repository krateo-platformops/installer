#!/usr/bin/env python3
"""CI guard: inst.readyFromConditions requires Synced=True, and inst.ready asks at ANY SERVED tag.

WHY THIS NEEDS helm-unittest AND NOT `helm template`. inst.ready's whole job is a `lookup` of the
live composition, and `helm template` DISABLES lookup -- every lookup returns empty, so the only
arm a plain render can reach is the uninteresting one. helm-unittest's `kubernetesProvider` FAKES
the cluster, which is the one mechanism that can drive the interesting arms. Proven before being
relied on: a probe asserting both directions (no objects -> the generate arm; the object present ->
the reuse arm WITH its decoded data) passes, so the fake really feeds lookup rather than silently
returning empty.

THE CHART IS NOT POLLUTED. The probe chart is built at test time from the REAL
chart/templates/_helpers.tpl, so there is no second copy of the helper to drift from the first, and
no probe template ships to a cluster. This is the same construction the other helper gates use;
what helm-unittest adds is the fake cluster.

WHAT A GREEN RUN DOES NOT COVER. The fake is not the apiserver: it does not evaluate served-version
semantics, so the "older served tag" case is exercised by TELLING the helper which tags are served
(inst.servedTags reads the $allCRDs slice it is handed) and faking an object at one of them. It
proves the helper looks where it should and reads what it finds. It does not prove the real cluster
would serve that tag.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HELPERS = Path("chart/templates/_helpers.tpl")

# Two conditions, one object: the matrix that matters is Ready x Synced.
CASES = [
    # (label, Ready status/reason, Synced status, expected inst.readyFromConditions)
    ("healthy: Ready=True + Synced=True",                "True",    "Available",      "True",  "true"),
    ("PAUSED: Ready=True + Synced=False -> NOT ready",   "True",    "ReconcilePaused","False", ""),
    ("ZeroEvidence + Synced=True -> accepted",           "Unknown", "ZeroEvidence",   "True",  "true"),
    ("ZeroEvidence + Synced=False -> NOT accepted",      "Unknown", "ZeroEvidence",   "False", ""),
    ("ChildrenForbidden + Synced=True -> accepted",      "Unknown", "ChildrenForbidden","True","true"),
    ("Ready=False + Synced=True -> not ready",           "False",   "Creating",       "True",  ""),
    ("Synced ABSENT -> not ready (cannot confirm)",      "True",    "Available",      None,    ""),
]

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
{{- range $i, $c := .Values.cases }}
  case{{ $i }}: {{ (include "inst.readyFromConditions" (list $c.conds)) | quote }}
{{- end }}
"""


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


def build(tmp, extra_templates=None):
    chart = Path(tmp) / "probe"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    (chart / "templates" / "_helpers.tpl").write_text(HELPERS.read_text())
    (chart / "templates" / "probe.yaml").write_text(PROBE)
    for name, body in (extra_templates or {}).items():
        (chart / "templates" / name).write_text(body)
    return chart


def render(chart, values):
    vf = Path(chart).parent / "v.yaml"
    vf.write_text(json.dumps(values))
    p = subprocess.run(["helm", "template", "probe", str(chart), "-f", str(vf)],
                       capture_output=True, text=True)
    return (p.stdout, None) if p.returncode == 0 else (None, (p.stderr or p.stdout).strip()[:400])


def main() -> int:
    if not HELPERS.is_file():
        print("::error::chart/templates/_helpers.tpl not found — run from the repo root")
        return 1
    failures = []
    tmp = tempfile.mkdtemp(prefix="ready-synced-")
    try:
        # ---- part 1: the Ready x Synced matrix, via a plain render (no lookup needed) ----
        cases = []
        for _l, rs, rr, ss, _w in CASES:
            conds = [{"type": "Ready", "status": rs, "reason": rr}]
            if ss is not None:
                conds.append({"type": "Synced", "status": ss, "reason": "x"})
            cases.append({"conds": conds})
        chart = build(tmp)
        out, err = render(chart, {"cases": cases})
        if out is None:
            print("::error::probe render failed — the gate cannot vouch for a helper it could not run")
            print(err)
            return 1
        import yaml
        doc = next(d for d in yaml.safe_load_all(out)
                   if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        data = doc.get("data") or {}
        for i, (label, _rs, _rr, _ss, want) in enumerate(CASES):
            got = data.get(f"case{i}")
            ok = got == want
            print(f"  {'PASS' if ok else 'FAIL'}  {label}\n          -> {got!r}"
                  + ("" if ok else f"   <-- WANT {want!r}"))
            if not ok:
                failures.append(f"{label}: want {want!r}, got {got!r}")

        # ---- part 2: the mutation self-check. Delete the Synced gate; the PAUSED case must flip ----
        mutant = HELPERS.read_text().replace(
            '{{- if ne $synced "true" -}}{{- $r = "" -}}{{- end -}}', "", 1)
        if mutant == HELPERS.read_text():
            failures.append("could not construct the mutant: the Synced gate line no longer matches "
                            "the pattern this gate deletes, so the self-check cannot run.")
        else:
            m = Path(tempfile.mkdtemp(prefix="ready-synced-mut-"))
            mc = m / "probe"
            (mc / "templates").mkdir(parents=True)
            (mc / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
            (mc / "templates" / "_helpers.tpl").write_text(mutant)
            (mc / "templates" / "probe.yaml").write_text(PROBE)
            mout, merr = render(mc, {"cases": cases})
            if mout is None:
                failures.append("the mutant did not render, so the self-check is inconclusive: "
                                + str(merr)[:200])
            else:
                mdoc = next(d for d in yaml.safe_load_all(mout)
                            if isinstance(d, dict) and d.get("kind") == "ConfigMap")
                if (mdoc.get("data") or {}).get("case1") != "true":
                    failures.append("MUTANT NOT CAUGHT: with the Synced gate deleted, the PAUSED "
                                    "case still read not-ready, so this gate cannot detect the "
                                    "regression it exists for.")
                else:
                    print("  PASS  self-check: deleting the Synced gate makes the PAUSED case "
                          "read ready, and this gate sees it")
            shutil.rmtree(m, ignore_errors=True)

        # ---- part 3: structural. inst.ready must not derive a single version. ----
        src = HELPERS.read_text()
        i = src.index('{{- define "inst.ready" -}}')
        body = src[i:src.index('{{- define "', i + 10)]
        if 'include "inst.servedTags"' in body:
            print("  PASS  inst.ready iterates the SERVED tags rather than one derived version")
        else:
            failures.append("inst.ready no longer iterates inst.servedTags. A dep may legitimately "
                            "live at an older served tag across a bump, so a lookup keyed on the "
                            "pinned version reports a healthy dep as not-Ready.")
        if 'include "inst.apiVersion" (list $ver)' in body:
            failures.append("inst.ready builds an apiVersion from the pinned $ver again, which is "
                            "the #477 defect in its last remaining site.")
        else:
            print("  PASS  inst.ready does not build an apiVersion from the pinned version")

        # ---- part 4: the LOOKUP, driven against a FAKED cluster via helm-unittest ----
        # This is the arm `helm template` cannot reach: inst.ready's whole job is to look the live
        # composition up, and a plain render returns empty for every lookup. The fake is the only
        # mechanism that exercises it, and the case that matters is the #477 one -- the object sits
        # at an OLDER SERVED TAG than the pin, which inst.emitDecision now keeps it at on purpose.
        have = subprocess.run(["helm", "plugin", "list"], capture_output=True, text=True)
        if "unittest" not in (have.stdout or ""):
            # A missing plugin must FAIL, not skip. A skipped test that reports success is how a
            # gate comes to vouch for an arm it never ran.
            print("::error::helm-unittest is not installed, so the LOOKUP arm cannot be exercised. "
                  "Install it (helm plugin install https://github.com/helm-unittest/helm-unittest) "
                  "— this gate will not report OK on the strength of the arms that need no cluster.")
            return 1
        ut = Path(tempfile.mkdtemp(prefix="ready-synced-ut-"))
        try:
            uc = ut / "probe"
            (uc / "templates").mkdir(parents=True)
            (uc / "tests").mkdir(parents=True)
            (uc / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
            (uc / "values.yaml").write_text("namespaces:\n  krateo: krateo-system\n")
            (uc / "templates" / "_helpers.tpl").write_text(HELPERS.read_text())
            (uc / "templates" / "ready.yaml").write_text(READY_PROBE)
            (uc / "tests" / "ready_test.yaml").write_text(READY_SUITE)
            r = subprocess.run(["helm", "unittest", "probe"], cwd=str(ut),
                               capture_output=True, text=True)
            if r.returncode != 0:
                failures.append("the faked-cluster suite FAILED — inst.ready does not read a live "
                                "composition the way this gate requires:\n"
                                + "\n".join("    " + l for l in (r.stdout or r.stderr).split("\n")
                                            if l.strip())[:1500])
            else:
                n = [l for l in (r.stdout or "").split("\n") if "Tests:" in l]
                print("  PASS  faked-cluster suite: inst.ready finds the object at an OLDER served "
                      "tag, and refuses a paused one   (%s)" % (n[0].strip() if n else "ok"))
        finally:
            shutil.rmtree(ut, ignore_errors=True)

        if failures:
            print("::error::ready/synced gate: the dependency readiness rule is not what this gate "
                  "requires. A PAUSED composition satisfying a dependency emits a dependant against "
                  "a component that is not converging and will not:")
            for f in failures:
                print(f"  {f}")
            return 1
        print(f"\nready-synced gate: OK — {len(CASES)} Ready x Synced arms, the mutant is caught, "
              f"and inst.ready reads the served tags.")
        print("NOT asserted: that a REAL cluster serves the tag inst.ready looks at. The lookup "
              "is exercised against helm-unittest's fake, which proves the helper looks where it "
              "should and reads what it finds — not that the apiserver would serve that version.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
