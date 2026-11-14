#!/usr/bin/env python3
"""Refuse a pin set where members of one RELEASE LINE carry different versions.

This is NOT the `<x>` / `<x>-crd` rule -- that is check-pin-crd-lockstep.py, which compares a
component against its own CRD chart and tolerates independent version lines (see
.github/pin-crd-independent-lines.yaml). This gate is for groups whose members are the SAME
ARTIFACT released together, where "the versions differ" has no valid reading at all.

WHY IT EXISTS: `controller-render-service` IS oasgen-render.

Its chart runs the oasgen-provider IMAGE with a different entrypoint -- measured on krateo-057,
2026-11-12:

    controller-render-service   ghcr.io/krateo-platformops/oasgen-provider:0.48.0
                                command: [/bin/oasgen-render]
    oasgen-provider             ghcr.io/krateo-platformops/oasgen-provider:0.61.0

It was pinned as an independent component and drifted 13 versions on 057, 11 on krateo-demo. The
chart's own values.yaml claims "same image, same tag, different command - so the renderer can never
drift from the controller", which is true of the chart and FALSE of this repo's delivery model: a
description is not a constraint, so here is the constraint.

THE CONSEQUENCE IS USER-VISIBLE, not merely a broken measuring instrument.
`crdgen.GenerateWithWarnings` first exists in plumbing v1.26.1 and oasgen only began CALLING it at
0.53.0 (references: 0.48.0 -> 0, 0.53.0 -> 3, 0.61.0 -> 3). The 0.48.0 renderer calls plain
`Generate`, which has NO WARNING CHANNEL. The Controller Builder posts drafts to this service's
POST /render, so at 0.48.0 an author gets a CLEAN PREVIEW for a document the controller will warn
about -- a dropped constraint, a vendor-shape conflict, a schema that silently will not appear.
frontend confirmed their UI shows warnings distinctly from errors and arms publish only on zero
problems, so moving the renderer forward is pure gain.

NOT the reason, recorded because I asserted it and it was FALSE: I first justified this by the
`not`-subschema transpile change between plumbing v1.33.0 and v1.40.0. oasgen 0.61.0 pins v1.33.0,
BELOW v1.40.0, so the controller does not carry that change either; it arrives only with the
unreleased v1.60.0 bump. The real divergence span is v1.14.2 -> v1.33.0. oasgen-provider caught this
before it became a gate's rationale, which is why the rationale is written down at all.

The frontend line (frontend + frontend-crd + builder-gate) is declared here too. It was enforced by
hand until now and it has slipped: check-no-silent-downgrade.py records that installer#727 would
have reverted all three 1.8.32 -> 1.8.28 together.

PROVEN TO DETECT, not merely to pass (`--self-test`): it is run against the pin set as it stood
BEFORE this change, where oasgen-provider was 0.61.0 and controller-render-service 0.48.0, and must
refuse it.
"""
import os
import sys

import yaml

PINS = os.environ.get("PINS", "chart/files/component-pins.yaml")
CHART = os.environ.get("CHART", "chart/Chart.yaml")
DECL = os.environ.get("DECL", ".github/pin-version-lines.yaml")


def pins_from(text):
    doc = yaml.safe_load(text)
    comps = doc.get("components") if isinstance(doc, dict) else doc
    if not isinstance(comps, list):
        return None
    out = {}
    for c in comps:
        if isinstance(c, dict) and c.get("name"):
            out[str(c["name"])] = c.get("version")
    return out or None


def deps_from(text):
    """name -> version for chart/Chart.yaml's dependencies.

    WHY THIS FILE TOO. Not every component this installer ships is a PIN. core-provider and
    core-provider-crds are bootstrap SUBCHARTS, declared in chart/Chart.yaml's `dependencies`, and
    they never appear in component-pins.yaml. So a gate that reads only the pins file cannot see
    them -- and their pairing is MANDATORY in a way that fails silently:

        The grace record is `status.servedVersionsPendingPrune` on the CompositionDefinition CRD,
        first present in core-provider-crds 2.13.85. A newer core-provider binary running against an
        older CRDs chart has that record PRUNED BY THE APISERVER on every write, so the grace never
        comes due and nothing is ever pruned.

    That is the same shape as our own componentValues pruning: an undeclared field vanishes while
    the write reports success. A skew here is therefore invisible at the moment it is introduced and
    surfaces later as "the prune stopped working" -- which is exactly how krateo-057 was diagnosed
    wrongly on 2026-11-13 before core-provider measured it.
    """
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return None
    out = {}
    for d in (doc.get("dependencies") or []):
        if isinstance(d, dict) and d.get("name"):
            out[str(d["name"])] = d.get("version")
    return out


