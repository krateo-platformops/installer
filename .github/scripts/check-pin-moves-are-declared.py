#!/usr/bin/env python3
"""Every component this branch moves must be NAMED in the PR that moves it.

WHY THIS GATE EXISTS. On 2026-11-16, PR #984 ("stage the two new ApiResolved rows") silently
carried three pin moves:

    oasgen-provider            0.71.0 -> 0.74.0
    oasgen-provider-crd        0.71.0 -> 0.74.0
    controller-render-service  0.71.0 -> 0.74.0

Cause: `git checkout -b <new>` without checking out main first, so the branch grew from the
PREVIOUS PR's HEAD. The PR described one file; its diff carried two commits.

AND EVERY EXISTING INSTRUMENT WAS RIGHT TO PASS:

    gh pr view                     MERGEABLE/CLEAN -- it WAS cleanly mergeable
    check-no-silent-downgrade      OK -- it looks for DOWNGRADES, and 0.71.0 -> 0.74.0 is an
                                   UPGRADE. The whole-file diff it performs is against the merge
                                   target, so it SAW the move and correctly did not object to it.
    check-pins-min-in-sync         OK -- both files moved together, so they were in sync
    the 42-gate sweep              42/42

So the defect was not a broken check. It was an ABSENT one: nothing asked whether a pin move was
INTENDED. "Is this a downgrade?" and "is this declared?" are different questions, and only the
first had an instrument. What actually caught it was noticing the PR listing said 3 files for a
1-file change -- a human glance, not a gate.

WHAT WOULD HAVE HAPPENED. oasgen 0.74.0 lands on main and the next tag carries it, bypassing
Diego's explicit decision that 0.74.0 waits for a consumer arm to report. Not overruled -- quietly
skipped, by git hygiene rather than by anyone's judgement. That is the failure mode worth a gate:
a decision is not reversed, it is BYPASSED, so no one sees a conflict to resolve.

THE RULE. For every component whose pin entry differs from the merge target, its name must appear
in the PR title, the PR body, or the HEAD commit's message. The HEAD commit specifically, not the
whole branch log -- a stacked commit always explains itself, which is exactly the case this gate
must refuse.

An intentional move is one sentence away from passing: name the component. A move nobody can name
is a move nobody decided.

    python3 .github/scripts/check-pin-moves-are-declared.py              -> check this branch
    python3 .github/scripts/check-pin-moves-are-declared.py --self-test  -> offline cases
"""
import json
import os
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required")

PIN = "chart/files/component-pins.min.yaml"
TARGET = os.environ.get("GITHUB_BASE_REF") or "main"


