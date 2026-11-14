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
SOURCES = Path(".github/retraction-sources.yaml")
RAW = "https://raw.githubusercontent.com/%s/%s/retracted-versions.yaml"
API_REF = "https://api.github.com/repos/%s/commits/main"

# The ENFORCE flag is GONE as of 2026-11-14. It existed because a gate that fails closed on a
# file not yet published would block every PR here for a reason unrelated to the PR -- true while
# frontend#628 was open, and it was flipped to True on 2026-11-05 when their list went live. With
# per-publisher sources that tolerance has a better home: a repo is only listed in SOURCES once
# its list exists, and an unlisted repo is simply not read. So a 404 on a LISTED repo now always
# fails closed, and the flag is one fewer thing that can be left in the wrong position. The
# reason a 404 must fail has not changed: a list that disappears is indistinguishable from every
# pin being clean, and the second is what a tolerated 404 would silently report.


def _get(url, token=None, accept=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "krateo-installer-ci")
    if accept:
        req.add_header("Accept", accept)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def resolve_sha(repo, token):
    """Resolve a repo's main to a commit SHA so the output names the ref it actually read."""
    try:
        return json.loads(_get(API_REF % repo, token, "application/vnd.github+json"))["sha"]
    except Exception as e:
        print("  could not resolve %s main to a commit: %s" % (repo, e))
        return None


def load_sources():
    """[(repo, {charts})] -- who publishes a list, and what each may speak for. None if broken."""
    if not SOURCES.is_file():
        return None
    try:
        d = yaml.safe_load(SOURCES.read_text()) or {}
    except Exception:
        return None
    out = []
    for src in (d.get("sources") or []):
        if not isinstance(src, dict) or not src.get("repo"):
            return None
        out.append((str(src["repo"]), {str(c) for c in (src.get("charts") or [])}))
    return out


def entry_versions(entry):
    """The versions an entry retracts, or None when its shape is invalid.

    `version: X` for one, or `versions: [X, Y, ...]` for several. A retracted RANGE is written
    as the explicit list of versions in it, NOT as bounds: comparing semver would add a way to
    be wrong (2.13.9 vs 2.13.90) to a file whose whole job is to be exact.

    Both keys at once is ambiguous and neither is empty. Either is an ERROR, never a skipped
    entry -- an entry this gate quietly ignores is an entry nobody is enforcing, which is the
    failure mode the list exists to remove.
    """
    one, many = entry.get("version"), entry.get("versions")
    if one is not None and many is not None:
        return None
    if one is not None:
        return [str(one)]
    if isinstance(many, list) and many:
        return [str(v) for v in many]
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


def index_entries(entries, repo=None, allowed=None):
    """((chart, version) -> (entry, repo)), and a list of shape/authority errors.

    `allowed` is the chart set this publisher may retract. An entry outside it is an error here,
    on the CONSUMER side, as well as in the publisher's own guard test -- a list that can retract
    anything is a list anyone can use to block anyone.
    """
    index, errs = {}, []
    for e in (entries or []):
        if not isinstance(e, dict):
            errs.append("%s: an entry is not a mapping" % repo)
            continue
        vers = entry_versions(e)
        if vers is None:
            errs.append("%s: entry for charts %s must carry exactly one of `version:` or a "
                        "non-empty `versions:`" % (repo, e.get("charts") or "(none)"))
            continue
        charts = [str(c) for c in (e.get("charts") or [])]
        if not charts:
            errs.append("%s: entry for %s names no charts" % (repo, vers))
            continue
        for ch in charts:
            if allowed is not None and ch not in allowed:
                errs.append("%s retracts chart %r, which it is NOT declared to publish "
                            "(declared: %s)" % (repo, ch, ", ".join(sorted(allowed)) or "none"))
                continue
            for v in vers:
                index[(ch, v)] = (e, repo)
    return index, errs


