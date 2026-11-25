#!/usr/bin/env python3
"""Refuse to cut a tag that carries more than the author declared it carries.

WHY THIS EXISTS. Every pin gate in this repo asserts that a PULL REQUEST changes exactly the pins it
claims, and all of them were green on 2026-11-25. Then six of those PRs merged while a peer held the
cluster, and the next tag would have carried FIVE pin changes and two new templates while three
sessions had been told, three times, that it was "otel-collector-deployment 0.3.24 alone":

    builder-gate              1.8.85 -> 1.8.97
    frontend                  1.8.85 -> 1.8.97
    frontend-crd              1.8.85 -> 1.8.97
    krateo-observability      0.1.55 -> 0.1.59      <- a ClickHouse restart and a password rotation
    otel-collector-deployment 0.3.23 -> 0.3.24      <- the only one declared

A ROLL IS NOT A MERGE, AND A TAG IS NOT A PR. A tag is cut from a COMMIT, and `main` accumulates:
"one pin per PR" and "one pin per tag" are different properties, and the second had no gate at all.
I had the right rule and was applying it at the wrong boundary. A peer reading my own status message
caught it by asking whether the tag was really scoped -- and a cut tag can never be re-cut, so there
was nothing between that question and the cluster.

WHAT IT COMPARES. Not the merge base, and not the previous tag in the repo: **the tag that is
actually DEPLOYED on the target cluster**, read from the live helm release. A tag's scope is only
meaningful relative to what the cluster is running -- the repo's newest tag may never have been
rolled anywhere, and comparing against it would declare a roll empty that is in fact five versions
deep.

USAGE
    check-tag-scope.py --context <kube-context> --release installer \\
        --declare otel-collector-deployment [--declare ...] [--allow-template-changes]

Exit 0 only when the set of pins differing between the deployed tag and HEAD is EXACTLY the declared
set. It names every undeclared difference, and it also fails when a DECLARED pin does not move --
because a tag that does not carry what it claims is as wrong as one that carries more.
"""
import argparse
import io
import json
import os
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PINS = "chart/files/component-pins.yaml"
TEMPLATE_PATHS = ("chart/templates/", "chart/values.yaml", "chart/values.schema.json")


def _pins(text):
    doc = yaml.safe_load(text) or {}
    entries = doc.get("components") if isinstance(doc, dict) else doc
    return {e["name"]: e.get("version") for e in (entries or []) if e.get("name")}


def deployed_chart_version(context, release):
    """The installer chart version running on the cluster, from the live helm release.

    `helm list` has no --request-timeout flag; passing one makes it exit non-zero with "unknown
    flag", which an unguarded caller reads as "no releases found". Measured twice today.
    """
    run = subprocess.run(["helm", "list", "-A", "--kube-context", context, "-o", "json"],
                         capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        return None, (run.stderr or run.stdout or "").strip()[:160]
    try:
        rows = json.loads(run.stdout or "[]")
    except ValueError as exc:
        return None, "unparseable helm output: %s" % str(exc)[:80]
    for row in rows:
        if row.get("name") == release:
            chart = row.get("chart") or ""
            # "installer-0.3.707" -> "0.3.707"
            return chart.rsplit("-", 1)[-1] if "-" in chart else None, None
    return None, "release %r not found among %d release(s)" % (release, len(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--release", default="installer")
    ap.add_argument("--declare", action="append", default=[],
                    help="a component this tag is allowed to move; repeatable")
    ap.add_argument("--allow-template-changes", action="store_true",
                    help="permit chart template/values/schema changes in this tag")
    ap.add_argument("--ref", default="HEAD", help="the commit a tag would be cut from")
    args = ap.parse_args()

    version, err = deployed_chart_version(args.context, args.release)
    if err or not version:
        print("::error::cannot read the deployed chart version from %s/%s: %s"
              % (args.context, args.release, err or "no chart version in the release"))
        print("::error::WITHOUT IT THERE IS NOTHING TO COMPARE AGAINST. This is a refusal, not a "
              "skip: a scope check that cannot see the cluster would pass every tag.")
        return 2

    tag = version
    show = subprocess.run(["git", "show", "%s^{commit}:%s" % (tag, PINS)],
                          capture_output=True, text=True)
    if show.returncode != 0:
        print("::error::the deployed tag %s is not in this clone (%s). Fetch tags; do not guess."
              % (tag, (show.stderr or "").strip()[:90]))
        return 2
    deployed = _pins(show.stdout)

    head = subprocess.run(["git", "show", "%s:%s" % (args.ref, PINS)],
                          capture_output=True, text=True)
    candidate = _pins(head.stdout if head.returncode == 0
                      else io.open(os.path.join(HERE, PINS), encoding="utf-8").read())

    moved = {k: (deployed.get(k), candidate.get(k))
             for k in set(deployed) | set(candidate)
             if deployed.get(k) != candidate.get(k)}
    declared = set(args.declare)
    undeclared = {k: v for k, v in moved.items() if k not in declared}
    stationary = sorted(declared - set(moved))

    print("  deployed on %s: tag %s" % (args.context.split("_")[-1], tag))
    print("  candidate ref: %s" % args.ref)
    print("  pins differing: %d     declared: %d" % (len(moved), len(declared)))

    problems = []
    for name in sorted(undeclared):
        a, b = undeclared[name]
        problems.append("UNDECLARED: %s %s -> %s would roll in this tag" % (name, a, b))
    for name in stationary:
        problems.append("DECLARED BUT STATIONARY: %s does not move between %s and %s. A tag that "
                        "does not carry what it claims is as wrong as one that carries more."
                        % (name, tag, args.ref))

    if not args.allow_template_changes:
        diff = subprocess.run(["git", "diff", "--name-only", "%s^{commit}" % tag, args.ref, "--"]
                              + list(TEMPLATE_PATHS), capture_output=True, text=True)
        files = [f for f in (diff.stdout or "").split("\n") if f.strip()]
        for f in files:
            problems.append("TEMPLATE CHANGE: %s differs from %s. Pass "
                            "--allow-template-changes if the tag is meant to carry it." % (f, tag))

    print()
    if problems:
        for p in problems:
            print("::error::%s" % p)
        print("::error::check-tag-scope: this tag carries more or less than declared. "
              "A RELEASED TAG IS NEVER RE-CUT, so this is the last point at which it is free.")
        return 1
    print("check-tag-scope: OK -- %d pin(s) move and all %d were declared%s"
          % (len(moved), len(declared),
             "" if args.allow_template_changes else "; no template/values/schema changes"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
