#!/usr/bin/env python3
"""
Exercises inst.subAgents — the agentgateway's A2A delegation-target derivation.

WHY THIS GATE EXISTS AT ALL. The derivation used to sit INLINE in
templates/compositions.yaml, and that file is lookup-GATED: line 37 reads the live CRD list with
`lookup`, which `helm template` DISABLES. So the file renders ZERO composition objects locally and
the derivation could not be exercised in any way — not by rendering 057's real values, not by a
minimal values file, not at all. Every "I rendered it and diffed it" claim about that block was
weaker than it sounded.

The derivation itself needs NO cluster: only $components, $top.Values.componentValues and
inst.featureEnabled. Extracting it into a named helper makes it reachable from a probe chart by
plain render, with no faked cluster.

THE TWO BUGS THIS FOUND, both invisible until the logic became testable:
  1. the orchestrator's route name was the LITERAL "autopilot", so two orchestrators pinned side by
     side emitted the same name twice: a duplicate entry and NO route for the second agent.
  2. k8s-agent and helm-agent were appended by EVERY orchestrator, so two orchestrators emitted
     each of those names twice as well. The agentName fix alone does not address this. The route is
     now contributed only by the orchestrator that OWNS the Agent (`owned` not false).
  3. (2026-11-14) `agentName` was read in the ORCHESTRATOR branch only. A non-orchestrator that set
     it was routed by chart-or-name instead -- silently, because a route IS emitted, just under a
     name no Service answers. Found while registering nightly-review, whose component and chart are
     both `nightly-review` while its Agent object is `nightly-review-agent`.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "chart")

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  subAgents: |
{{ (include "inst.subAgents" (list $ .Values.components)) | default "" | indent 4 }}
"""

ONE_ORCH_NO_NAME = {
    "features": {"coreAgents": True},
    "componentValues": {"krateo-autopilot": {"agents": {"k8s": {"enabled": True}, "helm": {"enabled": True}}}},
    "components": [
        {"name": "krateo-autopilot", "agent": True, "orchestrator": True, "feature": "coreAgents"},
        {"name": "incident-agent", "agent": True, "feature": "coreAgents"},
    ],
}
ONE_ORCH_NAMED = yaml.safe_load(yaml.safe_dump(ONE_ORCH_NO_NAME))
ONE_ORCH_NAMED["components"][0]["agentName"] = "autopilot"

