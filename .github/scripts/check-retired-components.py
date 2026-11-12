#!/usr/bin/env python3
"""
Refuses a PR that UNPINS a component without finishing the retirement.

Deleting a pin from chart/files/component-pins.yaml is only half of a retirement. A live Installer
CR ships its FULL spec.components list wholesale on every upgrade (self-bootstrap.yaml), and it
carries its own componentValues, so BOTH surfaces keep the retired name for at least one more
reconcile. Each one fails CLOSED, and each one has its own repair:

  SURFACE 1  spec.components still names it
             -> inst.componentsYaml's typo-guard calls `fail`:
                  components[<name>]: unknown name not in files/component-pins.yaml
                from withheld-components.yaml, which aborts the ENTIRE umbrella render
                (ChartRenderable=False) in both composition and bootstrap mode.
             -> repair: the name must be in chart/files/retired-components.yaml.

  SURFACE 2  componentValues.<name> is still set
             -> componentValues has additionalProperties:false, so helm refuses the values:
                  at '/componentValues': additional properties '<name>' not allowed
             -> repair: values.schema.json must KEEP declaring componentValues.<name> (a bare
                `{"type": "object"}` stub is enough: it validates, and with no properties and no
                x-kubernetes-preserve-unknown-fields the generated CRD prunes the contents, so a
                live CR drains the key to {} by itself) until no live CR carries it.

THE FILE-COMMENT RULE WAS NOT ENOUGH. files/retired-components.yaml has said "DELETE its pin AND
append its name here, in the SAME commit" since installer#144, and it has been broken twice:

  2026-11-04  incident-controller / incident-controller-crd. Detonated: krateo-demo recorded
              ChartRenderable=False, umbrella render aborted, demo patched by hand to recover.
  2026-11-12  krateo-autopilot, unpinned by 0.3.606 (#849) -- BOTH surfaces at once, and the
              values error MASKS the render error so fixing one reveals the other. krateo-057 had
              already self-bootstrapped the name out of spec.components; krateo-demo carried it in
              spec.components (no registerOnly -> the `fail` branch) and in componentValues
              (agents, knowledge). demo was frozen on 0.3.592 and so never wedged, but its
              catch-up roll would have been refused twice over.

Nothing in CI read that file before this gate: the only two matches for "retired" under .github/
and hack/ were unrelated prose in check-signing-key.py and check-rbac-stale-window.py.

Like check-no-silent-downgrade.py this compares against the MERGE TARGET, not a diff: the
three-dot form shows what the branch added since the merge base and says nothing about what the
base moved on to.

PROVEN TO DETECT, not just to pass (self-test below, `--self-test`). Run against BOTH real
incidents, so the gate is tested on the two commits that actually caused outages:
  f556563 -> 140776c   the real #849 (krateo-autopilot)   2 findings -> refused (exit 1)
  c5b5e23^ -> c5b5e23  the incident-controller cutover     2 findings -> refused (exit 1)
  f556563 -> worktree  with both repairs applied           0 findings -> accepted (exit 0)
  140776c -> 140776c   no unpin at all                     0 findings -> accepted (exit 0)
  + declaring "<name> ... drained" suppresses the surface-2 finding only.

WHAT THIS GATE DOES NOT DO: it cannot know whether a live CR has drained a key -- no CI job can.
For surface 2 it only enforces that the schema node is not dropped in the SAME change that unpins
the component. Dropping it later is legitimate cleanup, and the declaration bar below is how you
say you checked the clusters.

DECLARING A DRAIN: put the component's name AND the word "drained" in the PR title, body or a
commit message. Same deliberately low bar as check-no-silent-downgrade: the point is that a human
wrote the name next to the claim, not that CI verified it.
"""
import os
import re
import subprocess
import sys

import yaml

PINS = "chart/files/component-pins.yaml"
RETIRED = "chart/files/retired-components.yaml"
SCHEMA = "chart/values.schema.json"


