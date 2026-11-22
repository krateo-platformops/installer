#!/usr/bin/env python3
"""Refuse a workflow file GitHub Actions would reject before it runs a single job.

WHY THIS EXISTS. On 2026-11-18 #1060 added a second `run:` key to an EXISTING step of
check-platform-routes.yaml:

    - name: Prove Keycloak's path-mode exposure, and that the admin console is NOT routed
      run: python3 .github/scripts/check-keycloak-routes.py
      run: python3 .github/scripts/check-intake-route.py      # <- the collision

GitHub refuses a duplicate mapping key, so the workflow failed at STARTUP: conclusion `failure`,
`total_count: 0` jobs, `run_started_at == updated_at` to the same second, no logs, no annotations
and no check-run on the commit. Three gates went dark for three days -- check-platform-routes.py
(234 lines), check-keycloak-routes.py (386) and check-intake-route.py (200) -- and every PR in that
window showed a red tick whose cause was invisible in the usual places.

WHY IT WAS NOT CAUGHT BY READING. PyYAML's `safe_load` SILENTLY KEEPS THE LAST duplicate. So the
file parses, reports a plausible 5 steps, and the swallowed step simply is not there. Any check
built on `yaml.safe_load` -- including every other script in this directory -- agrees with the
broken file and disagrees with GitHub. That is the whole hazard: the permissive parser makes the
file look fine exactly where the strict one refuses it.

WHAT THIS ASSERTS, per workflow file:
  1. it loads under a loader that REFUSES duplicate keys, at any depth;
  2. it has at least one job, and every job has at least one step;
  3. every step has EXACTLY ONE of `run` / `uses` -- never both, never neither.

NON-VACUITY. A scan that finds no files, or whose loader silently accepts a duplicate, would print
the same "OK" as a clean repo. So this refuses to pass when it saw fewer than MIN_WORKFLOWS files,
and `--self-test` plants a duplicate key and a two-verb step in memory and FAILS if they are not
both caught. Run the self-test in CI alongside the scan, so the detector is exercised on every run
rather than trusted.

NOT IN SCOPE: whether a step's command is correct, whether an action's SHA is pinned (that is
check-pinned-uses), or whether the workflow's triggers are sensible. This answers one question
only -- will GitHub start this workflow at all.
"""

import glob
import io
import os
import sys

import yaml

MIN_WORKFLOWS = 40


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys, which GitHub also refuses."""


def _no_duplicate_keys(loader, node, deep=False):
    seen = {}
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None,
                "duplicate key %r (first seen on line %d, repeated on line %d). GitHub Actions "
                "refuses this and the workflow fails at STARTUP with zero jobs and no logs."
                % (key, seen[key], key_node.start_mark.line + 1),
                key_node.start_mark)
        seen[key] = key_node.start_mark.line + 1
        out[key] = loader.construct_object(value_node, deep=deep)
    return out


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def check_one(path, text):
    """[] when the file is fine, else a list of human-readable problems."""
    problems = []
    try:
        doc = yaml.load(text, Loader=StrictLoader)
    except yaml.constructor.ConstructorError as exc:
        return ["%s" % (exc.problem or exc)]
    except yaml.YAMLError as exc:
        return ["not valid YAML: %s" % exc]

    if not isinstance(doc, dict):
        return ["parsed as %s, not a mapping" % type(doc).__name__]

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return ["has no `jobs:` mapping"]

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            problems.append("job %s is not a mapping" % job_name)
            continue
        if "uses" in job:
            continue  # a reusable-workflow call has no steps of its own
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append("job %s has no steps" % job_name)
            continue
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                problems.append("job %s step %d is not a mapping" % (job_name, i + 1))
                continue
            verbs = [v for v in ("run", "uses") if v in step]
            label = step.get("name") or ("step %d" % (i + 1))
            if len(verbs) != 1:
                problems.append(
                    "job %s / %r has %s -- a step needs exactly one of run/uses"
                    % (job_name, label, verbs or "neither run nor uses"))
    return problems


def self_test():
    """Prove the detector detects. Returns 0 when both planted faults are caught."""
    dup = (
        "name: planted\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: two run keys\n"
        "        run: echo first\n"
        "        run: echo second\n"
    )
    both = (
        "name: planted\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: run and uses\n"
        "        run: echo hi\n"
        "        uses: actions/checkout@v5\n"
    )
    neither = (
        "name: planted\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: no verb at all\n"
        "        with:\n"
        "          k: v\n"
    )
    clean = (
        "name: planted\n"
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: fine\n"
        "        run: echo ok\n"
    )

    cases = [
        ("duplicate `run:` key", dup, True),
        ("step with both run and uses", both, True),
        ("step with neither run nor uses", neither, True),
        ("a clean workflow", clean, False),
    ]
    failures = 0
    for label, text, should_fail in cases:
        problems = check_one("<planted>", text)
        caught = bool(problems)
        ok = caught == should_fail
        print("  %-36s %s%s" % (
            label,
            "CAUGHT" if caught else "passed",
            "" if ok else "   <- WRONG, expected %s" % ("CAUGHT" if should_fail else "passed")))
        if problems and should_fail:
            print("      %s" % problems[0][:150])
        if not ok:
            failures += 1

    # And prove the permissive parser disagrees, which is the reason this script exists.
    permissive = yaml.safe_load(dup)
    swallowed = permissive["jobs"]["j"]["steps"][0]
    print("  yaml.safe_load on the duplicate: keeps %r and loses the other -- "
          "which is why reading the file does not catch this"
          % swallowed.get("run"))

    if failures:
        print("::error::check-workflow-shape --self-test: %d case(s) behaved wrongly. The "
              "detector is not trustworthy, so a clean scan means nothing." % failures)
        return 1
    print("check-workflow-shape --self-test: OK -- the detector catches all three fault shapes "
          "and passes a clean file.")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()

    paths = sorted(glob.glob(".github/workflows/*.yaml") + glob.glob(".github/workflows/*.yml"))
    if len(paths) < MIN_WORKFLOWS:
        print("::error::check-workflow-shape: scanned only %d workflow file(s), expected at least "
              "%d. Either the glob is wrong or this is not the installer repo -- FAILING CLOSED, "
              "because a scan that sees nothing prints the same OK as a clean repo."
              % (len(paths), MIN_WORKFLOWS))
        return 2

    bad = 0
    for path in paths:
        text = io.open(path, encoding="utf-8").read()
        problems = check_one(path, text)
        if problems:
            bad += 1
            for p in problems:
                print("::error file=%s::%s: %s" % (path, os.path.basename(path), p))

    if bad:
        print("::error::check-workflow-shape: %d of %d workflow file(s) would be REFUSED by "
              "GitHub Actions. Such a workflow fails at startup with zero jobs and no logs, so "
              "its gates are silently dark while its tick is red."
              % (bad, len(paths)))
        return 1

    print("check-workflow-shape: OK -- %d workflow file(s), no duplicate keys, every job has "
          "steps, every step has exactly one of run/uses." % len(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
