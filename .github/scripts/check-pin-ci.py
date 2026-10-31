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
import base64
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


def repo_readable(slug):
    """Can this token see the repo at all? Distinguishes NO ACCESS from NO SUCH TAG.

    CI runs with the repo-scoped GITHUB_TOKEN, which CANNOT read a PRIVATE repo in another org.
    26 of this chart's 66 pins are cross-org (krateo-agentiko, krateo-blueprints), and several of
    those repos are private -- so in CI their tag lookups fail for a reason that has nothing to do
    with the pin. Without this distinction the gate reports "appVersion X matches no tag", which
    sends the reader looking for a tag that exists. Measured: the same lookup succeeds from a
    developer token and fails in CI, so a local run is NOT equivalent to the CI run.
    """
    _, rc = sh("gh", "api", f"repos/{slug}", "--jq", ".full_name")
    return rc == 0


def tag_sha(slug, tag):
    out, rc = sh("gh", "api", f"repos/{slug}/git/ref/tags/{tag}",
                 "--jq", '.object.sha + " " + .object.type')
    if rc != 0:
        return None
    sha, _, kind = out.partition(" ")
    # ANNOTATED TAGS: /git/ref/tags/X returns the TAG OBJECT's sha, not the commit's. Checking CI
    # against the tag-object sha finds nothing and reads as "no CI ran" -- it produced three bogus
    # verdicts before this dereference was added.
    if kind.strip() == "tag":
        out2, rc2 = sh("gh", "api", f"repos/{slug}/git/tags/{sha}", "--jq", ".object.sha")
        return out2 or None if rc2 == 0 else None
    return sha or None


# Recognises a check-run that TESTS CODE, as opposed to building, scanning or publishing it.
# Built from the names actually in use across this estate, not from imagination:
#
#   frontend        test · kind · drift · gate · a11y · css-tokens
#   sse-proxy       go test                              (added in sse-proxy#28/#31)
#   core-provider   test-core-provider · test-chart-inspector · test-composition-dynamic-controller
#   snowplow        (NONE — build, lint, preflight, release-*, security/* only)
#
# It is a RECOGNISER and it FAILS CLOSED: when nothing matches, the pin fails rather than
# passes. That direction is the whole point. The earlier version of this guard only failed
# on "no check-run of ANY kind", which let two real pins through vacuously on the day it
# was written: sse-proxy 1.1.9 (11 check-runs, none a test) and snowplow 1.12.56 (13, none
# a test) -- the latter pinned and rolled to krateo-057 behind a guard reporting it green.
#
# A false negative here (a real test suite this pattern does not recognise) is a LOUD,
# correctable failure: widen the pattern. A false positive is a silent pass, which is the
# failure this guard exists to end. Keep the asymmetry.
TEST_RUN_RE = re.compile(r"(^|[^a-z])(go )?test(s|ing)?([^a-z]|$)|^(vet|kind|drift|gate)$", re.I)