TWO_ORCH = {
    "features": {"coreAgents": True},
    "componentValues": {
        "krateo-autopilot": {"agents": {"k8s": {"enabled": True}, "helm": {"enabled": True}}},
        "agentiko": {"agents": {"k8s": {"enabled": True, "owned": False},
                                "helm": {"enabled": True, "owned": False}}},
    },
    "components": [
        {"name": "krateo-autopilot", "agent": True, "orchestrator": True, "feature": "coreAgents",
         "agentName": "autopilot"},
        {"name": "agentiko", "agent": True, "orchestrator": True, "feature": "coreAgents",
         "agentName": "agentiko"},
        {"name": "incident-agent", "agent": True, "feature": "coreAgents"},
    ],
}
TWO_ORCH_BOTH_OWNING = {
    "features": {"coreAgents": True},
    "componentValues": {
        "krateo-autopilot": {"agents": {"k8s": {"enabled": True}, "helm": {"enabled": True}}},
        "agentiko": {"agents": {"k8s": {"enabled": True}, "helm": {"enabled": True}}},
    },
    "components": [
        {"name": "krateo-autopilot", "agent": True, "orchestrator": True, "feature": "coreAgents",
         "agentName": "autopilot"},
        {"name": "agentiko", "agent": True, "orchestrator": True, "feature": "coreAgents",
         "agentName": "agentiko"},
    ],
}
FEATURE_OFF = {
    "features": {"coreAgents": False},
    "componentValues": {},
    "components": [{"name": "krateo-autopilot", "agent": True, "orchestrator": True,
                    "feature": "coreAgents", "agentName": "autopilot"}],
}
NON_AGENT_ONLY = {
    "features": {"coreAgents": True},
    "componentValues": {},
    "components": [{"name": "portal", "feature": "portal"}],
}
CHART_NAME_WINS = {
    "features": {"coreAgents": True},
    "componentValues": {},
    "components": [{"name": "frontend-agent", "chart": "frontend-agent-chart", "agent": True,
                    "feature": "coreAgents"}],
}
# agentName on a NON-orchestrator. Until 2026-11-14 the else-branch read only chart-or-name, so a
# non-orchestrator setting agentName was routed under the WRONG name -- silently, because a route
# IS emitted, just pointing at a Service that does not exist. CHART_NAME_WINS above is this case's
# mirror: same shape without agentName, still routing by chart name, which is what proves agentName
# is what changes the answer rather than something incidental to the fixture.
AGENTNAME_WINS_OVER_CHART = {
    "features": {"coreAgents": True},
    "componentValues": {},
    "components": [{"name": "frontend-agent", "chart": "frontend-agent-chart", "agent": True,
                    "feature": "coreAgents", "agentName": "frontend-agent-routed"}],
}
# nightly-review's REAL pin shape: component and chart are both `nightly-review` while the chart's
# Agent object is `nightly-review-agent` (observability confirmed metadata.name is
# .Values.reviewer.name, default nightly-review-agent). Without agentName the route name would be
# `nightly-review` and the HTTPRoute would resolve to nothing.
AGENTNAME_NO_CHART_FIELD = {
    "features": {"specialistAgents": True},
    "componentValues": {},
    "components": [{"name": "nightly-review", "agent": True, "feature": "specialistAgents",
                    "agentName": "nightly-review-agent"}],
}
# An orchestrator's agentName must keep behaving EXACTLY as before: the change is in the else-branch
# only, and this asserts the if-branch was not touched by accident.
ORCH_AGENTNAME_UNCHANGED = {
    "features": {"coreAgents": True},
    "componentValues": {},
    "components": [{"name": "agentiko", "agent": True, "orchestrator": True,
                    "feature": "coreAgents", "agentName": "agentiko"}],
}

def _two_orch(ap_owned, ag_owned):
    """Two orchestrators, with `owned` set explicitly on each one's k8s and helm sub-agents."""
    return {
        "features": {"coreAgents": True},
        "componentValues": {
            "krateo-autopilot": {"agents": {"k8s": {"enabled": True, "owned": ap_owned},
                                            "helm": {"enabled": True, "owned": ap_owned}}},
            "agentiko": {"agents": {"k8s": {"enabled": True, "owned": ag_owned},
                                    "helm": {"enabled": True, "owned": ag_owned}}},
        },
        "components": [
            {"name": "krateo-autopilot", "agent": True, "orchestrator": True,
             "feature": "coreAgents", "agentName": "autopilot"},
            {"name": "agentiko", "agent": True, "orchestrator": True,
             "feature": "coreAgents", "agentName": "agentiko"},
        ],
    }


# THE HANDOVER, which the plan performs as ONE release: autopilot gives the sub-agents up in the
# same render in which agentiko takes them. Exactly one route each, none missing.
HANDOVER = _two_orch(False, True)

# ITS MIRROR, THE STATE THAT MUST NEVER SHIP: neither orchestrator owns them, so NO route is
# emitted for either sub-agent. agentgateway-policies' own values.yaml says a deployed agent with no
# subAgents entry FAILS HARD — delegation to it dies at the first hop. This is what splitting the
# handover across two releases would produce for the length of the gap, which is why the two flips
# are one release.
NEITHER_OWNS = _two_orch(False, False)

_PROBE_DIR = {"d": None}


