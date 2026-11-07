#!/usr/bin/env python3
"""Assert componentValuesUnset puts an EXPLICIT NULL in the CR merge patch.

WHY THIS GATE EXISTS. components-patch.json is applied as an RFC-7386 merge patch, where an
OMITTED key is PRESERVED and only an explicit null deletes. That is deliberate -- it stops a chart
upgrade clobbering operator-set values -- but it meant a componentValues key could be SET and
CHANGED and never REMOVED. Deleting it from the values file was SILENTLY INERT.

Measured on krateo-057 (2026-11-07): after removing componentValues.authn.env and rolling 0.3.519,
the live CR still held both keys and the rendered ConfigMap still carried AUTHN_DUMP_ENV. A sweep
found NINE components whose live componentValues were explained by neither the values file nor a
schema default -- the CR was no longer reconstructible from the repo.

WHAT THIS ASSERTS, and why rendering alone is not enough: a test that only checks "it renders"
passes whether or not the null is there. So each case below asserts the EXACT value at the path --
present, null, or the parent gone -- and the NO-OP case asserts the patch is UNCHANGED, because a
removal mechanism that corrupts unrelated values is worse than none.
"""
import json, os, subprocess, sys, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHART = os.path.join(ROOT, "chart")

# Only SCHEMA-DECLARED keys: componentValues.<component> is a closed object, so an invented key
# fails helm's validation and the gate would report a render error instead of a verdict.
VALUES = """
bootstrap:
  coreProvider:
    enabled: true
componentValues:
  authn:
    env:
      AUTHN_DEBUG: "false"
      AUTHN_DUMP_ENV: "false"
  portal:
    enableAdminUser: true
"""

_PREPARED = {"dir": None}

def prepared_chart():
    """A copy of the chart with a valid version and its dependencies built.

    The chart in-tree carries `version: CHART_VERSION`, substituted at release time, so
    `helm template` refuses it. My first run of this gate failed on exactly that and would have
    reported a render error rather than a verdict -- the same shape as a check that cannot fail.
    """
    if _PREPARED["dir"]:
        return _PREPARED["dir"]
    td = tempfile.mkdtemp(prefix="cvunset-")
    dst = os.path.join(td, "chart")
    shutil.copytree(CHART, dst)
    cy = os.path.join(dst, "Chart.yaml")
    txt = open(cy, encoding="utf-8").read().replace("version: CHART_VERSION", "version: 0.0.1-test")
    open(cy, "w", encoding="utf-8").write(txt)
    if not os.path.isdir(os.path.join(dst, "charts")):
        subprocess.run(["helm", "dependency", "build"], cwd=dst, capture_output=True, text=True)
    _PREPARED["dir"] = dst
    return dst

def patch_for(unset):
    with tempfile.TemporaryDirectory() as td:
        vf = os.path.join(td, "v.yaml")
        with open(vf, "w") as f:
            f.write(VALUES)
            if unset:
                f.write("componentValuesUnset:\n")
                for u in unset:
                    f.write("  - %s\n" % u)
        cmd = ["helm", "template", "t", prepared_chart(), "--kube-version", "1.36.0", "-f", vf]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr.strip().split("\n")[0][:160]
        import yaml
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap" and "self-cr" in d["metadata"]["name"]:
                raw = (d.get("data") or {}).get("components-patch.json")
                if raw:
                    return (json.loads(raw).get("spec") or {}).get("componentValues") or {}, None
        return None, "no components-patch.json in the render"

def main():
    if shutil.which("helm") is None:
        print("check-componentvalues-unset: SKIPPED (no helm)"); return 0
    base, err = patch_for([])
    if base is None:
        print("::error::baseline render failed: %s" % err); return 1
    # CONTROL: the baseline must actually CONTAIN the key, or every assertion below is vacuous
    if (base.get("authn") or {}).get("env", {}).get("AUTHN_DUMP_ENV") != "false":
        print("::error::CONTROL FAILED -- the baseline patch does not contain the key under test, "
              "so a later 'it is null' result would prove nothing"); return 1
    print("  ok   control: baseline patch contains authn.env.AUTHN_DUMP_ENV = 'false'")

    fails = []
    cases = [
        (["authn.env.AUTHN_DUMP_ENV"], lambda cv: cv["authn"]["env"]["AUTHN_DUMP_ENV"] is None
                                              and cv["authn"]["env"]["AUTHN_DEBUG"] == "false",
         "one key -> null, sibling untouched"),
        (["authn.env.AUTHN_DUMP_ENV", "authn.env.AUTHN_DEBUG"],
         lambda cv: cv["authn"]["env"]["AUTHN_DUMP_ENV"] is None and cv["authn"]["env"]["AUTHN_DEBUG"] is None,
         "both keys -> null"),
        (["authn.env"], lambda cv: cv["authn"]["env"] is None, "whole object -> null"),
        (["nosuch.component.key"], lambda cv: cv == base, "no-op path leaves the patch UNCHANGED"),
    ]
    for unset, check, label in cases:
        cv, err = patch_for(unset)
        if cv is None:
            fails.append(label); print("  FAIL %-44s render failed: %s" % (label, err)); continue
        try:
            ok = check(cv)
        except Exception as e:
            ok = False
        if not ok: fails.append(label)
        print("  %s %-44s %s" % ("ok  " if ok else "FAIL", label, json.dumps(cv.get("authn"))))
        # an unset must never disturb another component
        if cv.get("portal") != base.get("portal"):
            fails.append(label + " [collateral damage to portal]")
            print("  FAIL %-44s it changed componentValues.portal" % label)

    if fails:
        print("::error::check-componentvalues-unset: %d case(s) failed: %s" % (len(fails), fails))
        return 1
    print("check-componentvalues-unset: OK — 1 control + %d cases, nulls land in the patch" % len(cases))
    return 0

if __name__ == "__main__":
    sys.exit(main())