def _pins(ref):
    out = subprocess.run(["git", "show", "%s:%s" % (ref, PIN)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    doc = yaml.safe_load(out.stdout)
    comps = doc["components"] if isinstance(doc, dict) else doc
    return {c["name"]: c for c in comps}


def moved_components(base_ref, head_ref):
    base, head = _pins(base_ref), _pins(head_ref)
    if base is None or head is None:
        return None
    names = set()
    names |= set(head) - set(base)
    names |= set(base) - set(head)
    names |= {k for k in set(base) & set(head) if base[k] != head[k]}
    return names


def undeclared(moved, text, lines=None):
    """A component counts as declared if its name appears in `text`, or if it shares a VERSION
    LINE with a component that does.

    Substring, not token: `oasgen-provider-crd` is not satisfied by prose that only says
    `oasgen-provider`.

    THE VERSION-LINE RULE, and why it is not a loosening. Some charts MOVE AS ONE LINE and the
    chart already treats that as mandatory -- `portal` ships with sixteen page charts, and
    check-pin-crd-lockstep refuses a half-move. Requiring a PR to enumerate all seventeen would
    make the portal PR's body a list nobody reads, and the first response to a noisy gate is to
    stop reading its output.

    So: components moving between the SAME two versions form a line, and naming ANY member
    declares the line. Every member is still PRINTED, so the reader sees the full set.

    Measured on the real branches when this was written:
        pin-portal-1.8.129   17 components, ONE line 1.8.128 -> 1.8.129, `portal` named  -> PASS
        #984 (stacked)        3 components, one line 0.71.0 -> 0.74.0, NONE named        -> REFUSE

    The second is the case this gate exists for, and the rule does not weaken it: #984's problem
    was never that it under-enumerated a line, it was that it moved a line NOBODY MENTIONED. A PR
    that names one oasgen component and moves all three is fine, because moving one without the
    others is a DIFFERENT defect with its own gate.
    """
    low = (text or "").lower()
    named = {n for n in moved if n.lower() in low}
    if lines:
        for group in lines.values():
            if group & named:
                named |= group
    return sorted(n for n in moved if n not in named)


def version_lines(base_ref, head_ref):
    """Group the moved components by their (from, to) version pair. A pair shared by two or more
    components is a line; a solo move is its own group, which the caller treats identically."""
    base, head = _pins(base_ref), _pins(head_ref)
    if base is None or head is None:
        return {}
    groups = {}
    for n in set(base) & set(head):
        if base[n] == head[n]:
            continue
        key = (base[n].get("version"), head[n].get("version"))
        if key[0] == key[1]:
            continue  # a non-version change (apiRef, requiresApiGroups) is never a line
        groups.setdefault(key, set()).add(n)
    return groups


def _self_test():
    cases = [
        # (moved, declared text, expect undeclared)
        ({"kyverno"}, "pin kyverno 3.9.2", []),
        ({"krateo-slo-rollup"}, "pin krateo-slo-rollup 0.1.2: the default Secret", []),
        # THE REAL #984. Its own message is about ready-reasons and names no component.
        ({"oasgen-provider", "oasgen-provider-crd", "controller-render-service"},
         "ready-reasons: stage the two new ApiResolved rows, and repair a note YAML had eaten",
         ["controller-render-service", "oasgen-provider", "oasgen-provider-crd"]),
        # #983 itself: names the line but ALSO each component, so it passes.
        ({"oasgen-provider", "oasgen-provider-crd", "controller-render-service"},
         "pin the oasgen line 0.71.0 -> 0.74.0: oasgen-provider, oasgen-provider-crd and "
         "controller-render-service move together",
         []),
        # FALSIFIER: naming the line ALONE is not enough. This is the case that makes the gate
        # worth having rather than a formality -- "the oasgen line" reads like a declaration.
        ({"oasgen-provider", "oasgen-provider-crd", "controller-render-service"},
         "pin the oasgen line 0.71.0 -> 0.74.0",
         ["controller-render-service", "oasgen-provider", "oasgen-provider-crd"]),
        # FALSIFIER: a prefix must not satisfy a longer name.
        ({"oasgen-provider-crd"}, "pin oasgen-provider 0.74.0", ["oasgen-provider-crd"]),
        # the 17-component portal line, named as a line AND enumerated
        ({"portal", "portal-agents"}, "pin the portal line: portal, portal-agents", []),
        # no movement at all -> nothing to declare
        (set(), "any text", []),
    ]
    # THE VERSION-LINE CASES, on the real shapes measured from the branches.
    PORTAL = {"portal", "portal-agents", "alerts-page", "audit-page", "blueprints-page",
              "builders-page", "clusters-page", "compositions-page", "dashboard-page",
              "incidents-page", "marketplace-page", "observability-page", "resources-page",
              "reviews-page", "search-page", "service-desk-page", "settings-page"}
    OASGEN = {"oasgen-provider", "oasgen-provider-crd", "controller-render-service"}
    line_cases = [
        # the real #979: 17 components on ONE line, only `portal` named -> the line is declared
        (PORTAL, "pin the portal line to 1.8.129", {("1.8.128", "1.8.129"): PORTAL}, []),
        # the real #984: the SAME line shape, and NOTHING names it -> still refused
        (OASGEN, "ready-reasons: stage the two new ApiResolved rows",
         {("0.71.0", "0.74.0"): OASGEN}, sorted(OASGEN)),
        # FALSIFIER: a line does NOT vouch for a component outside it. Two separate transitions,
        # one named -- the other must still be declared on its own.
        (PORTAL | {"kyverno"}, "pin the portal line to 1.8.129",
         {("1.8.128", "1.8.129"): PORTAL, ("3.8.0", "3.9.2"): {"kyverno"}}, ["kyverno"]),
        # FALSIFIER: a non-version change is never a line, so it cannot be vouched for.
        ({"identity-lifecycle", "portal"}, "pin the portal line to 1.8.129",
         {("1.8.128", "1.8.129"): {"portal"}}, ["identity-lifecycle"]),
    ]
    bad = 0
    for moved, text, lines, want in line_cases:
        got = undeclared(moved, text, lines)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-4s LINE moved=%-2d -> undeclared=%s%s"
              % ("ok" if ok else "FAIL", len(moved), got, "" if ok else "  want=%s" % want))
    for moved, text, want in cases:
        got = undeclared(moved, text)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-4s moved=%-2d -> undeclared=%s%s"
              % ("ok" if ok else "FAIL", len(moved), got, "" if ok else "  want=%s" % want))
    print("self-test: %d case(s), %d failure(s)" % (len(cases) + len(line_cases), bad))
    return 1 if bad else 0


