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
OWNER_REPO = "krateo-platformops/frontend"
RAW = "https://raw.githubusercontent.com/%s/%s/retracted-versions.yaml"
API_REF = "https://api.github.com/repos/%s/commits/main" % OWNER_REPO

# DELIBERATE PRE-ENABLE STATE. frontend#628 publishes the file; until it merges the URL 404s.
# A gate that fails closed on a file that does not exist yet would block every PR in this repo
# for a reason unrelated to the PR. So while ENFORCE is false a 404 is reported and tolerated;
# every OTHER failure (bad YAML, a reachable-but-broken file, a retracted pin) still fails.
# Flip this to True in the same commit that frontend#628 merges, and the 404 tolerance is gone.
ENFORCE = False


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


def changed_pins():
    """The pins this PR changes, against the merge base. Empty on a push to main."""
    base = os.environ.get("GITHUB_BASE_REF")
    if not base:
        return None  # not a PR: check every pin instead
    r = subprocess.run(["git", "merge-base", "origin/%s" % base, "HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    mb = r.stdout.strip()
    old = subprocess.run(["git", "show", "%s:%s" % (mb, PINS)], capture_output=True, text=True)
    if old.returncode != 0:
        return None
    try:
        od = yaml.safe_load(old.stdout) or {}
    except Exception:
        return None
    oc = od.get("components") if isinstance(od, dict) else od
    before = {c["name"]: c.get("version") for c in (oc or [])}
    now = load_pins()
    return {n: v for n, v in now.items() if before.get(n) != v}


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

    # (chart, version) -> entry. A retraction is per CHART, never a bare version.
    index = {}
    for e in entries:
        ver = str(e.get("version") or "")
        for ch in (e.get("charts") or []):
            index[(str(ch), ver)] = e

    charts = pin_charts()
    scope = changed_pins()
    if scope is None:
        scope = load_pins()
        print("  scope: EVERY pin (not a pull request, or the base could not be read)")
    else:
        print("  scope: %d pin(s) changed by this PR%s"
              % (len(scope), (" -- " + ", ".join(sorted(scope))) if scope else ""))

    bad = []
    for name, ver in sorted(scope.items()):
        chart = charts.get(name, name)
        hit = index.get((chart, str(ver)))
        if hit:
            bad.append((name, chart, ver, hit))

    for name, chart, ver, hit in bad:
        print("  FAIL  %s pins chart %s at %s, which its own repo has RETRACTED"
              % (name, chart, ver))
        print("          use instead: %s" % (hit.get("use") or "<not stated>"))
        print("          reason: %s" % (hit.get("reason") or "<not stated>"))

    if bad:
        print("::error::retracted-versions: a pin points at a version its own repo has "
              "retracted. This is the installer#675 class: frontend asked that 1.8.24 not be "
              "pinned, nothing in either repo recorded it, and it was pinned anyway and caught "
              "only by someone reading main's pins before a tag. Bump to the `use` version.")
        return 1

    print("\nretracted-versions: OK -- %d pin(s) checked against %d retraction entry(ies), "
          "matched on the chart field rather than the component name." % (len(scope), len(entries)))
    print("WHAT THIS DOES NOT COVER: only %s publishes a list today, so a retraction by any "
          "other component repo is invisible here." % OWNER_REPO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
