#!/usr/bin/env python3
"""CI guard: a non-empty `registryAuth.registries[]` must cover EVERY repo that needs a credential.

THE FOOT-GUN, read from inst.chartExtras (chart/templates/_helpers.tpl):

    {{- if $registries -}}
          ... match $r.repo VERBATIM against the component's effective repo,
              take that entry's credential ...
    {{- else if $ra.enabled -}}
          ... use the GLOBAL credential ...
    {{- end -}}

It is `if` / `ELSE IF`. So the moment `registries[]` is non-empty the global credential becomes
UNREACHABLE, for every component whose repo does not match an entry. There is no fallback.

WHY THAT IS DANGEROUS RATHER THAN MERELY SURPRISING. Adding one scoped entry reads as a purely
additive values change -- a new list item, nothing removed. On a cluster in global mode it silently
withdraws the credential from every other private repo, and the symptom is charts failing to pull
with no values line that looks wrong.

MEASURED ON krateo-057, 2026-11-22, which is one entry away from exactly that:

    registryAuth.enabled: true, username set, passwordRef present, registries: []   <- GLOBAL mode
    pins span SIX repos:
      <default> ghcr.io/krateo-platformops/charts      51 components
      ghcr.io/krateo-blueprints/charts                 22
      ghcr.io/krateo-agentiko/charts                   17   <- PRIVATE by design
      ghcr.io/cloudnative-pg/charts                     1
      ghcr.io/external-secrets/charts                   1
      ghcr.io/krateo-platformops/krateo-saas            1   <- PRIVATE

So scoping a credential for krateo-saas, which is what demo needs, would cut the credential for the
17 agentiko components if done the same way on 057.

WHAT THIS GATE CHECKS, and it is deliberately narrow: when `registries[]` is non-empty in
chart/values.yaml, every repo that a PINNED component actually uses and that is KNOWN to require
authentication must have an entry whose `repo` matches it verbatim. A repo nobody pins is not a
finding, and a public repo needs no credential.

WHAT IT CANNOT DO. It cannot tell whether a registry requires authentication -- that is a property
of the registry, not of this repo. So the authenticated set is a DECLARED list below, and a new
private repo must be added to it deliberately. The gate's value is that it refuses to let a
`registries[]` entry be added without covering what is already known to need one; it cannot discover
a newly-private registry on its own, and says so rather than implying otherwise.

It also does not check the live cluster. A CR can carry its own `registryAuth`, and a cluster in
global mode with an entry added by hand is beyond anything this file can see.
"""
import json
import pathlib
import sys

import yaml

VALUES = pathlib.Path("chart/values.yaml")
PINS = pathlib.Path("chart/files/component-pins.yaml")
DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"

# Repos KNOWN to require authentication. A new private repo must be added here deliberately --
# the gate cannot discover that a registry has become private.
AUTHENTICATED_REPOS = {
    "oci://ghcr.io/krateo-agentiko/charts":            "agent charts, private BY DESIGN",
    "oci://ghcr.io/krateo-platformops/krateo-saas":    "saas charts, private (verified via the "
                                                       "authenticated packages API 2026-11-22)",
}


def load_pins():
    d = yaml.safe_load(PINS.read_text(encoding="utf-8"))
    items = d if isinstance(d, list) else (d.get("components") or d.get("pins") or [])
    return [c for c in items if isinstance(c, dict) and c.get("name")]


def repos_in_use(pins):
    """Every effective repo a pinned component pulls from, with the components that use it."""
    out = {}
    for c in pins:
        r = c.get("repo") or DEFAULT_REPO
        out.setdefault(r, []).append(c["name"])
    return out


