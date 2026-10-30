#!/usr/bin/env python3
"""CI guard: the image-pull Secret's render gate and the imagePullSecrets injection gate must be
the SAME predicate.

WHY. compositions.yaml injects `imagePullSecrets: [<name>]` into every component listed in
inst.privateImageComponents, gated on `inst.imagePullOn`. imagepull-secret.yaml renders the Secret
that name refers to. If those two gates differ, the injection can fire while the Secret is never
rendered -- a dangling reference, which is precisely the failure both files' comments warn about.

They DID differ. The Secret's gate was a hardcoded `coreAgents OR specialistAgents`, while the
injection has no feature gate at all. The snowplow session hit it on bench cluster run19b2:
features.agentGateway on with both agent features off, registryAuth set, and
agentgateway-policies-{ui-token-hop,cert-replay-hop} in ImagePullBackOff for 43 minutes against a
NotFound krateo-registry-image-pull, falling back to anonymous and getting 401.

It was never only agentGateway. FOUR of the seven inst.privateImageComponents entries had features
outside the old gate: structure-graph-mcp-server (structureGraph), agentgateway-policies
(agentGateway, two paths) and alert-provider (portal). Both live clusters escaped for different
reasons -- 057 has coreAgents on, demo has registryAuth disabled so nothing is injected -- which is
exactly why a bench cluster found it and neither production one did.

WHAT IT ASSERTS, structurally, because `helm template` disables `lookup` and so cannot render this
Secret at all (no token, no dockerconfigjson, no object) -- a render-based test here would pass
vacuously:
  1. both files reference inst.imagePullOn
  2. imagepull-secret.yaml's gate line IS inst.imagePullOn, not a feature expression
  3. imagepull-secret.yaml contains no featureEnabled call in a gate position
Self-test reintroduces the old predicate and requires each rule to fire.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SECRET = ROOT / "chart" / "templates" / "imagepull-secret.yaml"
COMPS = ROOT / "chart" / "templates" / "compositions.yaml"
GATE_RE = re.compile(r'^\{\{-\s*\$privateImageOn\s*:=\s*(.+?)\s*\}\}\s*$', re.M)


def strip_comments(text):
    """Drop {{/* ... */}} blocks so prose mentioning a predicate is not read as code."""
    return re.sub(r"\{\{/\*.*?\*/\}\}", "", text, flags=re.S)


def findings(secret_text, comps_text):
    out = []
    sec = strip_comments(secret_text)
    cmp_ = strip_comments(comps_text)
    if "inst.imagePullOn" not in cmp_:
        out.append("compositions.yaml no longer gates the injection on inst.imagePullOn")
    if "inst.imagePullOn" not in sec:
        out.append("imagepull-secret.yaml does not gate on inst.imagePullOn")
    m = GATE_RE.search(sec)
    if not m:
        out.append("imagepull-secret.yaml has no recognisable $privateImageOn gate line")
    else:
        expr = m.group(1)
        if "inst.imagePullOn" not in expr:
            out.append(f"the Secret's gate is not inst.imagePullOn: {expr[:90]}")
        if "featureEnabled" in expr:
            out.append("the Secret's gate hardcodes features again: "
                       "a feature list cannot track inst.privateImageComponents")
    return out


def main():
    secret_text = SECRET.read_text()
    comps_text = COMPS.read_text()

    real = findings(secret_text, comps_text)

    # self-test: put the old predicate back and require every rule to fire
    broken = GATE_RE.sub(
        '{{- $privateImageOn := or (eq (include "inst.featureEnabled" (list . "coreAgents")) "true") '
        '(eq (include "inst.featureEnabled" (list . "specialistAgents")) "true") }}',
        strip_comments(secret_text), count=1)
    st = findings(broken, comps_text)
    if not st:
        print("::error::SELF-TEST FAILED — reintroducing the hardcoded feature gate produced no "
              "finding, so this guard would not have caught the run19b2 defect.")
        return 2
    print(f"  self-test OK — the old predicate is rejected ({len(st)} rule(s) fire: "
          f"{'; '.join(f[:48] for f in st)})")

    if real:
        print(f"::error::{len(real)} image-pull gate parity problem(s):")
        for f in real:
            print(f"  {f}")
        print("  The injection and the Secret MUST read one predicate, or imagePullSecrets names a "
              "Secret that is never created and pods sit in ImagePullBackOff.")
        return 1
    print("imagepull-gate-parity: OK — the Secret and the injection read the same inst.imagePullOn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