def probe_dir():
    if _PROBE_DIR["d"]:
        return _PROBE_DIR["d"]
    td = tempfile.mkdtemp(prefix="subagents-")
    p = os.path.join(td, "probe")
    os.makedirs(os.path.join(p, "templates"))
    with open(os.path.join(p, "Chart.yaml"), "w") as f:
        f.write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
    with open(os.path.join(p, "values.yaml"), "w") as f:
        f.write("features: {}\ncomponentValues: {}\ncomponents: []\n")
    shutil.copy(os.path.join(CHART, "templates", "_helpers.tpl"),
                os.path.join(p, "templates", "_helpers.tpl"))
    with open(os.path.join(p, "templates", "sub.yaml"), "w") as f:
        f.write(PROBE)
    _PROBE_DIR["d"] = p
    return p


def names(values):
    """Return the derived subAgents names, or None with the error text."""
    with tempfile.TemporaryDirectory() as td:
        vf = os.path.join(td, "v.yaml")
        with open(vf, "w") as f:
            yaml.safe_dump(values, f)
        r = subprocess.run(["helm", "template", "probe", probe_dir(), "-f", vf],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap":
                raw = (d.get("data") or {}).get("subAgents") or ""
                lst = yaml.safe_load(raw) or []
                return [x.get("name") for x in lst], None
        return None, "no probe ConfigMap in the render"


# ---------------------------------------------------------------------------------------------
# extraAgents: an orchestrator routes the fleet and is NOT a member of it. Asserted here because
# the same fixtures drive it, and because listing the other orchestrator made kagent fail to
# compile BOTH agents on 057 at 0.3.540 — a live outage of config propagation that `Ready=True`
# hid, since the pod kept serving its last good compile.
# ---------------------------------------------------------------------------------------------
# The SAME derivation with the OLD self-only condition (`ne $comp.name $c.name`). It exists purely
# as a falsifier: it MUST list the other orchestrator, proving that `not $comp.orchestrator` is what
# removes it rather than something incidental to the fixture.
EXTRA_PROBE_OLD = """apiVersion: v1
kind: ConfigMap
metadata:
  name: extraold
data:
{{- range $c := .Values.components }}
{{- if $c.orchestrator }}
{{- $enabled := dict }}
{{- range $comp := $.Values.components }}
{{- if eq (include "inst.featureEnabled" (list $ $comp.feature)) "true" }}
{{- $_ := set $enabled $comp.name true }}
{{- end }}
{{- end }}
{{- $fleet := list }}
{{- range $comp := $.Values.components }}
{{- if and $comp.agent (ne $comp.name $c.name) (hasKey $enabled $comp.name) }}
{{- $fleet = append $fleet (default $comp.name $comp.chart) }}
{{- end }}
{{- end }}
  {{ $c.name }}: {{ $fleet | join "," | quote }}
{{- end }}
{{- end }}
"""

EXTRA_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: extra
data:
{{- range $c := .Values.components }}
{{- if $c.orchestrator }}
{{- $enabled := dict }}
{{- range $comp := $.Values.components }}
{{- if eq (include "inst.featureEnabled" (list $ $comp.feature)) "true" }}
{{- $_ := set $enabled $comp.name true }}
{{- end }}
{{- end }}
{{- $fleet := list }}
{{- range $comp := $.Values.components }}
{{- if and $comp.agent (not $comp.orchestrator) (hasKey $enabled $comp.name) }}
{{- $fleet = append $fleet (default $comp.name $comp.chart) }}
{{- end }}
{{- end }}
  {{ $c.name }}: {{ $fleet | join "," | quote }}
{{- end }}
{{- end }}
"""


def _extra(values, probe_src, name):
    d = probe_dir()
    fn = os.path.join(d, "templates", name + ".yaml")
    with open(fn, "w") as f:
        f.write(probe_src)
    try:
        with tempfile.TemporaryDirectory() as td:
            vf = os.path.join(td, "v.yaml")
            with open(vf, "w") as f:
                yaml.safe_dump(values, f)
            r = subprocess.run(["helm", "template", "probe", d, "-f", vf],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
            for doc in yaml.safe_load_all(r.stdout):
                if isinstance(doc, dict) and doc.get("kind") == "ConfigMap" \
                        and doc["metadata"]["name"] == name:
                    return {k: ([x for x in v.split(",") if x] if v else [])
                            for k, v in (doc.get("data") or {}).items()}, None
            return None, "no %s ConfigMap" % name
    finally:
        os.remove(fn)


def extra_agents_old(values):
    """The OLD self-only condition, for the falsifier."""
    return _extra(values, EXTRA_PROBE_OLD, "extraold")


def extra_agents(values):
    """Return {orchestrator: [extraAgents names]} from the same derivation the chart uses."""
    return _extra(values, EXTRA_PROBE, "extra")


def main() -> int:
    if shutil.which("helm") is None:
        print("check-subagents: SKIPPED (no helm)")
        return 0

    fails = []

    # ---- CONTROL FIRST: the probe must produce a NON-EMPTY list for a normal case, or every
    # ---- "it is absent" result below would be vacuous.
    base, err = names(ONE_ORCH_NO_NAME)
    if base is None:
        print("::error::the probe itself failed to render: %s" % err)
        return 1
    if not base:
        print("::error::CONTROL FAILED -- the probe derives an EMPTY list for a normal "
              "single-orchestrator case, so no assertion below would mean anything")
        return 1
    print("  ok   control: the probe derives %d entries for a normal case" % len(base))

    cases = [
        (ONE_ORCH_NO_NAME, ["autopilot", "k8s-agent", "helm-agent", "incident-agent"],
         "one orchestrator, NO agentName -> the historical literal"),
        (ONE_ORCH_NAMED, ["autopilot", "k8s-agent", "helm-agent", "incident-agent"],
         "agentName: autopilot is IDENTICAL to omitting it"),
        (TWO_ORCH, ["autopilot", "k8s-agent", "helm-agent", "agentiko", "incident-agent"],
         "TWO orchestrators, the second owning nothing -> one route each, NO duplicates"),
        (FEATURE_OFF, [], "the feature off -> nothing derived"),
        (NON_AGENT_ONLY, [], "a non-agent component contributes nothing"),
        (CHART_NAME_WINS, ["frontend-agent-chart"], "a non-orchestrator agent routes by CHART name"),
        (AGENTNAME_WINS_OVER_CHART, ["frontend-agent-routed"],
         "a non-orchestrator's agentName WINS over its chart name"),
        (AGENTNAME_NO_CHART_FIELD, ["nightly-review-agent"],
         "nightly-review's real shape: agentName with no chart: field"),
        (ORCH_AGENTNAME_UNCHANGED, ["agentiko"],
         "an ORCHESTRATOR's agentName is unchanged by the else-branch fix"),
        (HANDOVER, ["autopilot", "agentiko", "k8s-agent", "helm-agent"],
         "THE HANDOVER in ONE release: autopilot owned:false + agentiko owned:true"),
    ]
    for values, want, label in cases:
        got, err = names(values)
        if got is None:
            fails.append(label)
            print("  FAIL %-62s render failed: %s" % (label, err))
            continue
        ok = got == want
        if not ok:
            fails.append(label)
        print("  %s %-62s %s" % ("ok  " if ok else "FAIL", label, got))
        if not ok:
            print("       wanted %s" % want)

    # ---- the duplicate case must STILL produce duplicates, because it is a MISCONFIGURATION
    # ---- (two orchestrators both claiming to own the same sub-agents). A gate that cannot
    # ---- distinguish the broken input from the fixed one proves nothing.
    got, err = names(TWO_ORCH_BOTH_OWNING)
    if got is None:
        fails.append("the both-owning probe")
        print("  FAIL both-owning render failed: %s" % err)
    else:
        dups = sorted({n for n in got if got.count(n) > 1})
        if dups:
            print("  ok   %-62s duplicates %s" % ("FALSIFIER: two orchestrators both OWNING still duplicate", dups))
        else:
            fails.append("falsifier")
            print("  FAIL FALSIFIER -- two orchestrators both owning produced NO duplicates, so "
                  "the `owned` gate is not what removes them and this suite cannot fail")

    # ---- THE STATE THAT MUST NEVER SHIP: neither orchestrator owns the sub-agents, so NEITHER
    # ---- emits their routes and delegation to them dies at the first hop. Asserted explicitly so
    # ---- that the suite DOCUMENTS the outage rather than leaving it to be rediscovered, and so
    # ---- that splitting the handover across two releases is visibly wrong.
    got, err = names(NEITHER_OWNS)
    if got is None:
        fails.append("the neither-owns probe")
        print("  FAIL neither-owns render failed: %s" % err)
    else:
        missing = [n for n in ("k8s-agent", "helm-agent") if n in got]
        if got == ["autopilot", "agentiko"] and not missing:
            print("  ok   %-62s %s" % ("MUST NEVER SHIP: neither owns -> NO k8s/helm route at all", got))
        else:
            fails.append("neither-owns")
            print("  FAIL neither-owns expected exactly ['autopilot','agentiko'], got %s" % got)

    # ---- extraAgents: no orchestrator may appear in another orchestrator's fleet -------------
    ea, err = extra_agents(TWO_ORCH)
    if ea is None:
        fails.append("extraAgents two-orchestrator probe")
        print("  FAIL extraAgents probe failed: %s" % err)
    else:
        banned = {"autopilot", "krateo-autopilot", "agentiko"}
        bad = {k: sorted(set(v) & banned) for k, v in ea.items() if set(v) & banned}
        if bad:
            fails.append("extraAgents excludes orchestrators")
            print("  FAIL %-62s %s" % ("an orchestrator appears in a fleet", bad))
        else:
            print("  ok   %-62s %s" % ("TWO orchestrators: neither is in the other's extraAgents",
                                       {k: len(v) for k, v in ea.items()}))
        if not ea or any(len(v) == 0 for v in ea.values()):
            fails.append("extraAgents non-empty control")
            print("  FAIL CONTROL -- a fleet came back EMPTY, so 'no orchestrator in it' is vacuous")

    eao, err = extra_agents_old(TWO_ORCH)
    if eao is None:
        fails.append("extraAgents falsifier probe")
        print("  FAIL extraAgents falsifier probe failed: %s" % err)
    else:
        banned = {"autopilot", "krateo-autopilot", "agentiko"}
        leaked = {k: sorted(set(v) & banned) for k, v in eao.items() if set(v) & banned}
        if leaked:
            print("  ok   %-62s %s" % ("FALSIFIER: the OLD self-only condition DOES leak the other", leaked))
        else:
            fails.append("extraAgents falsifier")
            print("  FAIL FALSIFIER -- the old self-only condition did NOT leak the other "
                  "orchestrator, so `not $comp.orchestrator` is not what removes it and this "
                  "assertion cannot fail")

    ea1, err = extra_agents(ONE_ORCH_NO_NAME)
    if ea1 is None:
        fails.append("extraAgents single-orchestrator probe")
        print("  FAIL single-orchestrator extraAgents probe failed: %s" % err)
    else:
        want = {"krateo-autopilot": ["incident-agent"]}
        ok = ea1 == want
        if not ok:
            fails.append("extraAgents single orchestrator unchanged")
        print("  %s %-62s %s" % ("ok  " if ok else "FAIL",
                                 "ONE orchestrator: its fleet is the non-orchestrator agents", ea1))

    if fails:
        print("::error::check-subagents: %d case(s) failed: %s" % (len(fails), fails))
        return 1
    print("check-subagents: OK — 1 control + %d cases + 1 falsifier + 1 must-never-ship + 2 extraAgents + 1 falsifier" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
