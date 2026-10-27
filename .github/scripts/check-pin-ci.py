#!/usr/bin/env python3
"""CI guard: a pin bump must point at a commit whose OWN repo CI was green.

WHY THIS EXISTS. On 2026-10-27 this repo pinned frontend 1.8.12 and rolled it to
krateo-057. That tag is commit 191e7f65, whose `test` job on main was FAILURE, with 52
tests silently not collecting. Nothing objected, because:

  - the pin PR's checks are about the CHART (pin-kinds, pin-fields, feature-keys,
    fill-defaults, no-unknowns) and the reviewer verified chart contents by unpacking
    both artifacts and diffing them -- all correct, and all about the chart;
  - the component's release workflows (release-oci / release-notes / release-tag) are
    TAG-TRIGGERED and independent of `test`, so a tag cuts cleanly over a red main.

A green release workflow is not a green repo. Every check in the path was about the
chart; none was about the code.

HOW IT RESOLVES THE REPO, without a hardcoded map. A published chart names its own
source repositories in its `sources` annotation, and its `appVersion` is the app repo's
tag. So for each changed pin:

    chart version -> helm show chart -> appVersion + sources[]
    for each source repo that HAS a tag == appVersion:
        tag -> commit sha -> that repo's test workflow conclusion at that sha

TWO OUTCOMES ARE FAILURES, and the second is the worse one:

    conclusion == failure   the code was red at that commit
    no run exists           NOTHING EVER TESTED that commit, which reads
                            identically to "nothing was wrong"

NOT-FOUND IS NOT A PASS. If no source repo carries a tag matching appVersion, this
reports UNRESOLVED and fails, rather than silently approving. A guard that cannot find
its subject must say so -- an unresolvable pin is exactly where an untested commit hides.
"""
import json
import os
import re
import subprocess
import sys

import yaml



def sh(*args, check=False):
    p = subprocess.run(args, capture_output=True, text=True)
    if check and p.returncode != 0:
        print(f"::error::command failed: {' '.join(args)}\n{p.stderr.strip()[:500]}")
        sys.exit(1)
    return p.stdout.strip(), p.returncode


def changed_pins(base_ref):
    """Pins whose `version:` differs from the base ref. Reads both files that carry pins."""
    out = []
    for path, loader in (("chart/files/component-pins.yaml", "pins"),
                         ("chart/Chart.yaml", "deps")):
        before, rc = sh("git", "show", f"{base_ref}:{path}")
        if rc != 0:
            continue
        try:
            after = open(path).read()
        except FileNotFoundError:
            continue
        b = yaml.safe_load(before) or {}
        a = yaml.safe_load(after) or {}
        if loader == "pins":
            bm = {c["name"]: c for c in (b.get("components") or []) if isinstance(c, dict)}
            am = {c["name"]: c for c in (a.get("components") or []) if isinstance(c, dict)}
        else:
            bm = {d["name"]: d for d in (b.get("dependencies") or [])}
            am = {d["name"]: d for d in (a.get("dependencies") or [])}
        for name, entry in am.items():
            old = (bm.get(name) or {}).get("version")
            new = entry.get("version")
            if new and old != new:
                out.append({
                    "name": name,
                    "chart": entry.get("chart", name),
                    "version": new,
                    "previous": old,
                    "repo": entry.get("repository") or entry.get("repo"),
                    "source_file": path,
                })
    return out


def chart_metadata(oci_repo, chart, version):
    txt, rc = sh("helm", "show", "chart", f"{oci_repo}/{chart}", "--version", version)
    if rc != 0:
        return None
    try:
        return yaml.safe_load(txt) or {}
    except yaml.YAMLError:
        return None


def repo_slug(url):
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", url or "")
    return m.group(1) if m else None


def tag_sha(slug, tag):
    out, rc = sh("gh", "api", f"repos/{slug}/git/ref/tags/{tag}",
                 "--jq", '.object.sha + " " + .object.type')
    if rc != 0:
        return None
    sha, _, kind = out.partition(" ")
    if kind.strip() == "tag":  # annotated: dereference to the commit
        out2, rc2 = sh("gh", "api", f"repos/{slug}/git/tags/{sha}", "--jq", ".object.sha")
        return out2 or None if rc2 == 0 else None
    return sha or None


