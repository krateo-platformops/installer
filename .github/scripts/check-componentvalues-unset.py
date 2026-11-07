#!/usr/bin/env python3
"""
Asserts that componentValuesUnset puts an EXPLICIT NULL into the CR merge patch.

components-patch.json is an RFC-7386 merge patch: an OMITTED key is PRESERVED and only an explicit
null deletes. That protects operator-set values from a chart upgrade -- and it meant a
componentValues key could be SET and CHANGED but never REMOVED, because the schema types these
fields so a literal null cannot be written in a values file either.

THIS GATE EXISTS IN ITS CURRENT FORM BECAUSE ITS FIRST VERSION PASSED ON A BROKEN FEATURE.
Every case used one VALUES block that DECLARED componentValues.authn.env, so it only ever tested
"remove a key you still declare". The sequence that actually occurs is the opposite:

    1. the operator removes the key from the values file
    2. the next roll shows the live CR STILL CARRIES IT, because omission preserves
    3. only then is componentValuesUnset added -- and by now the parent is GONE from values

The first helper required every parent to already exist, so in that sequence it emitted NOTHING.
Measured on krateo-057 at installer 0.3.524: `componentValuesUnset: [authn.env]` with
componentValues.authn absent from the values produced no null at all, and the CR kept
{"env":{"AUTHN_DEBUG":"false","AUTHN_DUMP_ENV":"false"}}. The gate was green throughout.

So this gate now runs TWO values shapes -- parent present AND parent absent -- and asserts the
two must-fail guards as well, because a guard that cannot refuse is not a guard.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "..", "..", "chart")

# Only SCHEMA-DECLARED keys: componentValues.<component> is a closed object, so an invented key
# fails helm's validation and the gate would report a render error instead of a verdict.
BASE = """
bootstrap:
  coreProvider:
    enabled: true
"""

# Shape A -- the parent IS declared. This is what the first version of the gate tested, and it is
# the easier half: removing a key you still declare.
VALUES_PARENT_PRESENT = BASE + """
componentValues:
  authn:
    env:
      AUTHN_DEBUG: "false"
      AUTHN_DUMP_ENV: "false"
  portal:
    enableAdminUser: true
"""

# Shape B -- THE CASE THAT ACTUALLY OCCURS. The parent is ABSENT from componentValues, because the
# operator already removed it and discovered the merge patch preserved it anyway.
VALUES_PARENT_ABSENT = BASE + """
componentValues:
  portal:
    enableAdminUser: true
"""

_PREPARED = {"dir": None}


def prepared_chart():
    """Copy the chart, give it a real version and build deps.

    The committed Chart.yaml carries `version: CHART_VERSION`, a placeholder the release workflow
    substitutes, and `helm template` refuses it. The first run of this gate failed on exactly that
    and would have reported a render error rather than a verdict -- the same shape as a check that
    cannot fail.
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


def render(values, unset):
    """Return (componentValues-from-the-patch, error-text). Exactly one is None."""
    with tempfile.TemporaryDirectory() as td:
        vf = os.path.join(td, "v.yaml")
        with open(vf, "w") as f:
            f.write(values)
            if unset:
                f.write("componentValuesUnset:\n")
                for u in unset:
                    f.write("  - %s\n" % u)
        cmd = ["helm", "template", "t", prepared_chart(), "--kube-version", "1.36.0", "-f", vf]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
        import yaml
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap" and "self-cr" in d["metadata"]["name"]:
                raw = (d.get("data") or {}).get("components-patch.json")
                if raw:
                    return (json.loads(raw).get("spec") or {}).get("componentValues") or {}, None
        return None, "no components-patch.json in the render"