def evaluate(scope, charts, index):
    """[(name, chart, version, entry, repo)] for each `scope` member at a retracted version."""
    bad = []
    for name, ver in sorted((scope or {}).items()):
        chart = charts.get(name, name)
        hit = index.get((chart, str(ver)))
        if hit:
            entry, repo = hit
            bad.append((name, chart, ver, entry, repo))
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

    sources = load_sources()
    if sources is None:
        print("::error::%s is missing or malformed -- FAILING CLOSED. Without it this gate does "
              "not know whose retraction lists to read." % SOURCES)
        return 1
    if not sources:
        print("::error::%s declares no sources -- FAILING CLOSED. An empty source list reads "
              "exactly like every pin being clean." % SOURCES)
        return 1

    index, n_entries, shape_errors = {}, 0, []
    for repo, allowed in sources:
        sha = resolve_sha(repo, token)
        print("  %s -> %s" % (repo, RAW % (repo, sha or "main")))
        print("    ref: %s" % (("main @ %s" % sha[:7]) if sha
                               else "main (UNRESOLVED -- provenance not checkable)"))
        try:
            body = _get(RAW % (repo, sha or "main"), token)
        except urllib.error.HTTPError as e:
            print("::error::%s's retraction list is unreadable (HTTP %s) -- FAILING CLOSED. A "
                  "list that silently reads as empty reports a safety it never checked. If this "
                  "repo has not published one yet, remove it from %s rather than tolerating the "
                  "404." % (repo, e.code, SOURCES))
            return 1
        except Exception as e:
            print("::error::%s's retraction list is unreadable (%s) -- FAILING CLOSED."
                  % (repo, e))
            return 1
        try:
            doc = yaml.safe_load(body) or {}
        except Exception as e:
            print("::error::%s's retraction list is not valid YAML (%s) -- FAILING CLOSED."
                  % (repo, e))
            return 1
        entries = doc.get("retracted") or []
        idx, errs = index_entries(entries, repo, allowed)
        n_versions = len({v for _, v in idx})
        print("    entries: %d covering %d version(s); may retract: %s"
              % (len(entries), n_versions, ", ".join(sorted(allowed)) or "NOTHING"))
        shape_errors.extend(errs)
        clash = sorted(set(idx) & set(index))
        if clash:
            shape_errors.append("%s retracts %s, which another declared publisher already "
                                "retracts -- two publishers claim the same chart" % (repo, clash))
        index.update(idx)
        n_entries += len(entries)

    if shape_errors:
        for e in shape_errors:
            print("  SHAPE  %s" % e)
        print("::error::a retraction list is malformed, or retracts a chart its publisher is not "
              "declared to own -- FAILING CLOSED. An entry this gate skips is an entry nobody is "
              "enforcing, which is the failure the list exists to remove.")
        return 1

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

    for name, chart, ver, hit, repo in bad:
        print("  FAIL  %s points chart %s at %s, RETRACTED by its publisher %s "
              "(this repo declares it in %s)" % (name, chart, ver, repo, origin.get(name, PINS)))
        print("          use instead: %s" % (hit.get("use") or "<not stated>"))
        print("          reason: %s" % (hit.get("reason") or "<not stated>"))

    if bad:
        print("::error::retracted-versions: a pin points at a version its own repo has "
              "retracted. This is the installer#675 class: frontend asked that 1.8.24 not be "
              "pinned, nothing in either repo recorded it, and it was pinned anyway and caught "
              "only by someone reading main's pins before a tag. Bump to the `use` version.")
        return 1

    print("\nretracted-versions: OK -- %d pin(s)/dependency(ies) checked against %d retraction "
          "entry(ies) from %d publisher(s), matched on the chart field rather than the component "
          "name." % (len(scope), n_entries, len(sources)))
    print("WHAT THIS DOES NOT COVER: a repo absent from %s is not read at all, so its "
          "retractions are invisible here. Declared today: %s."
          % (SOURCES, ", ".join(r for r, _ in sources)))
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

    def idx(entries, repo="krateo-platformops/x", allowed=None):
        i, e = index_entries(entries, repo, allowed)
        return i, e

    # 2. A retraction of a Chart.yaml DEPENDENCY must be caught. Structurally impossible before
    #    2026-11-14: core-provider is not a pin, so nothing matched it.
    i, e = idx([{"version": deps["core-provider"], "charts": ["core-provider"],
                 "use": "2.13.95", "reason": "synthetic"}])
    hit = evaluate(everything, charts, i)
    good = len(hit) == 1 and not e
    print("%s  synthetic retraction of a Chart.yaml dependency caught: %d (want 1), %d error(s)"
          % ("ok  " if good else "FAIL", len(hit), len(e)))
    if not good:
        ok = False

    # 3. The SAME chart at a version nothing points at must NOT match, or case 2 only proves the
    #    matcher keys on the chart name.
    i, _ = idx([{"version": "0.0.0-nonexistent", "charts": ["core-provider"]}])
    miss = evaluate(everything, charts, i)
    print("%s  same chart at an unused version not matched: %d (want 0)"
          % ("ok  " if not miss else "FAIL", len(miss)))
    if miss:
        ok = False

    # 4. `versions:` -- a retracted RANGE as the explicit list of versions in it. This is the
    #    shape core-provider needs for 2.13.85-2.13.94, ten versions in one entry. Both the
    #    pinned version and a version NOT in the list are checked, so a list that matched
    #    everything would fail here.
    live = deps["core-provider"]
    i, e = idx([{"versions": ["2.13.85", "2.13.86", live], "charts": ["core-provider"],
                 "use": "2.13.95", "reason": "synthetic range"}])
    hit = evaluate(everything, charts, i)
    i2, _ = idx([{"versions": ["2.13.85", "2.13.86"], "charts": ["core-provider"]}])
    miss2 = evaluate(everything, charts, i2)
    good = len(hit) == 1 and not miss2 and not e
    print("%s  `versions:` list: pinned version caught %d (want 1), unpinned-only list "
          "matched %d (want 0)" % ("ok  " if good else "FAIL", len(hit), len(miss2)))
    if not good:
        ok = False

    # 5. An entry's SHAPE must be an error, never a silent skip: both keys, or neither.
    _, e_both = idx([{"version": "1.0.0", "versions": ["1.0.0"], "charts": ["core-provider"]}])
    _, e_none = idx([{"charts": ["core-provider"]}])
    _, e_empty = idx([{"versions": [], "charts": ["core-provider"]}])
    _, e_nochart = idx([{"version": "1.0.0"}])
    good = all(len(x) == 1 for x in (e_both, e_none, e_empty, e_nochart))
    print("%s  invalid entry shapes are errors, not skips: both=%d neither=%d empty=%d "
          "nocharts=%d (want 1 each)" % ("ok  " if good else "FAIL", len(e_both), len(e_none),
                                         len(e_empty), len(e_nochart)))
    if not good:
        ok = False

    # 6. A publisher may only retract charts it is declared to publish -- enforced HERE, not
    #    only in the publisher's own test. And the error must not quietly index the entry anyway.
    i, e_auth = idx([{"version": live, "charts": ["core-provider"]}],
                    repo="krateo-platformops/frontend", allowed={"frontend", "frontend-crds"})
    leaked = evaluate(everything, charts, i)
    good = len(e_auth) == 1 and not leaked
    print("%s  a publisher retracting a chart it does not own: %d error(s) (want 1), %d "
          "match(es) leaked (want 0)" % ("ok  " if good else "FAIL", len(e_auth), len(leaked)))
    if not good:
        ok = False
    i, e_okauth = idx([{"version": pins["frontend-crd"], "charts": ["frontend-crds"]}],
                      repo="krateo-platformops/frontend",
                      allowed={"frontend", "frontend-crds", "builder-gate"})
    good = not e_okauth and len(evaluate(pins, charts, i)) == 1
    print("%s  the same publisher retracting a chart it DOES own is accepted: %s"
          % ("ok  " if good else "FAIL", good))
    if not good:
        ok = False

    # 7. frontend's chart-vs-component subtlety, still enforced after the refactor.
    if "frontend-crd" in pins:
        v = pins["frontend-crd"]
        i1, _ = idx([{"version": v, "charts": ["frontend-crds"]}])
        i2, _ = idx([{"version": v, "charts": ["frontend-crd"]}])
        by_chart, by_comp = evaluate(pins, charts, i1), evaluate(pins, charts, i2)
        good = len(by_chart) == 1 and not by_comp
        print("%s  frontend-crd matched by CHART, not component: chart=%d comp=%d (want 1, 0)"
              % ("ok  " if good else "FAIL", len(by_chart), len(by_comp)))
        if not good:
            ok = False
    else:
        print("FAIL  frontend-crd is not pinned -- case 7 cannot run")
        ok = False

    # 8. An empty list matches nothing and does not throw.
    i, e = idx([])
    none = evaluate(everything, charts, i)
    print("%s  empty retraction list matches nothing: %d (want 0), %d error(s)"
          % ("ok  " if (not none and not e) else "FAIL", len(none), len(e)))
    if none or e:
        ok = False

    # 9. SOURCES itself: it is an input to this gate, and a malformed one must fail rather than
    #    read as "no publishers". The declared chart set is frontend's own, confirmed by them.
    src = load_sources()
    good = src is not None and len(src) >= 1
    print("%s  %s parses: %s" % ("ok  " if good else "FAIL", SOURCES,
                                 [(r, len(c)) for r, c in (src or [])]))
    if not good:
        ok = False
    else:
        fe = [c for r, c in src if r == "krateo-platformops/frontend"]
        want = {"frontend", "frontend-crds", "builder-gate"}
        good = len(fe) == 1 and fe[0] == want
        print("%s  frontend declared for exactly %s: %s"
              % ("ok  " if good else "FAIL", sorted(want), sorted(fe[0]) if fe else None))
        if not good:
            ok = False

    print("\nself-test: %s" % ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(0 if self_test() else 1)
    sys.exit(main())