def ci_at(slug, sha):
    """(verdict, detail). verdict in {green, red, norun, notested, running}.

    Uses the commit's CHECK-RUNS, not a guessed workflow filename. Guessing was the first
    implementation and it was silently blind: core-provider has no `test.yaml` at all --
    its tests run as `test-core-provider` et al -- so a filename lookup returned "unknown"
    and PASSED.

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

    # Everything green -- but green at WHAT? A commit whose only checks build, scan and
    # publish it has not been tested, and "CI was green" would be read as "the code was
    # tested". That does not follow, and a check-run COUNT cannot separate the two either
    # (11 for an untested repo against 13 for another untested one).
    tested = sorted({r["n"] for r in runs if TEST_RUN_RE.search(r["n"] or "")})
    if tested:
        return "green", f"all {len(runs)} passed at {sha[:8]}, incl. {tested[:3]}"

    # THE NAME IS NOT THE EVIDENCE. A check-run is named after its workflow JOB, and a job that
    # runs tests need not say so in its name: krateo-platformops/portal runs nine suites under
    # names like `composition-architecture` (three test-*.py scripts) and `builder-crs`. Judging
    # by name alone produced a FALSE `notested` on portal 1.8.85 and blocked a roll on a regex.
    #
    # So before reporting notested, resolve each GREEN check-run to its job in the workflow files
    # AT THE PINNED COMMIT and look at what that job actually runs. This can only convert a false
    # notested into green; it cannot excuse a failure, because failures are judged above and never
    # reach here. And it is anchored to jobs that ACTUALLY RAN AND PASSED — a test job defined in
    # the workflow but skipped or failed is not evidence.
    ran = {(r["n"] or "").split(" /")[0].strip() for r in runs}
    invoked = _test_invocations(slug, sha, ran)
    if invoked:
        return "green", (f"all {len(runs)} passed at {sha[:8]}; no check-run NAME says test, but "
                         f"{len(invoked)} green job(s) invoke one: " +
                         ", ".join(f"{j} -> {s}" for j, s in sorted(invoked)[:3]))
    # Distinguish "unrecognised" from "absent" by looking at the tree, not at names.
    has_tests, examined, sample = _repo_has_test_paths(slug, sha)
    if has_tests is False:
        return "notests", (f"all {len(runs)} check-runs passed at {sha[:8]}; repo has NO test suite "
                           f"— {examined} paths examined at that commit, none test-shaped. Nothing "
                           f"to run, so green is honest here (chart-only repo).")
    # A FOURTH CASE, and the message matters as much as the verdict. The tests may have RUN, green,
    # on a DIFFERENT commit whose tree is byte-identical — which is the same code. This is the
    # normal shape when a repo runs its test job on `pull_request` only: the squash commit pushed to
    # main (the one that gets tagged) never acquires a test check-run, while the PR head it was
    # squashed from has one. Hit live on krateo-platformops/authn 0.29.0: tagged 71cc990 and PR #51
    # head 143d86d share tree 39c059b3, and `checks / test` passed on the head.
    #
    # Reporting that as "they exist and did not run" is FALSE and actively harmful: it sends someone
    # to wire tests that already exist and already passed on this exact code. So resolve the
    # commit's PR, compare TREES (not messages, not dates), and if a test check-run passed on an
    # identical tree, say so and give the real remedy — run the checks job on push to main so the
    # tagged commit carries its own evidence.
    #
    # It still REFUSES. The tag is what gets pinned, and a tag with no test evidence of its own is a
    # genuine release-process gap; the component owner who hit this agreed and fixed the process
    # rather than ask for the gate to be loosened. But it refuses for the true reason.
    tt = _tested_on_identical_tree(slug, sha)
    if tt:
        other, tree, names = tt
        return "tree-tested", (
            f"the TAGGED COMMIT {sha[:8]} carries no test check-run ({len(runs)} green, none a "
            f"test) — BUT THE CODE IS TESTED: {other[:8]} has the byte-identical tree "
            f"{tree[:8]} and {names} "
            f"passed there. So the tests exist AND ran — on the PR head, not on the squash commit "
            f"that got tagged. The gap is the release process, not the suite: run the test job on "
            f"push to main (or on the tag) so the tagged commit carries its own test check-run. Do "
            f"NOT widen TEST_RUN_RE for this and do NOT wire new tests — both already exist.")

    unreadable = "" if has_tests is True else " (could not read the repo tree to check for a suite)"
    return "notested", (f"all {len(runs)} check-runs passed at {sha[:8]} but NONE tests code — "
                        f"neither by name nor by what any green job runs "
                        f"(saw: {sorted({r['n'].split(' /')[0] for r in runs})[:6]})"
                        f"{unreadable}. The repo DOES contain test-shaped paths"
                        f"{' e.g. ' + ', '.join(sample) if sample else ''}, and no commit with an "
                        f"identical tree has a passing test check-run either, so they did not run.")


def _tested_on_identical_tree(slug, sha):
    """(other_sha, tree_sha, names) if a test check-run passed on a commit with the SAME tree.

    Trees are compared, not commit messages or timestamps: an identical tree IS identical code,
    which is the only thing that makes the other commit's test result transferable. Anything that
    cannot be established is returned as None rather than guessed.
    """
    def api(path):
        out, rc = sh("gh", "api", path, "-H", "Accept: application/vnd.github+json")
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except Exception:
            return None

    def tree_of(c):
        d = api(f"repos/{slug}/commits/{c}")
        return ((d or {}).get("commit") or {}).get("tree", {}).get("sha")

    tree = tree_of(sha)
    if not tree:
        return None
    prs = api(f"repos/{slug}/commits/{sha}/pulls")
    for pr in prs if isinstance(prs, list) else []:
        head = (pr.get("head") or {}).get("sha")
        if not head or head == sha or tree_of(head) != tree:
            continue
        cr = ((api(f"repos/{slug}/commits/{head}/check-runs?per_page=100") or {})
              .get("check_runs") or [])
        green = sorted({c.get("name") for c in cr
                        if c.get("conclusion") == "success"
                        and TEST_RUN_RE.search(c.get("name") or "")})
        if green:
            return head, tree, green[:3]
    return None


# A THIRD CASE, distinct from both green and notested: the repo HAS NO TEST SUITE AT ALL.
#
# `notested` conflates two very different things — "tests exist and this guard did not recognise
# them" and "there is nothing to test". The guard's own remedy text offered only the first
# ("wire tests, or widen TEST_RUN_RE"), which is unactionable for a chart-only blueprint repo that
# ships YAML and has no code. Hit live on krateo-blueprints/github-provider-kog 0.6.0: 158 paths,
# zero test-shaped, workflows limited to lint / preflight-refs / release-* / security.
#
# THE ABSENCE MUST BE PROVEN, NOT INFERRED. This reads the git tree AT THE PINNED COMMIT and looks
# for test-shaped paths. A failure to match a NAME is not evidence of absence — that is the same
# error as reading an empty list as a real zero. So a repo is exempt only when the tree itself
# contains nothing testable, and the detail line says how many paths were examined so a reader can
# see the instrument looked.
TEST_PATH_RE = re.compile(r"(_test\.(go|py|ts|js)$|(^|/)tests?/|\.spec\.(ts|js)$|(^|/)test_[^/]+\.py$)")


def _repo_has_test_paths(slug, sha):
    """(has_tests, examined, sample). Reads the tree at `sha`; (None, 0, []) if unreadable."""
    out, rc = sh("gh", "api", f"repos/{slug}/git/trees/{sha}?recursive=1",
                 "--jq", "[.tree[].path]")
    if rc != 0:
        return None, 0, []
    try:
        paths = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, 0, []
    hits = [p for p in paths if TEST_PATH_RE.search(p)]
    return (len(hits) > 0), len(paths), hits[:3]


MAKE_RE = re.compile(r"\bmake\s+(?:-[\w-]+\s+)*([\w.-]+(?:\s+[\w.-]+)*)")


def _resolve_make_targets(makefile_text, targets):
    """PURE half of the Makefile resolution: does `make <targets>` reach a test command?

    Split from the fetch so it is testable with no network -- the fetching wrapper below is a
    thin shim, and every rule in here is exercised by fixtures in
    .github/scripts/check-pin-ci-fixtures.py.

    Minimal Makefile model: `target: prereqs ## help` plus tab-indented recipe lines. Resolves
    the named targets transitively through their prerequisites, cycle-safe, and matches
    TEST_CMD_RE against the recipe lines actually REACHED.

    Returns the matched command string, or None.
    """
    rules, cur = {}, None
    for line in makefile_text.splitlines():
        if line.startswith("\t") and cur:
            rules[cur][1].append(line.strip())
            continue
        m = re.match(r"^([A-Za-z0-9_.\-/%]+)\s*:(?!=)\s*(.*)$", line)
        if m:
            cur = m.group(1)
            deps = m.group(2).split("##")[0].split()
            rules.setdefault(cur, ([], []))
            rules[cur] = (rules[cur][0] + deps, rules[cur][1])
    seen, queue = set(), list(targets)
    while queue:
        t = queue.pop()
        if t in seen or t not in rules:
            continue
        seen.add(t)
        deps, recipe = rules[t]
        for r in recipe:
            m = TEST_CMD_RE.search(r)
            if m:
                return m.group(0).strip()
        queue.extend(deps)
    return None


def _make_targets_test(repo, sha, targets):
    """Does `make <target>` reach a test command, resolving the Makefile at `sha`?

    WHY THIS EXISTS. A step that runs `make check` matches no TEST_CMD_RE pattern, and correctly
    so -- the target name says nothing about what runs. But declining to resolve it produced a
    FALSE verdict: this guard reported incident-controller 0.5.1 as "CI is green but NOTHING
    TESTED THE CODE" when .github/workflows/check.yaml runs `make check`, whose chain is
    `check: build lint test check-generate lint-charts` and whose `test:` recipe is
    `go test -race -cover ./...`. The job log at 8bfc229a shows the race-enabled suite running
    with coverage on three packages. alert-provider 0.2.54 is the same shape at 033e9476.

    The pin was blocked on the guard's blindness, and I reported that blindness to the user as a
    finding about the repo -- "6 _test.go files and ZERO go test references across five
    workflows". The grep was true of the literal strings I searched (`go test`, `gotestsum`,
    `ginkgo`, `make test`) and false as the claim I drew from it, because the repo spells it
    `make check`. A fifth spelling I never looked for.

    STRICTNESS IS PRESERVED. This widens what the guard can SEE, not what it will ACCEPT:
      make build -> `build:` reaches no test command        -> still not tested
      make lint  -> `lint: go vet ./...`                    -> tested (go vet is in scope)
      make check -> reaches `go test -race -cover ./...`    -> tested
    A green job that genuinely runs no tests still fails: the match is on reached recipe text.

    No Makefile is not a failure -- the caller falls back to the direct-command match.
    """
    raw, rc = sh("gh", "api", f"/repos/{repo}/contents/Makefile?ref={sha}", "--jq", ".content")
    if rc != 0 or not raw:
        return None
    try:
        text = base64.b64decode(raw).decode("utf-8", "replace")
    except ValueError:
        return None
    return _resolve_make_targets(text, targets)


# Invocations that constitute "this job tested the code". Deliberately about the COMMAND, not the
# job's name: a script called test-*.py, a test runner, or a language's test subcommand.
TEST_CMD_RE = re.compile(
    r"(^|[\s/])test[-_][\w.-]+\.(py|sh|js|ts)"      # scripts/test-foo.py
    r"|(^|[\s/])[\w.-]+[-_]test\.(py|sh|js|ts)"      # foo_test.py
    r"|\bgo\s+test\b|\bpytest\b|\bnpm\s+(run\s+)?test\b|\byarn\s+test\b"
    r"|\bvitest\b|\bjest\b|\bhelm\s+unittest\b|\bgo\s+vet\b",
    re.I)


def _test_invocations(repo, sha, ran_names):
    """{(job, script)} for workflow jobs that BOTH ran green and invoke something test-shaped.

    Reads the workflow files at the pinned commit, so it describes the tree that was released
    rather than whatever main says today.
    """
    # sh() returns (stdout, returncode) — not a string. Unpacking it is the whole reason the
    # first version of this fallback silently found nothing: json.loads got a tuple, raised, and
    # a blanket `except` turned the breakage into "no tests here". A fallback that cannot
    # distinguish its own failure from a negative result is worse than no fallback, so the
    # excepts below are narrow and anything unexpected is REPORTED rather than swallowed.
    body, rc = sh("gh", "api", f"/repos/{repo}/contents/.github/workflows?ref={sha}")
    if rc != 0:
        print(f"  note: could not list {repo}'s workflows at {sha[:8]} (gh rc={rc}); "
              f"falling back to the name check alone")
        return set()
    try:
        listing = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"  note: {repo}'s workflow listing at {sha[:8]} was not JSON ({e}); "
              f"falling back to the name check alone")
        return set()
    out = set()
    for entry in listing if isinstance(listing, list) else []:
        name = entry.get("name", "")
        if not name.endswith((".yml", ".yaml")):
            continue
        raw, rc = sh("gh", "api", f"/repos/{repo}/contents/.github/workflows/{name}?ref={sha}",
                     "--jq", ".content")
        if rc != 0 or not raw:
            print(f"  note: could not read {repo}:.github/workflows/{name} at {sha[:8]}")
            continue
        try:
            text = base64.b64decode(raw).decode("utf-8", "replace")
            doc = yaml.safe_load(text) or {}
        except (ValueError, yaml.YAMLError) as e:
            print(f"  note: {name} at {sha[:8]} did not parse ({type(e).__name__}); skipping it")
            continue
        for job_key, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            label = str(job.get("name") or job_key)
            # The check-run carries the job's display name, falling back to its key.
            if label not in ran_names and job_key not in ran_names:
                continue
            for step in (job.get("steps") or []):
                run = step.get("run") if isinstance(step, dict) else None
                if not isinstance(run, str):
                    continue
                m = TEST_CMD_RE.search(run)
                if m:
                    out.add((label, m.group(0).strip()))
                    break
                # `make <target>` matches nothing above, and must not: the target name says
                # nothing about what runs. Resolve it through the Makefile at this commit.
                mk = MAKE_RE.search(run)
                if mk:
                    cmd = _make_targets_test(repo, sha, mk.group(1).split())
                    if cmd:
                        out.add((label, f"make {mk.group(1)} -> {cmd}"))
                        break
    return out


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
            # WHY it did not resolve matters: "no such tag" is the pin's problem, "cannot read
            # the repo" is this gate's. Reporting the first when it is the second sends the reader
            # hunting for a tag that exists.
            slugs = [repo_slug(s) for s in sources if repo_slug(s)]
            unreadable = [s for s in slugs if not repo_readable(s)]
            if unreadable and len(unreadable) == len(slugs):
                failures.append(
                    f"{name} {version}: UNVERIFIABLE — this token cannot read "
                    f"{', '.join(unreadable)}, so appVersion {app} could not be resolved to a "
                    f"commit. The tag may well exist. In CI the repo-scoped GITHUB_TOKEN cannot "
                    f"read a PRIVATE repo in another org, and 26 of this chart's pins are "
                    f"cross-org — so a local run of this script is NOT equivalent to the CI run. "
                    f"Fix by setting the org secret PIN_CI_READ_TOKEN to a fine-grained PAT with "
                    f"contents:read + checks:read on the cross-org repos; the workflow prefers it "
                    f"and falls back to GITHUB_TOKEN, and its first log line says which it used. "
                    f"Do NOT 'fix' it by removing the source or the appVersion. Still a failure: "
                    f"unverifiable is not a pass.")
            else:
                failures.append(
                    f"{name} {version}: UNRESOLVED — appVersion {app} matches no tag in any of "
                    f"{slugs or 'sources: (none declared)'}"
                    f"{' (readable: ' + ', '.join(s for s in slugs if s not in unreadable) + ')' if unreadable else ''}. "
                    f"Not a pass: an unresolvable pin is where an untested commit hides.")
            continue

        for slug, verdict, detail in checked:
            # ONE line per pin per source. The `notests` arm used to print a second line, which
            # read as the check disagreeing with itself — the same defect verify-roll.sh had when
            # it emitted `ok` and `FAIL` for one object.
            shown = "n/a" if verdict == "notests" else verdict
            print(f"  {name} {pin['previous']} -> {version}  app {app}  {slug}: {shown} ({detail})")
            if verdict == "red":
                failures.append(f"{name} {version}: {slug} CI was RED at the tagged commit — {detail}")
            elif verdict == "running":
                print(f"    note: {slug} CI still in progress — not failing, but the pin is "
                      f"unverified until it completes")
            elif verdict == "norun":
                failures.append(f"{name} {version}: {slug} has NO CI run for the tagged commit — "
                                f"{detail}. Nothing tested this code; that is not the same as it passing.")
            elif verdict == "notested":
                failures.append(
                    f"{name} {version}: {slug} CI is green but NOTHING TESTED THE CODE — {detail}. "
                    f"Either wire tests in that repo, or widen TEST_RUN_RE if its suite is simply "
                    f"named something this guard does not recognise. Passing here would report "
                    f"'CI was green' for a commit nothing tested.")
            elif verdict == "tree-tested":
                # Distinct from notested ON PURPOSE. The remedy is the opposite: do not touch the
                # suite or this regex, fix the release process. Saying "nothing tested the code"
                # here would be false and would send someone to wire tests that already pass.
                failures.append(f"{name} {version}: {slug} — {detail}")

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