def sh(*args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def read_at(ref, path):
    """File contents at `ref`, or from the worktree when ref is None."""
    if ref is None:
        try:
            return open(path, encoding="utf-8").read()
        except FileNotFoundError:
            return None
    return sh("git", "show", f"{ref}:{path}")


def pin_names(ref):
    txt = read_at(ref, PINS)
    if txt is None:
        return None
    doc = yaml.safe_load(txt)
    comps = doc.get("components") if isinstance(doc, dict) else doc
    if not isinstance(comps, list):
        return None
    out = set()
    for c in comps:
        if isinstance(c, dict) and c.get("name"):
            out.add(str(c["name"]))
    return out or None


def retired_names(ref):
    txt = read_at(ref, RETIRED)
    if txt is None:
        return set()
    doc = yaml.safe_load(txt) or {}
    return {str(n) for n in (doc.get("retired") or [])}


def componentvalues_keys(ref):
    """Declared componentValues property names. json is not imported at module scope on purpose:
    a schema that does not parse is a different failure, reported as such rather than crashing."""
    import json

    txt = read_at(ref, SCHEMA)
    if txt is None:
        return None
    try:
        s = json.loads(txt)
    except ValueError as e:
        print(f"{SCHEMA} at {ref or 'worktree'} does not parse: {e}", file=sys.stderr)
        return None
    node = ((s.get("properties") or {}).get("componentValues") or {}).get("properties") or {}
    return set(node)


def declared(text, name, word=None):
    """True when `name` appears in the PR prose (and `word` too, when given)."""
    low = text.lower()
    if name.lower() not in low:
        return False
    return word is None or word in low


def prose(base_ref):
    bits = [os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", "")]
    if base_ref:
        log = sh("git", "log", "--format=%B", f"{base_ref}..HEAD")
        if log:
            bits.append(log)
    return "\n".join(bits)


def evaluate(base_ref, head_ref, text):
    """The whole rule, as a pure function of two refs and the PR prose, so the self-test can feed
    it the real historical commits. Returns a list of finding strings."""
    base_pins = pin_names(base_ref)
    head_pins = pin_names(head_ref)
    if base_pins is None or head_pins is None:
        return [f"INSTRUMENT: could not parse {PINS} at base={base_ref} / head={head_ref}"]

    unpinned = sorted(base_pins - head_pins)
    if not unpinned:
        return []

    head_retired = retired_names(head_ref)
    base_cv = componentvalues_keys(base_ref)
    head_cv = componentvalues_keys(head_ref)
    if base_cv is None or head_cv is None:
        return [f"INSTRUMENT: could not read {SCHEMA} at base={base_ref} / head={head_ref}"]

    findings = []
    for name in unpinned:
        if name not in head_retired:
            findings.append(
                f"{name}: its pin is DELETED but it is not listed in {RETIRED}. Any cluster whose "
                f"CR still carries the name in spec.components aborts its ENTIRE umbrella render "
                f"with `components[{name}]: unknown name not in files/component-pins.yaml`. "
                f"Append `- {name}` to that file in THIS commit."
            )
        if name in base_cv and name not in head_cv:
            if declared(text, name, "drained"):
                continue
            findings.append(
                f"{name}: componentValues.{name} is removed from {SCHEMA} in the same change that "
                f"unpins it. componentValues sets additionalProperties:false, so every cluster whose "
                f"CR still sets that key is refused with `at '/componentValues': additional "
                f"properties '{name}' not allowed`. Keep the key declared (a bare "
                f'`{{\"type\": \"object\"}}` stub is enough; the generated CRD then prunes its '
                f"contents so a live CR drains it to {{}}), or say in the PR that {name} is "
                f"drained on every cluster."
            )
    return findings


def contradictions(head_ref):
    """A name both pinned and retired: the helper's pinned branch wins, so the allowlist entry is
    dead and reads as protection that is not there."""
    pins = pin_names(head_ref) or set()
    return sorted(pins & retired_names(head_ref))


def self_test():
    """Run the gate against the two real historical commits. Asserts it REFUSES the actual #849
    and ACCEPTS the fixed tree -- a gate that only ever passes has not been tested."""
    cases = [
        ("f556563", "140776c", "", 2, "the real #849 (krateo-autopilot): neither repair"),
        ("c5b5e23^", "c5b5e23", "", 2, "the 2026-11-04 incident-controller cutover"),
        ("f556563", None, "", 0, "worktree (fix applied)"),
        ("140776c", "140776c", "", 0, "no unpin at all"),
    ]
    ok = True
    for base, head, text, want, label in cases:
        if base and read_at(base, PINS) is None:
            print(f"SKIP  {label}: {base} not in this clone")
            continue
        got = evaluate(base, head, text)
        verdict = "ok" if len(got) == want else "FAIL"
        if len(got) != want:
            ok = False
        print(f"{verdict:4}  {label}: expected {want} findings, got {len(got)}")
        for g in got:
            print(f"        - {g.split(':')[0]}: {g.split(': ', 1)[1][:80]}...")
    # the declaration escape hatch must actually work
    got = evaluate("f556563", "140776c", "krateo-autopilot is drained on every cluster")
    n_decl = len([g for g in got if "componentValues" in g])
    print(f"{'ok' if n_decl == 0 else 'FAIL':4}  declaring a drain suppresses the surface-2 finding "
          f"(got {n_decl})")
    if n_decl:
        ok = False
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()

    base_ref = os.environ.get("BASE_REF") or "origin/main"
    findings = evaluate(base_ref, None, prose(base_ref))
    for n in contradictions(None):
        findings.append(
            f"{n}: listed in {RETIRED} while still pinned in {PINS}. The helper's pinned branch "
            f"wins, so that allowlist entry is dead. Remove one of the two."
        )
    if not findings:
        print(f"ok: no component is unpinned without its retirement finished (base {base_ref})")
        return 0
    print(f"REFUSED: {len(findings)} unfinished retirement(s) against {base_ref}\n")
    for f in findings:
        print(f"  - {f}\n")
    print(f"See the header of {RETIRED} for what a complete retirement looks like.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