def main():
    if "--self-test" in sys.argv:
        return _self_test()

    head = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    base = "origin/%s" % TARGET
    moved = moved_components(base, head)
    if moved is None:
        print("check-pin-moves-are-declared: SKIP -- cannot read %s at %s" % (PIN, base))
        return 0
    if not moved:
        print("check-pin-moves-are-declared: OK -- this branch moves no pin entry "
              "(control: %d components compared against %s)" % (len(_pins(head)), base))
        return 0

    # The declaration text: the PR's own title+body when CI provides it, plus the HEAD commit
    # message. Deliberately NOT the whole branch log -- a stacked commit always names its own
    # component, which would make this gate vacuous in the exact case it exists for.
    parts = [subprocess.run(["git", "log", "-1", "--format=%B", head],
                            capture_output=True, text=True).stdout]
    ev = os.environ.get("GITHUB_EVENT_PATH")
    if ev and os.path.exists(ev):
        try:
            with open(ev, encoding="utf-8") as fh:
                pr = (json.load(fh) or {}).get("pull_request") or {}
            parts += [pr.get("title") or "", pr.get("body") or ""]
        except Exception as exc:
            print("check-pin-moves-are-declared: could not read the PR event (%s); "
                  "falling back to the HEAD commit message alone" % exc)
    text = "\n".join(parts)

    lines = version_lines(base, head)
    missing = undeclared(moved, text, lines)
    multi = {k: v for k, v in lines.items() if len(v) > 1}
    if multi:
        for (fv, tv), group in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            print("    version line %s -> %s: %d components (%s)"
                  % (fv, tv, len(group), ", ".join(sorted(group)[:3]) + ("..." if len(group) > 3 else "")))
    print("check-pin-moves-are-declared: %d component(s) moved vs %s" % (len(moved), base))
    for n in sorted(moved):
        print("    %-28s %s" % (n, "DECLARED" if n not in missing else "*** NOT DECLARED ***"))
    if missing:
        print()
        print("REFUSED: this branch moves %d component(s) that nothing in the PR title, body or "
              "HEAD commit message names:" % len(missing))
        for n in missing:
            print("    %s" % n)
        print()
        print("If the move is intended, NAME IT -- one sentence in the PR body is enough, and a")
        print("reader of a pin PR needs to know which entries moved. If it is NOT intended, the")
        print("usual cause is a branch created from another PR's HEAD rather than from %s:" % TARGET)
        print("    git checkout %s && git pull && git checkout -b <name> && git cherry-pick <sha>" % TARGET)
        print()
        print("This gate exists because PR #984 moved the oasgen line without naming it, and")
        print("MERGEABLE/CLEAN, check-no-silent-downgrade and a 42-gate sweep all passed -- none")
        print("of them asks whether a move was INTENDED.")
        if not (ev and os.path.exists(ev)):
            print()
            print("RUNNING WITHOUT A PR EVENT, SO THIS MAY BE A FALSE REFUSAL. The declaration text")
            print("here is the HEAD COMMIT MESSAGE ALONE -- in CI the PR title and body are added,")
            print("and a component named there (or in an EARLIER commit on the branch) counts.")
            print("Measured when this gate was written: PR #980 is refused locally and PASSES in")
            print("CI, because its component is named in its title and in its first commit, not in")
            print("its HEAD commit. Check the PR's own title before treating this as a finding:")
            print("    gh pr view <n> --json title,body")
        return 1
    print("check-pin-moves-are-declared: OK -- every moved component is named")
    return 0


if __name__ == "__main__":
    sys.exit(main())