def main():
    if shutil.which("helm") is None:
        print("check-componentvalues-unset: SKIPPED (no helm)")
        return 0

    fails = []

    # ---- CONTROLS FIRST -------------------------------------------------------------------
    # Both baselines must render, and shape A's baseline must CONTAIN the key under test --
    # otherwise "it is null now" proves nothing, because it was never there.
    base_a, err = render(VALUES_PARENT_PRESENT, [])
    if base_a is None:
        print("::error::baseline render (parent present) failed: %s" % err)
        return 1
    if (base_a.get("authn") or {}).get("env", {}).get("AUTHN_DUMP_ENV") != "false":
        print("::error::CONTROL FAILED -- shape A's baseline does not contain authn.env.AUTHN_DUMP_ENV, "
              "so a later 'it is null' result would prove nothing")
        return 1
    print("  ok   control A: baseline contains authn.env.AUTHN_DUMP_ENV = 'false'")

    base_b, err = render(VALUES_PARENT_ABSENT, [])
    if base_b is None:
        print("::error::baseline render (parent absent) failed: %s" % err)
        return 1
    if "authn" in base_b:
        print("::error::CONTROL FAILED -- shape B's baseline already contains componentValues.authn, "
              "so it does not test the absent-parent case at all")
        return 1
    print("  ok   control B: baseline does NOT contain componentValues.authn")

    # ---- the cases that must PRODUCE A NULL ------------------------------------------------
    cases = [
        (VALUES_PARENT_PRESENT, ["authn.env.AUTHN_DUMP_ENV"],
         lambda cv: cv["authn"]["env"]["AUTHN_DUMP_ENV"] is None and cv["authn"]["env"]["AUTHN_DEBUG"] == "false",
         "A: one key -> null, sibling untouched"),
        (VALUES_PARENT_PRESENT, ["authn.env.AUTHN_DUMP_ENV", "authn.env.AUTHN_DEBUG"],
         lambda cv: cv["authn"]["env"]["AUTHN_DUMP_ENV"] is None and cv["authn"]["env"]["AUTHN_DEBUG"] is None,
         "A: both keys -> null"),
        (VALUES_PARENT_PRESENT, ["authn.env"],
         lambda cv: cv["authn"]["env"] is None,
         "A: whole object -> null"),
        # THE REGRESSION TEST. The first helper emitted nothing here and the roll changed nothing.
        (VALUES_PARENT_ABSENT, ["authn.env"],
         lambda cv: cv.get("authn") == {"env": None},
         "B: ABSENT parent -> null (the 0.3.524 case)"),
        (VALUES_PARENT_ABSENT, ["authn.env.AUTHN_DUMP_ENV"],
         lambda cv: cv.get("authn") == {"env": {"AUTHN_DUMP_ENV": None}},
         "B: ABSENT parent, one leaf -> null"),
        # Idempotency: deleting an already-absent key is a no-op under RFC-7386, so re-running a
        # values file after the unset has taken effect must stay safe.
        (VALUES_PARENT_ABSENT, ["authn.env", "authn.env"],
         lambda cv: cv.get("authn") == {"env": None},
         "B: the same path twice is idempotent"),
    ]
    for values, unset, check, label in cases:
        base = base_a if values is VALUES_PARENT_PRESENT else base_b
        cv, err = render(values, unset)
        if cv is None:
            fails.append(label)
            print("  FAIL %-44s render failed: %s" % (label, err))
            continue
        try:
            ok = check(cv)
        except Exception:
            ok = False
        if not ok:
            fails.append(label)
        print("  %s %-44s %s" % ("ok  " if ok else "FAIL", label, json.dumps(cv.get("authn"))))
        # an unset must never disturb another component
        if cv.get("portal") != base.get("portal"):
            fails.append(label + " [collateral damage to portal]")
            print("  FAIL %-44s it changed componentValues.portal" % label)

    # ---- the guards that must REFUSE -------------------------------------------------------
    # Creating intermediates means a typo would mint a componentValues entry the Installer CRD
    # rejects (additionalProperties: false), which would break EVERY componentValues update rather
    # than just its own. A guard that cannot refuse is not a guard, so both are asserted.
    guards = [
        (["nosuch-component.env"], "is not a known component", "GUARD: unknown component REFUSED"),
        (["authn"], "needs at least", "GUARD: a bare component name REFUSED"),
    ]
    for unset, needle, label in guards:
        cv, err = render(VALUES_PARENT_ABSENT, unset)
        if cv is not None:
            fails.append(label)
            print("  FAIL %-44s it RENDERED instead of refusing: %s" % (label, json.dumps(cv.get("authn"))))
        elif needle not in (err or ""):
            fails.append(label)
            print("  FAIL %-44s refused, but not for the stated reason: %s" % (label, err))
        else:
            print("  ok   %-44s refused with the right message" % label)

    if fails:
        print("::error::check-componentvalues-unset: %d case(s) failed: %s" % (len(fails), fails))
        return 1
    print("check-componentvalues-unset: OK — 2 controls + %d cases + %d guards"
          % (len(cases), len(guards)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
