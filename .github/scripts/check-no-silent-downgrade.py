#!/usr/bin/env python3
"""
Refuses a PR that LOWERS any pin it does not mention in its own diff.

THIS GATE EXISTS BECAUSE THE SAME PR BRANCH CARRIED A SILENT DOWNGRADE THREE TIMES IN ONE DAY,
2026-11-08, and all 30 CI checks passed every time.

`chart/files/component-pins.yaml` and `chart/Chart.yaml` are each ONE file listing EVERY pin. A
branch cut before some other pin moves still carries the OLD value for it, so merging that branch
silently reverts components the PR never mentions:

  #723 (frontend)      would have downgraded portal 1.8.95->1.8.94, authn 0.36.0->0.35.7 and
                       krateo-observability 0.1.32->0.1.31 on the live cluster.
  #723 again, after    would have downgraded core-provider 2.13.75->2.13.74 in Chart.yaml, undoing
  that was fixed       the leader-election test that 0.3.528 existed to run.
  #727 (observability) would have downgraded frontend/frontend-crd/builder-gate 1.8.32->1.8.28.

Nothing caught these but an ad-hoc assertion written by hand each time. A reviewer reading
"pin frontend 1.8.32" sees three extra lines in a 4000-line file and no CI signal.

WHAT HID THE SECOND ONE, and why this gate compares against the MERGE TARGET rather than a diff:
`git diff --stat origin/main...HEAD` reported ONLY component-pins.yaml. The THREE-DOT form shows
what the branch ADDED since the merge base and says NOTHING about what the base has moved on to, so
the reassuring diff was the wrong instrument.

PROVEN TO DETECT, not just to pass. Run against the three real cases:
  #723 at d0c4aeb      22 undeclared downgrades  -> refused (exit 1)
  #727 at 5b4f359       3 undeclared downgrades  -> refused (exit 1)
  the same, with the components named in PR_BODY -> accepted (exit 0)
  current main                                   -> accepted (exit 0)

TWENTY-TWO, on a PR where MY OWN careful hand-check had found THREE. I diffed the pins I thought
might be stale; the gate diffs all of them. It also caught identity-lifecycle 0.2.0->0.1.5 and the
entire 14-chart portal page set, none of which I had looked at.

KNOWN WEAKNESS, stated rather than hidden: "declared" means the component's name appears somewhere
in the PR title, body or commit messages. In control 1, `core-provider` counted as declared because
the commit message discussed core-provider in prose, while `core-provider-crds` did not. That is a
low bar on purpose -- the aim is that a revert is SAID, so a human has written the name -- but it is
a substring match, not a statement of intent, and prose about a component will satisfy it.

WHAT THIS GATE DOES NOT DO: it does not forbid a downgrade. A deliberate rollback is legitimate
(there is a recorded floor of core-provider 2.13.57 for exactly that reason). It forbids an
UNDECLARED one: put the component's name in the PR title or body, or in a `Downgrade:` trailer in a
commit message, and the gate accepts it. The point is that a revert must be SAID, not inherited.
"""
import json
import os
import re
import subprocess
import sys

import yaml

PINS = "chart/files/component-pins.yaml"
CHART = "chart/Chart.yaml"


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def at(ref, path):
    txt = sh("git", "show", f"{ref}:{path}") if ref else open(path, encoding="utf-8").read()
    return yaml.safe_load(txt) if txt else None


def pins(doc):
    if not doc:
        return {}
    return {c["name"]: str(c.get("version", "")) for c in (doc.get("components") or [])}


def deps(doc):
    if not doc:
        return {}
    return {d["name"]: str(d.get("version", "")) for d in (doc.get("dependencies") or [])}


def ver_tuple(v):
    """Compare semver-ish versions numerically; fall back to string order."""
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (-1,)


def declared_text():
    """Everything the PR said about itself: title, body, and commit subjects/bodies."""
    bits = []
    for k in ("PR_TITLE", "PR_BODY"):
        if os.environ.get(k):
            bits.append(os.environ[k])
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if ev and os.path.exists(ev):
        try:
            d = json.load(open(ev, encoding="utf-8"))
            pr = d.get("pull_request") or {}
            bits += [str(pr.get("title") or ""), str(pr.get("body") or "")]
        except Exception:
            pass
    base = os.environ.get("BASE_REF", "origin/main")
    log = sh("git", "log", "--format=%B", f"{base}..HEAD")
    if log:
        bits.append(log)
    return "\n".join(bits).lower()


def main():
    base = os.environ.get("BASE_REF", "origin/main")
    if sh("git", "rev-parse", "--verify", base) is None:
        print(f"check-no-silent-downgrade: SKIPPED (cannot resolve {base})")
        return 0

    before = {**pins(at(base, PINS)), **{f"dep:{k}": v for k, v in deps(at(base, CHART)).items()}}
    after = {**pins(at(None, PINS)), **{f"dep:{k}": v for k, v in deps(at(None, CHART)).items()}}

    # CONTROL: both sides must be non-empty, or a parse failure would read as "no downgrades".
    if not before or not after:
        print("::error::check-no-silent-downgrade: one side parsed EMPTY "
              f"(base={len(before)} head={len(after)}) — refusing to report a clean result")
        return 1
    print(f"  control: {len(before)} pins at {base}, {len(after)} at HEAD")

    said = declared_text()
    lowered, undeclared = [], []
    for name, old in sorted(before.items()):
        new = after.get(name)
        if new is None or new == old:
            continue
        if ver_tuple(new) < ver_tuple(old):
            bare = name[4:] if name.startswith("dep:") else name
            lowered.append((bare, old, new))
            if bare.lower() not in said:
                undeclared.append((bare, old, new))

    for bare, old, new in lowered:
        mark = "UNDECLARED" if (bare, old, new) in undeclared else "declared"
        print(f"  {'FAIL' if mark == 'UNDECLARED' else 'ok  '} {bare:<28} {old} -> {new}  [{mark}]")

    if undeclared:
        print("::error::check-no-silent-downgrade: %d pin(s) are LOWERED but never mentioned by this "
              "PR: %s. A branch cut before those pins moved carries their old values and reverts them "
              "on merge. Merge the base branch in (do NOT rebase a pushed branch), or name the "
              "component in the PR body if the rollback is deliberate."
              % (len(undeclared), ", ".join(n for n, _, _ in undeclared)))
        return 1

    print("check-no-silent-downgrade: OK — %d pins compared against %s, %d declared downgrade(s)"
          % (len(before), base, len(lowered)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
