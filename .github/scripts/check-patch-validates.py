#!/usr/bin/env python3
"""CI guard: the CR patch the post-upgrade hook will SEND must satisfy values.schema.json.

WHY THIS EXISTS, and it is a defect I shipped. installer#1060 added an `accessRequestIntake` arm to
the gatewayPathsFrom derivation AND set `gatewayPathsFrom: accessRequestIntake` on the
identity-lifecycle pin, while values.schema.json still declared `enum: ["keycloakRealm"]`. Every one
of 47 gates passed, the chart rendered, lint passed, the chart tests passed, and the release was
TAGGED as 0.3.686. A colleague caught it in review (installer#1063) eleven minutes after the tag.

WHAT WOULD HAVE HAPPENED ON A ROLL, traced rather than guessed:

    inst.componentsPatchList strips ONLY `namespace` and `readerGroups`
      -> gatewayPathsFrom IS carried into components-patch.json
    the Installer CRD's structural schema is generated FROM values.schema.json
      -> VERIFIED on krateo-057's live CRD: components[].gatewayPathsFrom enum = ["keycloakRealm"]
    the post-upgrade hook runs, under `set -e` with restartPolicy: OnFailure:
      kubectl patch installers.<ver>.composition.krateo.io installer --type merge \
        --patch-file /cr/components-patch.json

AN ENUM VIOLATION IS A REJECTION, NOT PRUNING. Pruning is for UNDECLARED fields; a declared field
carrying an illegal value is refused. So the apiserver rejects the whole patch, `set -e` fails the
container, the Job retries forever on OnFailure, and `helm upgrade` blocks on the hook until it
times out and fails -- leaving the new CRD applied while the CR keeps the old component list.

WHY check-pin-fields.py COULD NOT CATCH IT. That guard asks "is every pin KEY declared in the
schema?" -- key names only, never values:

    bad = [... for k in c if k not in declared]

`gatewayPathsFrom` is a declared key, so it passed. Its docstring explains why key-checking matters
(`unknown field "spec.components[5].internalUrls"` -> CrashLoopBackOff) and an enum violation is the
SAME CLASS of failure through a different door. Asserting the whole key set was the previous lesson;
asserting the whole VALUE set is this one.

WHY THIS VALIDATES THE RENDERED PATCH AND NOT THE PINS FILE. Validating
chart/files/component-pins.yaml would have caught this particular bug, and the keycloak-provider-kog
session showed the better check: render the chart, take `installer-self-cr`'s OWN
components-patch.json, and put THAT through the schema. The rendered effective list is not the pins
file -- it carries schema defaults, per-cluster overrides and appended registerOnly blueprints that
never appear in the pins -- and it is the exact payload the apiserver will decode. Their
`precheck2pass.sh` caught 0.3.686 offline against a live CR; this is the same idea with no cluster,
so it can gate every PR rather than every roll.

NO `lookup`, DELIBERATELY. This renders with `helm template`, which disables lookup -- so the
composition objects are vacuous here, as check-platform-routes.py's header records. That does not
matter: installer-self-cr is a plain ConfigMap built from the pins file and .Values, and it renders
fully. A check that needed a cluster could not run on a PR, which is where this one has to fire.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

NAME = "check-patch-validates"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CHART = os.path.join(ROOT, "chart")
KUBE_VERSION = "1.36.0"  # the chart declares kubeVersion >= 1.36.0-0; helm defaults to 1.34


def _err(msg):
    print("::error::%s: %s" % (NAME, msg))


def render(chart_dir, extra=None):
    """helm template the chart, with CHART_VERSION substituted. Returns the manifest text."""
    # The chart declares kubeVersion >= 1.36.0-0 and helm's built-in default is 1.34, which fails
    # the render outright. Every other render gate here pins it the same way.
    cmd = ["helm", "template", "inst", chart_dir,
           "--kube-version", KUBE_VERSION,
           "--set", "bootstrap.coreProvider.enabled=true"]
    for k, v in (extra or {}).items():
        cmd += ["--set", "%s=%s" % (k, v)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout or "")[-900:]
    return r.stdout, None


def patch_from(manifest):
    """Pull components-patch.json out of the installer-self-cr ConfigMap."""
    for doc in yaml.safe_load_all(manifest):
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") != "ConfigMap":
            continue
        if (doc.get("metadata") or {}).get("name") != "installer-self-cr":
            continue
        raw = (doc.get("data") or {}).get("components-patch.json")
        if raw is None:
            return None, "the installer-self-cr ConfigMap carries no components-patch.json key"
        try:
            return json.loads(raw), None
        except ValueError as e:
            return None, "components-patch.json is not valid JSON: %s" % e
    return None, ("no ConfigMap named installer-self-cr in the render -- this guard validated "
                  "NOTHING. That is not a pass.")


def _is_explicit_null(root, path):
    """True when `path` inside `root` holds None -- an RFC 7386 delete, not a value.

    Walks the error's own absolute_path rather than guessing: a null one level up or down is a
    different fact and must still be reported.
    """
    node = root
    for p in path:
        if isinstance(node, dict):
            if p not in node:
                return False
            node = node[p]
        elif isinstance(node, list):
            if not isinstance(p, int) or p >= len(node):
                return False
            node = node[p]
        else:
            return False
    return node is None


def main():
    if shutil.which("helm") is None:
        _err("helm is not on PATH. A guard that skips when its tool is missing reports success "
             "for a thing it did not do.")
        return 1

    schema_path = os.path.join(CHART, "values.schema.json")
    try:
        schema = json.load(open(schema_path, encoding="utf-8"))
    except Exception as e:
        _err("could not read %s: %s" % (schema_path, e))
        return 1

    try:
        import jsonschema
    except ImportError:
        _err("the `jsonschema` package is required. Install it in the workflow "
             "(pip install jsonschema) -- this guard cannot validate without it, and skipping "
             "would report success for an unperformed check.")
        return 1

    # Render in a COPY so the CHART_VERSION substitution never touches the working tree. An earlier
    # backup-and-restore of Chart.yaml in this repo collided with a directory, the restore silently
    # failed, and the file was left at version: 0.0.0 -- caught only by `git status`.
    tmp = tempfile.mkdtemp(prefix="patchgate-")
    try:
        cdir = os.path.join(tmp, "chart")
        shutil.copytree(CHART, cdir)
        cy = os.path.join(cdir, "Chart.yaml")
        text = open(cy, encoding="utf-8").read()
        new = re.sub(r"^version: CHART_VERSION$", "version: 0.0.0", text, flags=re.M)
        if new == text and "version: CHART_VERSION" in text:
            _err("could not substitute the CHART_VERSION placeholder in Chart.yaml")
            return 1
        open(cy, "w", encoding="utf-8").write(new)

        subprocess.run(["helm", "dependency", "build", cdir],
                       capture_output=True, text=True)

        manifest, rerr = render(cdir)
        if manifest is None:
            _err("the chart did not render, so the patch could not be checked: %s" % rerr)
            return 1

        patch, perr = patch_from(manifest)
        if patch is None:
            _err(perr)
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    spec = patch.get("spec") or {}
    findings = []
    checked = 0

    # Each top-level key of the patch is validated against ITS OWN subschema in values.schema.json,
    # because that file is what crdgen turns into the Installer CRD's structural schema.
    for key in sorted(spec):
        sub = (schema.get("properties") or {}).get(key)
        if sub is None:
            findings.append((key, "(root)",
                             "the patch sends spec.%s, which values.schema.json does not declare "
                             "at all -- the apiserver will reject the whole patch with an unknown "
                             "field error" % key))
            continue
        checked += 1
        v = jsonschema.Draft7Validator(sub)
        for e in v.iter_errors(spec[key]):
            path = "/".join(str(p) for p in e.absolute_path) or "(root)"
            # AN EXPLICIT NULL IS A DELETION INSTRUCTION, NOT A VALUE. components-patch.json is
            # applied as an RFC 7386 JSON Merge Patch, where null at a member means REMOVE THAT
            # MEMBER -- which is the entire mechanism componentValuesUnset exists for, and what
            # values.schema.json's own description of that key says ("only an explicit null
            # deletes"). The apiserver applies the merge and validates the RESULT, so a null never
            # reaches the type check.
            #
            # MEASURED 2026-11-22 on krateo-057, because this gate asserted the opposite and I
            # would not change it on a reading of the RFC:
            #   kubectl patch installers.../installer --type=merge --dry-run=server \
            #     -p '{"spec":{"componentValues":{"agentgateway-policies":{"llm":{"retry":null}}}}}'
            #   -> ACCEPTED, exit 0, and the returned object's `llm` had NO `retry` member.
            #      resourceVersion 1795296584012079016 and generation 581 identical before and
            #      after; the live value was untouched, so nothing persisted.
            #
            # WHY IT WENT UNNOTICED: this gate (#1065) was added AFTER componentValuesUnset (#712),
            # and ZERO tags have ever shipped a non-empty componentValuesUnset -- so the gate
            # forbade the mechanism outright and nothing exercised the contradiction. Before this
            # exemption it refused values.schema.json's OWN documented example,
            # authn.env.AUTHN_DUMP_ENV, identically.
            #
            # SCOPE OF THE EXEMPTION, deliberately narrow: only a `None` AT THE ERROR'S OWN PATH.
            # A null nested inside an otherwise-illegal value is still reported, and every
            # non-null violation is untouched -- those genuinely do break the whole patch.
            if _is_explicit_null(spec[key], e.absolute_path):
                continue
            findings.append((key, path, e.message))

    # THE INSTRUMENT MUST HAVE FOUND SOMETHING TO CHECK. A patch with no recognised keys, or a
    # components list of zero, is a render that went wrong rather than a clean bill of health.
    comps = spec.get("components")
    if not checked:
        _err("the patch carried no key this schema declares, so nothing was validated. "
             "That is not a pass.")
        return 1
    if not isinstance(comps, list) or not comps:
        _err("the patch's spec.components is %r -- expected a non-empty list. The render produced "
             "a patch with no components, so this guard verified nothing about them."
             % (type(comps).__name__ if comps is not None else None))
        return 1

    if findings:
        _err("the CR patch the post-upgrade hook will SEND violates values.schema.json, which is "
             "the schema the Installer CRD is generated from. The apiserver rejects the WHOLE "
             "patch on any one of these, `set -e` fails the hook, and `helm upgrade` blocks on it "
             "until timeout:")
        for key, path, msg in findings[:25]:
            where = "spec.%s" % key if path == "(root)" else "spec.%s/%s" % (key, path)
            print("    %-46s %s" % (where, msg))
        if len(findings) > 25:
            print("    ... and %d more" % (len(findings) - 25))
        print("  A value here is NOT pruned -- pruning is for undeclared fields. A declared field "
              "carrying an illegal value is REFUSED, so one bad entry breaks the entire roll.")
        return 1

    print("%s: OK -- the rendered components-patch.json satisfies values.schema.json "
          "(%d top-level key(s): %s; %d component entries)"
          % (NAME, checked, ", ".join(sorted(k for k in spec if k in (schema.get("properties") or {}))),
             len(comps)))
    return 0


def self_test():
    """Exercise _is_explicit_null's NARROWNESS, which is the whole risk in the exemption.

    A too-broad version would swallow real violations and this gate's purpose with them. These
    assert that ONLY a None at the error's own path is exempt -- not its parent, not a falsy value,
    not an absent path, and not a container that merely holds a null somewhere inside.
    """
    cases = [
        ({"a": {"b": None}},   ["a", "b"], True,  "null AT the error's own path -> exempt"),
        ({"a": {"b": None}},   ["a"],      False, "the PARENT of a null is not itself null"),
        ({"a": {"b": 1}},      ["a", "b"], False, "a real value is reported"),
        ({"a": {"b": ""}},     ["a", "b"], False, "empty string is NOT null"),
        ({"a": {"b": 0}},      ["a", "b"], False, "zero is NOT null"),
        ({"a": {"b": False}},  ["a", "b"], False, "false is NOT null"),
        ({"a": {"b": {}}},     ["a", "b"], False, "empty object is NOT null"),
        ({"a": {}},            ["a", "b"], False, "an ABSENT path is reported, not assumed null"),
        ({"a": [None]},        ["a", 0],   True,  "null at its own list index -> exempt"),
        ({"a": [{"b": None}]}, ["a", 0],   False, "a list item CONTAINING a null is not null"),
        ({"a": [1]},           ["a", "0"], False, "a string index into a list: reported, no crash"),
        ({"a": 1},             ["a", "b"], False, "descending into a scalar: reported, no crash"),
    ]
    bad = 0
    for root, path, want, label in cases:
        got = _is_explicit_null(root, path)
        ok = got is want
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
    print("self-test: %d case(s), %d failure(s)" % (len(cases), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
