#!/usr/bin/env python3
"""Exercises inst.jsonSchemaConfigmaps — the names a blueprint's readers may `get`.

WHY THE GRANT EXISTS. A tenant's blueprint create form is EMPTY without it: the form's schema source
is core-provider's `<resource>-<version>-jsonschema-configmap`, and tenants cannot read the CRD
fallback by design, so a tenant saw "This blueprint is still being registered". Diego chose to have
core-provider write that ConfigMap next to its CompositionDefinition; this grant lets the group read
it once it is there.

WHY A PROBE AND NOT A RENDER. definitions.yaml needs a live CRD LIST -- `lookup` -- to know a kind's
plural and its served versions, and `helm template` disables lookup. So a render-based check can only
ever observe the empty case. The construction was extracted into a helper, as inst.platformRoutes and
inst.kogUrls were, and this probe feeds it synthetic CRDs.

WHAT IT PROVES:
  1. the plural comes from the CRD, not from a pluralisation rule -- `selfservicekrateoes` is what
     crdgen produces from SelfserviceKrateo and no rule reproduces it;
  2. EVERY SERVED version is named, plus the PINNED one, deduped -- which removes the bump window:
     the outgoing ConfigMap stays readable until its CRD version stops being served, and the incoming
     one is readable as soon as it exists;
  3. the version form is the CRD's (v0-2-61), never the chart's (0.2.61);
  4. an EMPTY CRD list yields NOTHING, so the caller omits the rule rather than emitting a guessed
     name -- a guessed resourceNames entry is a grant that silently matches nothing.

Usage: check-jsonschema-cm-grant.py [CHART_DIR]   (default: ../../chart relative to this file)
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required by check-jsonschema-cm-grant.py")

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "chart")

PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  names: |
    {{ include "inst.jsonSchemaConfigmaps" (list .Values.kind .Values.ver .Values.crds) }}
"""


def crd(kind, plural, versions):
    return {"spec": {"group": "composition.krateo.io",
                     "names": {"kind": kind, "plural": plural},
                     "versions": [{"name": v, "served": s} for v, s in versions]}}


CASES = [
    ("one served version, equal to the pin",
     "SelfserviceKrateo", "0.2.61", [crd("SelfserviceKrateo", "selfservicekrateoes", [("v0-2-61", True)])],
     ["selfservicekrateoes-v0-2-61-jsonschema-configmap"]),
    ("MID-BUMP: old served, pin ahead -> BOTH named, no window",
     "SelfserviceKrateo", "0.2.62", [crd("SelfserviceKrateo", "selfservicekrateoes", [("v0-2-61", True)])],
     ["selfservicekrateoes-v0-2-61-jsonschema-configmap",
      "selfservicekrateoes-v0-2-62-jsonschema-configmap"]),
    ("two served versions plus the pin",
     "SelfserviceKrateo", "0.2.63", [crd("SelfserviceKrateo", "selfservicekrateoes",
                                         [("v0-2-61", True), ("v0-2-62", True)])],
     ["selfservicekrateoes-v0-2-61-jsonschema-configmap",
      "selfservicekrateoes-v0-2-62-jsonschema-configmap",
      "selfservicekrateoes-v0-2-63-jsonschema-configmap"]),
    ("an UNSERVED version is excluded",
     "SelfserviceKrateo", "0.2.62", [crd("SelfserviceKrateo", "selfservicekrateoes",
                                         [("v0-2-60", False), ("v0-2-61", True)])],
     ["selfservicekrateoes-v0-2-61-jsonschema-configmap",
      "selfservicekrateoes-v0-2-62-jsonschema-configmap"]),
    ("the IRREGULAR plural comes from the CRD, not a rule",
     "Portal", "1.8.129", [crd("Portal", "portals", [("v1-8-129", True)])],
     ["portals-v1-8-129-jsonschema-configmap"]),
    ("EMPTY CRD list (helm template) -> NOTHING, so the rule is omitted",
     "SelfserviceKrateo", "0.2.61", [], []),
    ("a CRD for ANOTHER kind is ignored",
     "SelfserviceKrateo", "0.2.61", [crd("SomethingElse", "somethingelses", [("v9-9-9", True)])], []),
]


def main():
    ok = True

    def check(good, msg):
        nonlocal ok
        print("%s  %s" % ("ok  " if good else "FAIL", msg))
        if not good:
            ok = False

    td = tempfile.mkdtemp(prefix="jscm-")
    p = os.path.join(td, "probe")
    try:
        os.makedirs(os.path.join(p, "templates"))
        with open(os.path.join(p, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: probe\nversion: 0.0.1\n")
        with open(os.path.join(p, "values.yaml"), "w") as f:
            f.write("kind: X\nver: 0.0.0\ncrds: []\n")
        shutil.copy(os.path.join(CHART, "templates", "_helpers.tpl"),
                    os.path.join(p, "templates", "_helpers.tpl"))
        with open(os.path.join(p, "templates", "pr.yaml"), "w") as f:
            f.write(PROBE)

        for label, kind, ver, crds, want in CASES:
            with tempfile.TemporaryDirectory() as vt:
                vf = os.path.join(vt, "v.yaml")
                with open(vf, "w") as f:
                    yaml.safe_dump({"kind": kind, "ver": ver, "crds": crds}, f)
                r = subprocess.run(["helm", "template", "probe", p, "-f", vf],
                                   capture_output=True, text=True)
            if r.returncode != 0:
                check(False, "%s: render failed: %s"
                      % (label, (r.stderr or "").strip().replace("\n", " ")[:160]))
                continue
            got = []
            for d in yaml.safe_load_all(r.stdout):
                if isinstance(d, dict) and d.get("kind") == "ConfigMap":
                    got = [x for x in ((d.get("data") or {}).get("names") or "").split() if x]
            check(got == want, "%s -> %s" % (label, got or "(none)"))
            # the version form must never be the chart's dotted one
            if any("." in n.rsplit("-jsonschema", 1)[0] for n in got):
                check(False, "%s: a name carries a DOTTED version, which would never match" % label)

        # NEGATIVE CONTROL: the probe must be able to fail. A wrong expectation must not pass.
        with tempfile.TemporaryDirectory() as vt:
            vf = os.path.join(vt, "v.yaml")
            with open(vf, "w") as f:
                yaml.safe_dump({"kind": "SelfserviceKrateo", "ver": "0.2.61",
                                "crds": [crd("SelfserviceKrateo", "selfservicekrateoes",
                                             [("v0-2-61", True)])]}, f)
            r = subprocess.run(["helm", "template", "probe", p, "-f", vf],
                               capture_output=True, text=True)
        got = []
        for d in yaml.safe_load_all(r.stdout):
            if isinstance(d, dict) and d.get("kind") == "ConfigMap":
                got = [x for x in ((d.get("data") or {}).get("names") or "").split() if x]
        check(got != ["selfservicekrateoes-0.2.61-jsonschema-configmap"],
              "NEGATIVE CONTROL: the dotted-version name must NOT be produced (got %s)" % got)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    if not ok:
        print("::error::check-jsonschema-cm-grant: the ConfigMap names are NOT what this gate requires")
        return 1
    print("\ncheck-jsonschema-cm-grant: OK — %d case(s) plus a negative control; the plural comes from "
          "the CRD, every served version is named alongside the pinned one, and an empty CRD list "
          "yields nothing" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