def versions_from(pins_text, chart_text):
    """(name -> version, name -> origin, collisions). Origin is for the failure message: a reader
    needs to know WHICH FILE to edit, and the two are edited differently."""
    pins = pins_from(pins_text)
    if pins is None:
        return None, None, None
    deps = deps_from(chart_text) if chart_text is not None else {}
    if deps is None:
        return None, None, None
    collisions = sorted(set(pins) & set(deps))
    merged = dict(pins)
    origin = {k: PINS for k in pins}
    for k, v in deps.items():
        merged[k] = v
        origin[k] = CHART
    return merged, origin, collisions


def evaluate(pins, lines, origin=None):
    """Pure function of (name->version, declared lines) so the self-test can feed it history."""
    findings = []
    for line in lines:
        name = line.get("name", "<unnamed>")
        members = line.get("members") or []
        if len(members) < 2:
            findings.append(f"line {name!r}: needs at least 2 members, has {len(members)}")
            continue
        missing = [m for m in members if m not in pins]
        if missing:
            # A member that is not pinned at all is not a skew -- but it IS a stale declaration,
            # and a declaration that silently covers nothing is worse than no declaration.
            findings.append(
                f"line {name!r}: declared member(s) {missing} are not in {PINS}. Either the pin was "
                f"removed (update this line) or the name is wrong (a typo'd member makes the whole "
                f"line vacuous)."
            )
            continue
        seen = {m: pins[m] for m in members}
        if len(set(seen.values())) > 1:
            where = ""
            if origin:
                files = sorted({origin.get(m, "?") for m in members})
                where = f" [declared in {', '.join(files)}]"
            findings.append(
                f"line {name!r}: members disagree -- " +
                ", ".join(f"{m}={v}" for m, v in seen.items()) + where +
                f". {line.get('reason', '').strip() or 'These must carry the same version.'}"
            )
    return findings