def main() -> int:
    for p in (VALUES, PINS):
        if not p.is_file():
            print("::error::%s not found -- run from the repo root" % p)
            return 1
    vals = yaml.safe_load(VALUES.read_text(encoding="utf-8")) or {}
    ra = (vals.get("registryAuth") or {})
    regs = ra.get("registries") or []
    pins = load_pins()
    used = repos_in_use(pins)

    print("  registryAuth.enabled:   %r" % ra.get("enabled"))
    print("  registryAuth.registries: %d entr%s" % (len(regs), "y" if len(regs) == 1 else "ies"))
    print("  repos used by pinned components: %d" % len(used))
    for r in sorted(used):
        mark = "AUTH" if r in AUTHENTICATED_REPOS else "    "
        print("    %-4s %-50s %d component(s)" % (mark, r, len(used[r])))

    if not pins:
        print("::error::parsed no pins -- a gate that inspects nothing must FAIL, because "
              "'every repo covered' is indistinguishable from 'no repos looked at'.")
        return 1

    if not regs:
        print("%s: OK -- registries[] is empty, so the global `enabled` path applies uniformly "
              "and no repo can be silently uncovered." % pathlib.Path(__file__).stem)
        return 0

    # registries[] is non-empty: the global credential is now UNREACHABLE.
    covered = {str(r.get("repo")) for r in regs if r.get("repo")}
    needed = {r for r in used if r in AUTHENTICATED_REPOS}
    missing = sorted(needed - covered)
    extra = sorted(covered - set(used))

    bad = 0
    if missing:
        print("::error::registryAuth.registries[] is non-empty, which makes the GLOBAL credential "
              "unreachable (inst.chartExtras is `if registries` / ELSE IF `enabled`), and %d repo(s) "
              "that pinned components pull from and that need authentication have NO entry:"
              % len(missing))
        for r in missing:
            print("::error::  %-50s %d component(s) -- %s"
                  % (r, len(used[r]), AUTHENTICATED_REPOS[r]))
        print("::error::Those components' charts will fail to pull. Add a registries[] entry whose "
              "`repo` matches VERBATIM (it is compared as a string, not normalised), or empty "
              "registries[] and use the global credential.")
        bad += 1

    if extra:
        print("::error::%d registries[] entr%s name a repo NO pinned component uses: %s"
              % (len(extra), "y" if len(extra) == 1 else "ies", ", ".join(extra)))
        print("::error::That is not harmful on its own, but it is dead configuration and usually "
              "means the repo string does not match the pin's -- the comparison is verbatim.")
        bad += 1

    if bad:
        return 1
    print("%s: OK -- %d registries[] entr%s cover every authenticated repo in use (%d of %d repos "
          "need one)" % (pathlib.Path(__file__).stem, len(regs),
                         "y" if len(regs) == 1 else "ies", len(needed), len(used)))
    return 0


def self_test() -> int:
    """The cases that must FAIL are the point; a gate proving only that it passes today is useless."""
    bad = 0
    pins = [
        {"name": "a"},                                                   # default repo
        {"name": "b", "repo": "oci://ghcr.io/krateo-agentiko/charts"},   # authenticated
        {"name": "c", "repo": "oci://ghcr.io/krateo-platformops/krateo-saas"},  # authenticated
        {"name": "d", "repo": "oci://ghcr.io/public/charts"},            # public
    ]
    used = repos_in_use(pins)
    ok = sorted(used) == sorted([DEFAULT_REPO, "oci://ghcr.io/krateo-agentiko/charts",
                                 "oci://ghcr.io/krateo-platformops/krateo-saas",
                                 "oci://ghcr.io/public/charts"])
    bad += 0 if ok else 1
    print("  %-4s repos_in_use collects the default AND each override" % ("ok" if ok else "FAIL"))

    needed = {r for r in used if r in AUTHENTICATED_REPOS}
    ok = needed == {"oci://ghcr.io/krateo-agentiko/charts",
                    "oci://ghcr.io/krateo-platformops/krateo-saas"}
    bad += 0 if ok else 1
    print("  %-4s only the DECLARED authenticated repos are required" % ("ok" if ok else "FAIL"))

    cases = [
        ({"oci://ghcr.io/krateo-platformops/krateo-saas"}, 1,
         "one entry covering saas LEAVES agentiko uncovered -- the 057 foot-gun"),
        ({"oci://ghcr.io/krateo-agentiko/charts",
          "oci://ghcr.io/krateo-platformops/krateo-saas"}, 0,
         "both covered -> no missing"),
        (set(), 2, "an entry list covering neither leaves both missing"),
    ]
    for covered, want, label in cases:
        got = len(needed - covered)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-4s %s (missing=%d)" % ("ok" if ok else "FAIL", label, got))

    # a public repo must never be required
    ok = "oci://ghcr.io/public/charts" not in needed
    bad += 0 if ok else 1
    print("  %-4s a PUBLIC repo in use is never required to have an entry" % ("ok" if ok else "FAIL"))

    # and the real values must pass, or the gate cannot be merged
    if VALUES.is_file() and PINS.is_file():
        v = yaml.safe_load(VALUES.read_text(encoding="utf-8")) or {}
        regs = ((v.get("registryAuth") or {}).get("registries") or [])
        print("  %-4s the REAL values.yaml has %d registries[] entr%s, so the gate's non-empty "
              "branch is %s today"
              % ("ok", len(regs), "y" if len(regs) == 1 else "ies",
                 "exercised" if regs else "NOT exercised -- the self-test above is its only cover"))

    print("self-test: %d case(s), %d failure(s)" % (len(cases) + 3, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
