#!/usr/bin/env python3
"""CI guard: run the chart's helm-unittest suites, which NOTHING RAN until 2026-11-14.

WHY THIS EXISTS. chart/tests/httproutes_test.yaml has been in the repo since the Gateway work and
no workflow, Makefile target or gate script ever executed it. A test nobody runs is worse than no
test: it reads as coverage. Confirmed by grepping .github/, Makefile and hack/ for `helm unittest`,
`chart/tests` and the suite's own filename -- the only hit was check-ready-gate.py, which builds its
OWN probe chart in a temp dir and never touches chart/tests/.

WHY helm-unittest AND NOT `helm template`. compositions.yaml LISTs every CRD with `lookup` at its
head and threads the slice into the gating helpers. `helm template` DISABLES lookup, so that file
renders ZERO composition objects and no plain render can exercise anything in it. helm-unittest's
kubernetesProvider serves both the LIST and the per-dep by-name lookups, so it is the only way to
assert what those templates actually produce.

WHAT IT CAUGHT IMMEDIATELY. The snowplow seedAuthn derivation it was written for was INERT: the
block had been inserted inside `{{- if $top.Values.features.agentGateway -}}`, a region it has
nothing to do with, so it never fired on a normal install. All 31 other gates passed with it inert,
including check-fill-defaults, which reads compositions.yaml as TEXT and therefore saw the call and
approved it. Only rendering it found the bug.

FAILS, NEVER SKIPS, on a missing plugin -- the check-ready-gate precedent. A guard that skips when
its tool is absent reports success for a thing it did not do.

THE CHART_VERSION PLACEHOLDER. chart/Chart.yaml ships `version: CHART_VERSION`, substituted at
release time, and helm refuses it: `validation: chart.metadata.version "CHART_VERSION" is invalid`.
So the chart is COPIED to a temp dir and the placeholder substituted there. The real Chart.yaml is
never modified -- an earlier draft of this script edited it in place, which would have left the
working tree dirty on a failure and is exactly the kind of thing that gets committed by accident.
"""
import glob
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

CHART = pathlib.Path("chart")
MIN_SUITES = 2  # httproutes + snowplow seedAuthn. Raise when suites are added; never lower.


def main() -> int:
    if not (CHART / "Chart.yaml").is_file():
        print("::error::chart/Chart.yaml not found -- run from the repo root")
        return 1

    suites = sorted(glob.glob("chart/tests/*_test.yaml"))
    print("  suites found: %d" % len(suites))
    for s in suites:
        print("    %s" % s)
    if len(suites) < MIN_SUITES:
        print("::error::expected at least %d suite(s) in chart/tests/, found %d. Either a suite was "
              "deleted or this glob went stale -- a runner that finds nothing must FAIL, because "
              "'0 suites, 0 failures' is indistinguishable from success."
              % (MIN_SUITES, len(suites)))
        return 1

    have = subprocess.run(["helm", "plugin", "list"], capture_output=True, text=True)
    if "unittest" not in (have.stdout or ""):
        print("::error::helm-unittest is not installed, so these suites CANNOT run. FAILING rather "
              "than skipping: compositions.yaml is lookup-gated and this plugin is the only thing "
              "that can render it. Install: helm plugin install "
              "https://github.com/helm-unittest/helm-unittest --version 1.2.1")
        return 1

    work = pathlib.Path(tempfile.mkdtemp(prefix="chart-unittests-"))
    try:
        dest = work / "chart"
        shutil.copytree(CHART, dest)
        cy = dest / "Chart.yaml"
        text = cy.read_text()
        new = re.sub(r"^version: CHART_VERSION$", "version: 0.0.0", text, flags=re.M)
        if new == text and "version: CHART_VERSION" in text:
            print("::error::could not substitute the CHART_VERSION placeholder")
            return 1
        cy.write_text(new)

        r = subprocess.run(["helm", "unittest", "chart"], cwd=str(work),
                           capture_output=True, text=True)
        out = (r.stdout or "") + (r.returncode and (r.stderr or "") or "")
        for line in out.strip().split("\n"):
            print("  %s" % line)
        # Non-vacuity: the runner must report having executed tests, not merely exit 0.
        # helm-unittest prints TWO shapes and the failure one has an extra field:
        #     Tests:       3 passed, 3 total
        #     Tests:       2 failed, 1 passed, 3 total
        # The first draft of this regex matched only the passing shape, so on a real failure it
        # reported "could not read a test count" instead of which tests failed -- it still exited
        # 1, but for a reason that was not true. Caught by the mutation control below, which is the
        # only run that ever produces the second shape.
        m = re.search(r"Tests:\s+(?:(\d+) failed, )?(\d+) passed, (\d+) total", out)
        if not m:
            print("::error::could not read a test count from helm-unittest's output -- refusing to "
                  "report success for a run whose result cannot be parsed")
            return 1
        failed = int(m.group(1) or 0)
        passed, total = int(m.group(2)), int(m.group(3))
        if failed:
            print("::error::chart unittests: %d of %d test(s) FAILED (see the suite output above). "
                  "compositions.yaml is lookup-gated, so a failure here is the only signal that a "
                  "template change did not do what it says." % (failed, total))
            return 1
        if total == 0:
            print("::error::helm-unittest executed 0 tests. Exit status alone would have read as a "
                  "pass.")
            return 1
        if r.returncode != 0 or passed != total:
            print("::error::chart unittests: %d of %d passed" % (passed, total))
            return 1
        print("\ncheck-chart-unittests: OK -- %d test(s) in %d suite(s), all passed"
              % (total, len(suites)))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