def self_test():
    """Run against the state this change fixes. A gate that only ever passes is untested."""
    ok = True
    decl = yaml.safe_load(open(DECL, encoding="utf-8"))["lines"]

    # 1. the real pre-change pin set, EMBEDDED rather than read from a ref.
    #
    #    This case first read `origin/main`, and that was wrong in a way worth recording:
    #    the fixture was the very state this gate's own change fixed, so the moment 0.3.611
    #    merged, main no longer contained the skew, case 1 began reporting
    #    "0 skew finding(s) -> ACCEPTED, which is wrong", and the job failed on EVERY
    #    subsequent PR. A self-test anchored to a MOVING ref tests a different thing each
    #    day; anchored to its own fix, it is guaranteed to break. The values below are the
    #    oasgen line exactly as it stood at e586525 (be0b9d7^, the commit before #855), and
    #    they can never drift because nothing can edit history into this string.
    #
    #    It stays real YAML in the pin file's own shape, so pins_from() is still exercised
    #    on text of the kind it must parse, rather than on a hand-built dict.
    INCIDENT = """
components:
  - name: oasgen-provider
    version: 0.61.0
  - name: oasgen-provider-crd
    version: 0.61.0
  - name: controller-render-service
    version: 0.48.0
"""
    p = pins_from(INCIDENT)
    f = evaluate(p, decl)
    hit = [x for x in f if "oasgen" in x and "disagree" in x]
    print(f"{'ok  ' if hit else 'FAIL'}  e586525 oasgen line (renderer "
          f"{p.get('controller-render-service')} vs controller {p.get('oasgen-provider')}): "
          f"{len(hit)} skew finding(s) -> "
          f"{'refused' if hit else 'ACCEPTED, which is wrong'}")
    if not hit:
        ok = False

    # 1b. a real IN-STEP line from the same commit must be accepted, so case 1 is not
    #     passing merely because the gate refuses everything it is shown.
    AGREED = """
components:
  - name: frontend
    version: 1.8.34
  - name: frontend-crd
    version: 1.8.34
  - name: builder-gate
    version: 1.8.34
"""
    fa = [x for x in evaluate(pins_from(AGREED), decl) if "frontend" in x and "disagree" in x]
    print(f"{'ok  ' if not fa else 'FAIL'}  e586525 frontend line (all 1.8.34): "
          f"{len(fa)} finding(s) -> {'accepted' if not fa else fa}")
    if fa:
        ok = False

    # 2. the worktree, which must be clean
    f = evaluate(pins_from(open(PINS, encoding="utf-8").read()), decl)
    print(f"{'ok  ' if not f else 'FAIL'}  worktree: {len(f)} finding(s) -> "
          f"{'accepted' if not f else f}")
    if f:
        ok = False

    # 3. a synthetic skew must be caught, and a synthetic agreement must not be
    synth = {"a": "1.0.0", "b": "1.0.0", "c": "2.0.0"}
    line = [{"name": "t", "members": ["a", "b", "c"], "reason": "synthetic"}]
    bad = evaluate(synth, line)
    good = evaluate({"a": "1.0.0", "b": "1.0.0", "c": "1.0.0"}, line)
    print(f"{'ok  ' if len(bad)==1 else 'FAIL'}  synthetic skew detected: {len(bad)} (want 1)")
    print(f"{'ok  ' if not good else 'FAIL'}  synthetic agreement accepted: {len(good)} (want 0)")
    # 3b. a Chart.yaml DEPENDENCY skew must be caught, and agreement accepted. This is the case
    #     the gate was blind to before 2026-11-14: core-provider lives in Chart.yaml, not the pins.
    CHART_SKEW = """
dependencies:
  - name: core-provider
    version: 2.13.95
  - name: core-provider-crds
    version: 2.13.82
"""
    CHART_OK = """
dependencies:
  - name: core-provider
    version: 2.13.95
  - name: core-provider-crds
    version: 2.13.95
"""
    cpline = [{"name": "core-provider", "members": ["core-provider", "core-provider-crds"],
               "reason": "synthetic"}]
    # NOTE: pins_from returns None for an EMPTY components list, so these fixtures carry one
    # unrelated pin. The first draft used "components: []", versions_from returned None, and the
    # guard below emitted a placeholder finding -- which made the SKEW case pass for the wrong
    # reason (one placeholder == one expected finding) while the agreement case failed. The
    # "failure names Chart.yaml" assertion is what exposed it; without that third check the false
    # pass would have shipped.
    SOME_PINS = "components:\n  - name: unrelated\n    version: 1.0.0\n"
    v_bad, o_bad, c_bad = versions_from(SOME_PINS, CHART_SKEW)
    v_ok, o_ok, c_ok = versions_from(SOME_PINS, CHART_OK)
    assert v_bad is not None and v_ok is not None, "fixture parse failed; the cases below are vacuous"
    fb = evaluate(v_bad, cpline, o_bad)
    fo = evaluate(v_ok, cpline, o_ok)
    print(f"{'ok  ' if len(fb)==1 else 'FAIL'}  Chart.yaml dep skew detected "
          f"(2.13.95 vs 2.13.82): {len(fb)} (want 1)")
    print(f"{'ok  ' if not fo else 'FAIL'}  Chart.yaml dep agreement accepted: {len(fo)} (want 0)")
    if len(fb) != 1 or fo:
        ok = False
    # the failure message must name Chart.yaml, so a reader edits the right file
    named = fb and CHART in fb[0]
    print(f"{'ok  ' if named else 'FAIL'}  failure names {CHART}: {named}")
    if not named:
        ok = False
    # 3c. a name in BOTH sources is ambiguous and must be reported
    _, _, coll = versions_from("components:\n  - name: core-provider\n    version: 1.0.0\n"
                               "  - name: unrelated\n    version: 1.0.0\n", CHART_OK)
    print(f"{'ok  ' if coll==['core-provider'] else 'FAIL'}  pins/Chart.yaml collision reported: {coll}")
    if coll != ["core-provider"]:
        ok = False

    # 4. a typo'd member must not pass silently
    typo = evaluate(synth, [{"name": "t", "members": ["a", "nosuch"], "reason": "x"}])
    print(f"{'ok  ' if len(typo)==1 else 'FAIL'}  typo'd member refused: {len(typo)} (want 1)")
    if len(bad) != 1 or good or len(typo) != 1:
        ok = False
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    try:
        decl = yaml.safe_load(open(DECL, encoding="utf-8"))
    except FileNotFoundError:
        print(f"{DECL} is missing -- the gate cannot run", file=sys.stderr)
        return 1
    lines = (decl or {}).get("lines") or []
    if not lines:
        print(f"{DECL} declares no lines -- nothing to check, which is probably a mistake",
              file=sys.stderr)
        return 1
    try:
        chart_text = open(CHART, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"{CHART} is missing -- a line may declare a Chart.yaml dependency, so this gate "
              f"cannot run without it", file=sys.stderr)
        return 1
    pins, origin, collisions = versions_from(open(PINS, encoding="utf-8").read(), chart_text)
    if pins is None:
        print(f"could not parse {PINS} or {CHART}", file=sys.stderr)
        return 1
    if collisions:
        # A name in BOTH files is ambiguous: the gate would silently prefer one, and a reader
        # fixing the other file would see no change in the verdict.
        print(f"::error::check-same-version-line: {collisions} appear in BOTH {PINS} and {CHART}. "
              f"A line member must have ONE source of truth; resolve the duplicate before this "
              f"gate can give a meaningful verdict.", file=sys.stderr)
        return 1
    findings = evaluate(pins, lines, origin)
    if not findings:
        n_pins = sum(1 for l in lines for m in (l.get("members") or []) if origin.get(m) == PINS)
        n_deps = sum(1 for l in lines for m in (l.get("members") or []) if origin.get(m) == CHART)
        print(f"ok: {len(lines)} release line(s), all members in step "
              f"({n_pins} pins + {n_deps} Chart.yaml dependencies checked)")
        return 0
    print(f"REFUSED: {len(findings)} release line(s) out of step\n")
    for f in findings:
        print(f"  - {f}\n")
    print(f"See {DECL} for why each line exists.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