def ci_at(slug, sha):
    """(verdict, detail). verdict in {green, red, norun, running}.

    Uses the commit's CHECK-RUNS, not a guessed workflow filename. Guessing was the first
    implementation and it was silently blind: core-provider has no `test.yaml` at all --
    its tests run as `test-core-provider`, `test-chart-inspector` and
    `test-composition-dynamic-controller` -- so a filename lookup returned "unknown" and
    PASSED. A guard that cannot find the tests reads exactly like a guard that found them
    green, which is the failure mode this guard exists to prevent one layer up.

    Any failed check-run is a failure, including lint or image verification: if a
    component's own CI is red for any reason, that is not a commit to pin.
    """
    out, rc = sh("gh", "api", f"repos/{slug}/commits/{sha}/check-runs?per_page=100",
                 "--jq", '[.check_runs[] | {n:.name, c:.conclusion, s:.status}]')
    if rc != 0:
        return "norun", f"could not read check-runs for {sha[:8]}"
    try:
        runs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return "norun", f"unparseable check-runs for {sha[:8]}"
    if not runs:
        return "norun", f"NO check-run of any kind for {sha[:8]}"
    bad = sorted({r["n"] for r in runs if r.get("c") in ("failure", "timed_out", "cancelled", "action_required")})
    pending = sorted({r["n"] for r in runs if r.get("s") != "completed"})
    if bad:
        return "red", f"{len(runs)} check-runs at {sha[:8]}, FAILED: {bad[:4]}"
    if pending:
        return "running", f"{len(runs)} check-runs at {sha[:8]}, still running: {pending[:3]}"
    return "green", f"all {len(runs)} check-runs passed at {sha[:8]}"


def main():
    base = os.environ.get("BASE_REF", "origin/main")
    oci = os.environ.get("OCI_REPO", "oci://ghcr.io/krateo-platformops/charts")

    pins = changed_pins(base)
    if not pins:
        print("pin-ci: no pin version changed in this PR — nothing to check")
        return 0

    failures = []
    for pin in pins:
        name, chart, version = pin["name"], pin["chart"], pin["version"]
        oci_repo = pin["repo"] if (pin["repo"] or "").startswith("oci://") else oci
        meta = chart_metadata(oci_repo, chart, version)
        if meta is None:
            failures.append(f"{name} {version}: chart {oci_repo}/{chart} is NOT PULLABLE — "
                            f"cannot verify anything about it")
            continue
        app = str(meta.get("appVersion") or "").strip()
        sources = [s for s in (meta.get("sources") or []) if s]
        if not app:
            # A CRD-only chart (frontend-crd, snowplow-crd, core-provider-crds) ships no
            # image, so there is no component commit to check. NOT a failure -- treating it
            # as one would block every CRD pin bump and the guard would be turned off.
            print(f"  {name} {pin['previous']} -> {version}  n/a (chart declares no appVersion: "
                  f"no image, so no component CI to check)")
            continue

        checked = []
        for src in sources:
            slug = repo_slug(src)
            if not slug:
                continue
            sha = tag_sha(slug, app)
            if not sha:
                continue
            verdict, detail = ci_at(slug, sha)
            checked.append((slug, verdict, detail))

        if not checked:
            failures.append(
                f"{name} {version}: UNRESOLVED — appVersion {app} matches no tag in any of "
                f"{[repo_slug(s) for s in sources] or 'sources: (none declared)'}. "
                f"Not a pass: an unresolvable pin is where an untested commit hides.")
            continue

        for slug, verdict, detail in checked:
            line = f"  {name} {pin['previous']} -> {version}  app {app}  {slug}: {verdict} ({detail})"
            print(line)
            if verdict == "red":
                failures.append(f"{name} {version}: {slug} CI was RED at the tagged commit — {detail}")
            elif verdict == "running":
                print(f"    note: {slug} CI still in progress — not failing, but the pin is "
                      f"unverified until it completes")
            elif verdict == "norun":
                failures.append(f"{name} {version}: {slug} has NO CI run for the tagged commit — "
                                f"{detail}. Nothing tested this code; that is not the same as it passing.")

    if failures:
        print("::error::pin-ci: a bumped pin points at a commit whose own repo CI is not green "
              "(installer: frontend 1.8.12 was pinned and rolled over a red main):")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"pin-ci: OK — {len(pins)} changed pin(s), every resolved source repo green at its tagged commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
