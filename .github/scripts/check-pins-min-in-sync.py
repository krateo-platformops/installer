#!/usr/bin/env python3
"""CI guard: the generated data-only pins file matches the annotated one, and the chart reads it.

WHY THE GENERATED FILE EXISTS. helm embeds the entire chart in its release record, and a release
Secret's data may not exceed 1 MiB DECODED. krateo-057 hit that wall on 2026-11-14: revision 546 sat
at 1,047,684 bytes -- 892 from the ceiling -- and revision 547 was refused outright with

    Secret "sh.helm.release.v1.installer.v547" is invalid: data: Too long

chart/files/component-pins.yaml is 740,859 bytes of which 97.6% (722,800 bytes over 8,290 lines) is
comment prose, and only 18,054 bytes are data. So it is .helmignore'd and the chart reads a generated
data-only copy; measured, that takes the packaged archive from 519,968 to 246,583 bytes (-52.6%).

THE TRAP THIS GATE EXISTS FOR, which is worse than the problem it solves if left unguarded: edit a
pin in the ANNOTATED file, forget to regenerate the min file, and the chart renders the OLD pin while
the repo, every code review and every other pin gate show the NEW one. Nothing would be red. A
component would silently stay on its previous version. That is precisely the class of silent failure
the other gates in this directory exist to refuse, so this one is a HARD failure, never a warning.

IT ALSO GUARDS ITS OWN PREMISE. The saving only happens while (a) the chart's `.Files.Get` reads the
min file and (b) .helmignore excludes the annotated one. If either is reverted the budget silently
returns and nothing else would notice, so both are asserted here rather than assumed.

    python3 .github/scripts/check-pins-min-in-sync.py              -> check the repo
    python3 .github/scripts/check-pins-min-in-sync.py --self-test  -> offline cases + falsifiers
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hack"))

import yaml  # noqa: E402

ANNOTATED = "chart/files/component-pins.yaml"
GENERATED = "chart/files/component-pins.min.yaml"
HELPERS = "chart/templates/_helpers.tpl"
HELMIGNORE = "chart/.helmignore"

# Below this the saving is not worth the generated file, and a near-equal pair almost certainly means
# the strip silently stopped working rather than that the comments genuinely vanished.
MIN_SAVING_RATIO = 0.50


def _strip(text):
    """The same rule as hack/strip-pin-comments.py: drop full-line comments and blank lines."""
    return "\n".join(l for l in text.split("\n") if l.strip() and not l.strip().startswith("#")) + "\n"


def _components(doc):
    return doc.get("components") if isinstance(doc, dict) else doc


def errors(annotated, generated, helpers, helmignore):
    """Broken-rule sentences; [] when everything holds."""
    bad = []

    # 1. the generated file is EXACTLY what the stripper produces, byte for byte
    want = _strip(annotated)
    if generated != want:
        a, b = generated.split("\n"), want.split("\n")
        where = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        bad.append("%s is STALE or hand-edited: it differs from the strip of %s at line %d "
                   "(%d lines vs %d). Regenerate: python3 hack/strip-pin-comments.py %s -o %s"
                   % (GENERATED, ANNOTATED, where + 1, len(a), len(b), ANNOTATED, GENERATED))

    # 2. and it is SEMANTICALLY the same document -- byte equality above could in principle be met
    #    by a stripper bug that mangles both sides identically, so parse both independently.
    try:
        da, dg = yaml.safe_load(annotated), yaml.safe_load(generated)
    except Exception as e:
        bad.append("one of the two files does not parse as YAML: %s" % e)
        return bad
    ca, cg = _components(da), _components(dg)
    if ca is None or cg is None:
        bad.append("no component list in one of the files")
        return bad
    if len(ca) != len(cg):
        bad.append("component COUNT differs: annotated %d, generated %d" % (len(ca), len(cg)))
    elif [c.get("name") for c in ca] != [c.get("name") for c in cg]:
        bad.append("component NAMES or ORDER differ between the two files")
    else:
        for x, y in zip(ca, cg):
            if x != y:
                bad.append("component %r differs field-for-field between the two files" % x.get("name"))

    # 3. NON-VACUITY: if the annotated file has no comments there is nothing to strip, and this
    #    gate would pass while proving nothing about the saving it exists to protect.
    ncomment = sum(1 for l in annotated.split("\n") if l.strip().startswith("#"))
    if ncomment == 0:
        bad.append("the annotated file contains NO comments, so this gate is vacuous -- either the "
                   "comments were lost or the two files have been swapped")
    elif len(annotated) and 1 - (len(generated) / len(annotated)) < MIN_SAVING_RATIO:
        bad.append("the generated file is only %.1f%% smaller than the annotated one (want >=%.0f%%): "
                   "the strip has probably stopped working"
                   % (100 * (1 - len(generated) / len(annotated)), 100 * MIN_SAVING_RATIO))

    # 4. the premise: the chart must READ the generated file and IGNORE the annotated one
    if '.Files.Get "files/component-pins.min.yaml"' not in helpers:
        bad.append("%s does not read files/component-pins.min.yaml -- the generated file is then "
                   "dead weight and the annotated one is back in the package" % HELPERS)
    if '.Files.Get "files/component-pins.yaml"' in helpers:
        bad.append("%s still reads the ANNOTATED files/component-pins.yaml, which .helmignore "
                   "excludes from the package -- the chart would render with NO pins at all" % HELPERS)
    if not any(l.strip() == "files/component-pins.yaml" for l in helmignore.split("\n")):
        bad.append("%s does not exclude files/component-pins.yaml, so the 722,800 bytes of comment "
                   "prose are still packaged and the release-Secret ceiling is still in play" % HELMIGNORE)
    return bad


def _read(p):
    return io.open(p, encoding="utf-8").read()


CASES = [
    ("in sync", "# c\ncomponents:\n- name: a\n", "components:\n- name: a\n", True),
    ("stale generated: a version edit not regenerated",
     "components:\n- name: a\n  version: '2'\n", "components:\n- name: a\n  version: '1'\n", False),
    ("generated missing a component", "components:\n- name: a\n- name: b\n", "components:\n- name: a\n", False),
    ("generated has an EXTRA component", "components:\n- name: a\n", "components:\n- name: a\n- name: z\n", False),
    ("components reordered in the generated file",
     "components:\n- name: a\n- name: b\n", "components:\n- name: b\n- name: a\n", False),
    ("comments still present in the generated file",
     "# c\ncomponents:\n- name: a\n", "# c\ncomponents:\n- name: a\n", False),
]
GOOD_HELPERS = 'x {{- $file := .Files.Get "files/component-pins.min.yaml" | fromYaml -}} y'
GOOD_IGNORE = "tests/\nfiles/component-pins.yaml\n"


def self_test():
    fails = 0
    big_ann = "# note\n" * 40 + "components:\n- name: a\n"
    big_gen = "components:\n- name: a\n"
    for label, ann, gen, want_ok in CASES:
        # pad the annotated side so the saving-ratio rule is satisfied for the in-sync case
        a = ("# pad\n" * 40) + ann if want_ok else ann
        g = _strip(a) if want_ok else gen
        bad = errors(a, g, GOOD_HELPERS, GOOD_IGNORE)
        ok = not bad
        if ok != want_ok:
            print("FAIL  %-50s ok=%s want=%s  %s" % (label, ok, want_ok, bad[:1]))
            fails += 1
        else:
            print("ok    %-50s %s" % (label, "accepted" if ok else "refused: %s" % bad[0][:52]))

    # premise falsifiers
    for label, helpers, ignore in [
        ("helpers reads the ANNOTATED file", 'x .Files.Get "files/component-pins.yaml" y', GOOD_IGNORE),
        ("helpers reads neither", "x nothing y", GOOD_IGNORE),
        (".helmignore does not exclude the annotated file", GOOD_HELPERS, "tests/\n"),
    ]:
        bad = errors(big_ann, big_gen, helpers, ignore)
        if not bad:
            print("FAIL  falsifier: %s was ACCEPTED" % label)
            fails += 1
        else:
            print("ok    falsifier refused: %-44s %s" % (label, bad[0][:46]))

    # vacuity falsifier: no comments at all must be refused
    bad = errors("components:\n- name: a\n", "components:\n- name: a\n", GOOD_HELPERS, GOOD_IGNORE)
    if not bad:
        print("FAIL  falsifier: an annotated file with NO comments was accepted (vacuous gate)")
        fails += 1
    else:
        print("ok    falsifier refused: %-44s %s" % ("annotated file with no comments", bad[0][:46]))

    print("check-pins-min-in-sync: %s" % ("OK -- %d cases + 4 falsifiers" % len(CASES) if not fails
                                          else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    for p in (ANNOTATED, GENERATED, HELPERS, HELMIGNORE):
        if not os.path.exists(p):
            print("::error::%s is missing" % p)
            return 1
    bad = errors(_read(ANNOTATED), _read(GENERATED), _read(HELPERS), _read(HELMIGNORE))
    for b in bad:
        print("::error::check-pins-min-in-sync: %s" % b)
    if bad:
        return 1
    ann, gen = _read(ANNOTATED), _read(GENERATED)
    ncomment = sum(1 for l in ann.split("\n") if l.strip().startswith("#"))
    print("check-pins-min-in-sync: OK — %s (%s bytes, %d comment lines) strips to %s (%s bytes, "
          "-%.1f%%); the chart reads the generated copy and .helmignore excludes the annotated one"
          % (ANNOTATED, format(len(ann), ","), ncomment, GENERATED, format(len(gen), ","),
             100 * (1 - len(gen) / len(ann))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
