#!/usr/bin/env python3
"""Exercise hack/rd-immutable-gate.py against fixtures, with no cluster.

WHY THIS GATE NEEDS A TEST MORE THAN MOST. It exists because three successive pre-roll checks
were each written for the field that bit last time, so the thing most likely to go wrong with
it is that it silently guards less than it claims. Two blindness modes are specifically
asserted here:

  * ZERO PAIRED NAMES must WITHHOLD, not pass. The real comparator was vacuous on its first
    run: the chart was rendered with release name `kog-probe` while the cluster carried
    `github-provider-kog-*`, nothing paired, and it reported "0 immutable-field changes" with
    complete confidence. An empty comparison reporting a pass is the failure this suite exists
    to make impossible.
  * A RULE-COUNT CHANGE must WITHHOLD. The field list is derived from the live CRD, so an
    oasgen release adding an eighth immutable field would otherwise be silently unguarded.

It subprocesses the real script rather than reimplementing its rules.

Each case asserts the exit code AND a distinguishing substring, because exit 2 covers four
different withholdings (rule-count mismatch, zero paired names, no RD owned by the release, a
stale --allow) and exit code alone cannot tell them apart.
"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "hack", "rd-immutable-gate.py")

STRICT = ["spec.resource.kind", "spec.resource.identifiers",
          "spec.resource.configurationFields", "spec.resource.excludedSpecFields"]
APPEND = ["spec.resource.additionalStatusFields", "spec.resource.includedSpecFields"]


def crd(strict=STRICT, append=APPEND, group_rule=True):
    """A RestDefinition CRD carrying the given CEL transition rules, nested as the real one is."""
    props = {}
    for path in strict:
        props[path.split(".")[-1]] = {
            "x-kubernetes-validations": [{"rule": "self == oldSelf"}],
            # A description mentioning oldSelf, to prove a grep would over-count.
            "description": "oldSelf is used in the transition rule; see oldSelf docs",
        }
    for path in append:
        props[path.split(".")[-1]] = {"x-kubernetes-validations": [
            {"rule": "size(self) >= size(oldSelf) && oldSelf.all(i, v, self[i] == v)"}]}
    spec_props = {"resource": {"properties": props}}
    if group_rule:
        spec_props["resourceGroup"] = {
            "x-kubernetes-validations": [{"rule": "self == oldSelf"}]}
    doc = {"spec": {"versions": [{"schema": {"openAPIV3Schema": {
        "properties": {"spec": {"properties": spec_props}}}}}]}}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return fh.name


def live(entries):
    """entries: list of (name, release, resource-dict)."""
    items = [{"metadata": {"name": n, "namespace": "krateo-system",
                           "annotations": {"meta.helm.sh/release-name": rel} if rel else {}},
              "spec": {"resource": res}} for n, rel, res in entries]
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"items": items}, fh)
    fh.close()
    return fh.name


def rendered(entries):
    """entries: list of (name, resource-dict)."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    for name, res in entries:
        fh.write("---\napiVersion: ogen.krateo.io/v1alpha1\nkind: RestDefinition\n")
        fh.write(f"metadata:\n  name: {name}\nspec:\n  resource:\n")
        for key, value in res.items():
            fh.write(f"    {key}:\n")
            for v in value:
                fh.write(f"      - {v}\n")
    fh.close()
    return fh.name


REL = "github-provider-kog"
BASE = {"identifiers": ["number"], "additionalStatusFields": ["a", "b"]}

CASES = [
    ("steady", crd(), live([("pr", REL, BASE)]), rendered([("pr", BASE)]), [REL], 0,
     "no immutable-field change"),
    ("strict field changed", crd(), live([("pr", REL, BASE)]),
     rendered([("pr", {"identifiers": ["head", "base"], "additionalStatusFields": ["a", "b"]})]),
     [REL], 3, "spec.resource.identifiers"),
    ("append at end is legal", crd(), live([("pr", REL, BASE)]),
     rendered([("pr", {"identifiers": ["number"], "additionalStatusFields": ["a", "b", "c"]})]),
     [REL], 0, "no immutable-field change"),
    ("reorder, same set, is refused", crd(), live([("pr", REL, BASE)]),
     rendered([("pr", {"identifiers": ["number"], "additionalStatusFields": ["b", "a"]})]),
     [REL], 3, "additionalStatusFields"),
    ("entry removed is refused", crd(), live([("pr", REL, BASE)]),
     rendered([("pr", {"identifiers": ["number"], "additionalStatusFields": ["a"]})]),
     [REL], 3, "additionalStatusFields"),
    ("zero paired names WITHHOLDS", crd(), live([("pr", REL, BASE)]),
     rendered([("probe-pr", BASE)]), [REL], 2, "zero names paired"),
    ("no RD owned by the release WITHHOLDS", crd(), live([("pr", "other-release", BASE)]),
     rendered([("pr", BASE)]), [REL], 2, "no live RestDefinition carries"),
    ("a rule-count change WITHHOLDS", crd(strict=STRICT[:2], group_rule=False),
     live([("pr", REL, BASE)]), rendered([("pr", BASE)]), [REL], 2,
     "immutability rules, expected"),
    ("a stale --allow WITHHOLDS", crd(), live([("pr", REL, BASE)]), rendered([("pr", BASE)]),
     [REL, "--allow", "pr:spec.resource.identifiers"], 2, "NOT observed"),
    ("a declared change passes", crd(), live([("pr", REL, BASE)]),
     rendered([("pr", {"identifiers": ["head", "base"], "additionalStatusFields": ["a", "b"]})]),
     [REL, "--allow", "pr:spec.resource.identifiers"], 0, "no immutable-field change"),
]


def main():
    failures = []
    for name, crd_path, live_path, rendered_path, extra, want_rc, want_sub in CASES:
        argv = [sys.executable, SCRIPT, "--crd", crd_path, "--live", live_path,
                "--rendered", rendered_path, "--release", extra[0]] + extra[1:]
        p = subprocess.run(argv, capture_output=True, text=True)
        out = p.stdout + p.stderr
        if p.returncode != want_rc:
            failures.append(f"{name}: exit {p.returncode}, wanted {want_rc}\n{out}")
        elif want_sub not in out:
            failures.append(f"{name}: exit {want_rc} as wanted, but {want_sub!r} "
                            f"not in output — right code for possibly the wrong reason\n{out}")

    # The self-test arm must itself pass, since the gate's own claim rests on it.
    p = subprocess.run([sys.executable, SCRIPT, "--crd", crd(), "--self-test"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        failures.append(f"--self-test did not pass:\n{p.stdout}{p.stderr}")

    # The case list must reach every exit code the script can return, or a path is untested.
    reached = {c[5] for c in CASES}
    for rc in (0, 2, 3):
        if rc not in reached:
            failures.append(f"no case exercises exit {rc} — that path is untested")

    if failures:
        print("check-rd-immutable-gate: FAILED")
        for f in failures:
            print(f"\n  {f}")
        return 1
    print(f"check-rd-immutable-gate: OK — all {len(CASES)} cases behave as required, "
          f"exit codes reached: {sorted(reached)}, self-test green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
