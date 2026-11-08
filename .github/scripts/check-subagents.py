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

    if fails:
        print("::error::check-subagents: %d case(s) failed: %s" % (len(fails), fails))
        return 1
    print("check-subagents: OK — 1 control + %d cases + 1 falsifier + 1 must-never-ship" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
