#!/usr/bin/env python3
"""CI guard: a pin must not point at a version its OWN REPO has retracted.

WHY THIS EXISTS, and it is a specific failure rather than a hypothetical.
installer#675 pinned frontend/frontend-crd/builder-gate 1.8.22 -> 1.8.24 at
2026-11-05T12:58:05Z. The frontend session had asked, in writing, that 1.8.24 NOT be pinned:
it was cut from their main before #624 merged, so its `allowedResources` enum carries 47
values where main carries 54 (missing columns, datagrids, eventlists, navmenus, routes,
routesloaders, tablists). That request reached one session and not the other, and NOTHING IN
EITHER REPO RECORDED IT -- 1.8.24's GitHub release is an ordinary one, prerelease=false with
normal notes, and a grep of frontend main found no marker. frontend confirmed that directly.

It was caught only because a human-equivalent read main's pins before cutting a tag and
noticed a commit they had not made. That is not a control; that is luck. Measured exposure
had it shipped: 057 and demo were both 0 objects outside the 47 (frontend's census: 326
values / 683 objects, and 298 values / 596 objects), so neither cluster I roll would have
broken. alpitour had THREE objects outside it -- Page/blueprints-page and
Page/compositions-page on `datagrids`, RoutesLoader/routes-loader on `routes`. And a CRD
tightening bites AT THE NEXT WRITE, not at the CRD apply, because stored objects are not
revalidated: the symptom would have been a failed helm release for whichever component next
wrote one of those kinds, surfacing long after the roll that caused it.

THE CONTRACT, as frontend specified it:

    retracted:
      - version: 1.8.24
        charts: [frontend-crds]
        use: 1.8.26
        reason: ...

MATCH ON THE `chart:` FIELD OF component-pins.yaml, NOT THE COMPONENT NAME. This is the
subtlety frontend called out and a test on their side refuses the wrong one: the component is
named `frontend-crd`, the chart is `frontend-crds`. Matching on the component name finds
nothing and the gate would pass silently -- the worst possible outcome for a guard. A pin with
no explicit `chart:` defaults to its component name, which is also correct, because for those
components the two coincide.

ONLY THE LISTED CHARTS ARE RETRACTED. frontend lists `frontend-crds` alone for 1.8.24: the app
and builder-gate at 1.8.24 do not carry the defect. So a retraction is (chart, version), never
a version across every chart that shares it.

FAILS CLOSED, like lint-pinned-crds. If the file cannot be read -- network, 404, bad YAML --
this gate FAILS rather than passing. A retraction list that silently becomes empty is worse
than none, because it reports safety it never checked. The one exception is the deliberate
pre-enable state below.

NAMES THE REF IT READ. `main` moves, so a run that says "read from main" is unfalsifiable a day
later. This resolves main to a commit SHA first and prints it, so the provenance is checkable.
That was frontend's own improvement on an earlier suggestion of mine, and it is better than
what I proposed.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PINS = Path("chart/files/component-pins.yaml")
CHART = Path("chart/Chart.yaml")
OWNER_REPO = "krateo-platformops/frontend"
RAW = "https://raw.githubusercontent.com/%s/%s/retracted-versions.yaml"
API_REF = "https://api.github.com/repos/%s/commits/main" % OWNER_REPO

# ENABLED 2026-11-05, in the same change that confirmed the file is live. frontend#628 merged and
# https://raw.githubusercontent.com/krateo-platformops/frontend/<sha>/retracted-versions.yaml
# returns 200 with one entry (frontend-crds 1.8.24 -> use 1.8.26). A 404 now FAILS, which is the
# point: the list disappearing is indistinguishable from every pin being clean, and the second is
# what a tolerated 404 would silently report. Historical note on the pre-enable state:
# A gate that fails closed on a file that does not exist yet would block every PR in this repo
# for a reason unrelated to the PR. So while ENFORCE is false a 404 is reported and tolerated;
# every OTHER failure (bad YAML, a reachable-but-broken file, a retracted pin) still fails.
# Flip this to True in the same commit that frontend#628 merges, and the 404 tolerance is gone.
ENFORCE = True


def _get(url, token=None, accept=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "krateo-installer-ci")
    if accept:
        req.add_header("Accept", accept)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def resolve_main(token):
    """Resolve main to a commit SHA so the output names the ref it actually read."""
    try:
        return json.loads(_get(API_REF, token, "application/vnd.github+json"))["sha"]
    except Exception as e:
        print("  could not resolve %s main to a commit: %s" % (OWNER_REPO, e))
        return None


def _merge_base():
    """The PR's merge base, or None when this is not a pull request."""
    base = os.environ.get("GITHUB_BASE_REF")
    if not base:
        return None  # not a PR: check everything instead
    r = subprocess.run(["git", "merge-base", "origin/%s" % base, "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _at(ref, path):
    """`path` as of `ref`, parsed, or None when it cannot be read."""
    r = subprocess.run(["git", "show", "%s:%s" % (ref, path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return yaml.safe_load(r.stdout) or {}
    except Exception:
        return None


def changed_pins():
    """The pins this PR changes, against the merge base. Empty on a push to main."""
    mb = _merge_base()
    if mb is None:
        return None
    od = _at(mb, PINS)
    if od is None:
        return None
    oc = od.get("components") if isinstance(od, dict) else od
    before = {c["name"]: c.get("version") for c in (oc or [])}
    now = load_pins()
    return {n: v for n, v in now.items() if before.get(n) != v}


def load_deps():
    """Chart.yaml dependency name -> version.

    The bootstrap subcharts live HERE and never in the pin file, so a gate reading only the pins
    cannot see them. core-provider and core-provider-crds are the live example: core-provider
    asked that 2.13.85-2.13.94 never be pinned, and before this change a retraction entry for
    them would have matched nothing and the gate would have passed -- silently, which for a guard
    is the worst outcome there is.
    """
    if not CHART.is_file():
        return {}
    d = yaml.safe_load(CHART.read_text()) or {}
    deps = d.get("dependencies") if isinstance(d, dict) else None
    out = {}
    for c in (deps or []):
        if isinstance(c, dict) and c.get("name"):
            out[str(c["name"])] = c.get("version")
    return out


def changed_deps():
    """The Chart.yaml dependencies this PR changes, against the merge base."""
    mb = _merge_base()
    if mb is None:
        return None
    od = _at(mb, CHART)
    if od is None:
        return None
    before = {}
    for c in ((od.get("dependencies") or []) if isinstance(od, dict) else []):
        if isinstance(c, dict) and c.get("name"):
            before[str(c["name"])] = c.get("version")
    now = load_deps()
    return {n: v for n, v in now.items() if before.get(n) != v}


def index_entries(entries):
    """(chart, version) -> entry. A retraction is per CHART, never a bare version."""
    index = {}
    for e in (entries or []):
        ver = str(e.get("version") or "")
        for ch in (e.get("charts") or []):
            index[(str(ch), ver)] = e
    return index


def evaluate(scope, charts, index):
    """[(name, chart, version, entry)] for each member of `scope` pointing at a retracted version."""
    bad = []
    for name, ver in sorted((scope or {}).items()):
        chart = charts.get(name, name)
        hit = index.get((chart, str(ver)))
        if hit:
            bad.append((name, chart, ver, hit))
    return bad


def load_pins():
    d = yaml.safe_load(PINS.read_text()) or {}
    comps = d.get("components") if isinstance(d, dict) else d
    return {c["name"]: c.get("version") for c in (comps or [])}


def pin_charts():
    """component name -> the chart it resolves to. Defaults to the component name."""
    d = yaml.safe_load(PINS.read_text()) or {}
    comps = d.get("components") if isinstance(d, dict) else d
    return {c["name"]: (c.get("chart") or c["name"]) for c in (comps or [])}


def main() -> int:
    if not PINS.is_file():
        print("::error::%s not found -- run from the repo root" % PINS)
        return 1
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    sha = resolve_main(token)
    url = RAW % (OWNER_REPO, sha or "main")
    print("  retraction list: %s" % url)
    print("  ref: %s" % (("%s main @ %s" % (OWNER_REPO, sha[:7])) if sha
                         else "%s main (UNRESOLVED -- provenance not checkable)" % OWNER_REPO))

    try:
        body = _get(url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404 and not ENFORCE:
            print("  retraction list is 404 and ENFORCE is False -- frontend#628 has not merged "
                  "yet. TOLERATED by design; flip ENFORCE in the same commit it lands.")
            print("\nretracted-versions: SKIPPED (list not published yet). This gate is INERT "
                  "until ENFORCE=True -- it is not evidence that any pin is unretracted.")
            return 0
        print("::error::retraction list unreadable (HTTP %s) -- FAILING CLOSED. A list that "
              "silently reads as empty reports a safety it never checked." % e.code)
        return 1
    except Exception as e:
        print("::error::retraction list unreadable (%s) -- FAILING CLOSED." % e)
        return 1

    try:
        doc = yaml.safe_load(body) or {}
    except Exception as e:
        print("::error::retraction list is not valid YAML (%s) -- FAILING CLOSED." % e)
        return 1
    entries = doc.get("retracted") or []
    print("  entries: %d" % len(entries))
    index = index_entries(entries)

    pins, deps = load_pins(), load_deps()
    charts = dict(pin_charts())
    for n in deps:
        charts.setdefault(n, n)  # a Chart.yaml dependency's chart IS its name
    collisions = sorted(set(pins) & set(deps))
    if collisions:
        print("::error::%s appear in BOTH %s and %s. A retraction would be checked against one "
              "file's version and fixed in the other's, so this is an error rather than a "
              "precedence rule." % (collisions, PINS, CHART))
        return 1

    cp, cd = changed_pins(), changed_deps()
    if cp is None or cd is None:
        scope = dict(pins)
        scope.update(deps)
        origin = {n: str(PINS) for n in pins}
        origin.update({n: str(CHART) for n in deps})
        print("  scope: EVERY pin and Chart.yaml dependency (%d + %d) -- not a pull request, or "
              "a base file could not be read" % (len(pins), len(deps)))
    else:
        scope = dict(cp)
        scope.update(cd)
        origin = {n: str(PINS) for n in cp}
        origin.update({n: str(CHART) for n in cd})
        print("  scope: %d changed by this PR -- %d pin(s), %d Chart.yaml dependency(ies)%s"
              % (len(scope), len(cp), len(cd),
                 (" -- " + ", ".join(sorted(scope))) if scope else ""))

    bad = evaluate(scope, charts, index)

    for name, chart, ver, hit in bad:
        print("  FAIL  %s points chart %s at %s, which its own repo has RETRACTED "
              "(declared in %s)" % (name, chart, ver, origin.get(name, PINS)))
        print("          use instead: %s" % (hit.get("use") or "<not stated>"))
        print("          reason: %s" % (hit.get("reason") or "<not stated>"))

    if bad:
        print("::error::retracted-versions: a pin points at a version its own repo has "
              "retracted. This is the installer#675 class: frontend asked that 1.8.24 not be "
              "pinned, nothing in either repo recorded it, and it was pinned anyway and caught "
              "only by someone reading main's pins before a tag. Bump to the `use` version.")
        return 1

    print("\nretracted-versions: OK -- %d pin(s)/dependency(ies) checked against %d retraction "
          "entry(ies), matched on the chart field rather than the component name."
          % (len(scope), len(entries)))
    print("WHAT THIS DOES NOT COVER: only %s publishes a list today, so a retraction by any "
          "other component repo is invisible here. And an entry is keyed on an EXACT version, so "
          "a retracted RANGE needs one entry per version in it -- core-provider 2.13.85-2.13.94 "
          "is ten entries, not one." % OWNER_REPO)
    return 0


def self_test():
    """Offline. Proves the Chart.yaml scope is real and that the matcher can still fail."""
    ok = True

    # 1. The real worktree, through the readers main() uses. Non-vacuity: if either file stops
    #    yielding members, every case below passes while measuring nothing.
    pins, deps = load_pins(), load_deps()
    good = bool(pins) and bool(deps)
    print("%s  worktree: %d pin(s) from %s, %d dependency(ies) from %s"
          % ("ok  " if good else "FAIL", len(pins), PINS, len(deps), CHART))
    if not good:
        return False
    for n in ("core-provider", "core-provider-crds"):
        present = n in deps
        print("%s  %s reachable via %s: %s" % ("ok  " if present else "FAIL", n, CHART, present))
        if not present:
            ok = False
    both = sorted(set(pins) & set(deps))
    print("%s  no name in both files: %s" % ("ok  " if not both else "FAIL", both or "none"))
    if both:
        ok = False

    charts = dict(pin_charts())
    for n in deps:
        charts.setdefault(n, n)
    everything = dict(pins)
    everything.update(deps)

    # 2. A retraction of a Chart.yaml DEPENDENCY must be caught. Structurally impossible before
    #    this change: core-provider is not a pin, so nothing matched it.
    hit = evaluate(everything, charts, index_entries(
        [{"version": deps["core-provider"], "charts": ["core-provider"], "use": "2.13.95",
          "reason": "synthetic"}]))
    print("%s  synthetic retraction of a Chart.yaml dependency caught: %d (want 1)"
          % ("ok  " if len(hit) == 1 else "FAIL", len(hit)))
    if len(hit) != 1:
        ok = False

    # 3. The SAME chart at a version nothing points at must NOT match, or case 2 only proves the
    #    matcher keys on the chart name.
    miss = evaluate(everything, charts,
                    index_entries([{"version": "0.0.0-nonexistent", "charts": ["core-provider"]}]))
    print("%s  same chart at an unused version not matched: %d (want 0)"
          % ("ok  " if not miss else "FAIL", len(miss)))
    if miss:
        ok = False

    # 4. frontend's chart-vs-component subtlety, still enforced after the refactor.
    if "frontend-crd" in pins:
        v = pins["frontend-crd"]
        by_chart = evaluate(pins, charts, index_entries([{"version": v, "charts": ["frontend-crds"]}]))
        by_comp = evaluate(pins, charts, index_entries([{"version": v, "charts": ["frontend-crd"]}]))
        good = len(by_chart) == 1 and not by_comp
        print("%s  frontend-crd matched by CHART, not component: chart=%d comp=%d (want 1, 0)"
              % ("ok  " if good else "FAIL", len(by_chart), len(by_comp)))
        if not good:
            ok = False
    else:
        print("FAIL  frontend-crd is not pinned -- case 4 cannot run")
        ok = False

    # 5. An empty list must match nothing, and must not throw.
    none = evaluate(everything, charts, index_entries([]))
    print("%s  empty retraction list matches nothing: %d (want 0)"
          % ("ok  " if not none else "FAIL", len(none)))
    if none:
        ok = False

    print("\nself-test: %s" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(0 if self_test() else 1)
    sys.exit(main())
