#!/usr/bin/env python3
"""Emit the DATA-ONLY form of an annotated pins file.

WHY THIS EXISTS. helm stores every release revision in a Secret, and a Secret's data may not exceed
1 MiB DECODED. On krateo-057 the installer's release record reached 1,047,684 bytes at revision 546
— 892 bytes of headroom — and revision 547 was refused outright:

    Secret "sh.helm.release.v1.installer.v547" is invalid: data: Too long

The release record embeds the whole chart, and the chart embeds files/component-pins.yaml. That file
is 740,859 bytes of which 97.6% (722,800 bytes over 8,290 lines) is comment prose and only 18,054
bytes are data. gzipped it is 274,262 bytes against 2,924 for the data alone, so the comments cost
~271 KB of a 1 MiB budget — 300x the headroom that was left.

WHAT IS AND IS NOT REMOVED. Full-line comments and blank lines go; every data line is copied BYTE
FOR BYTE, including any trailing `# ...` on it. Trailing comments are left deliberately: stripping
them means deciding whether a `#` inside a value is a comment, and that is a parser's job, not a
line filter's. They are a rounding error next to the 8,290 full-line ones.

THE OUTPUT IS NOT RE-EMITTED YAML. Round-tripping through a YAML dumper would reorder keys and
reformat values, so the diff against the source would stop being readable and any formatting the
pins rely on would be at the dumper's mercy. Copying the surviving lines keeps the data textually
identical to what a human authored.

    python3 hack/strip-pin-comments.py <in>            -> stdout
    python3 hack/strip-pin-comments.py <in> -o <out>   -> file
    python3 hack/strip-pin-comments.py --self-test     -> offline cases
"""
import io
import sys

import yaml


def strip(text):
    """Drop full-line comments and blank lines; copy every other line verbatim."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def components_of(doc):
    """The component list, however the file wraps it."""
    if isinstance(doc, dict):
        return doc.get("components")
    return doc


def check_lossless(original, stripped):
    """Broken-rule sentences; [] when the strip changed no data."""
    bad = []
    try:
        a = yaml.safe_load(original)
    except Exception as e:                                    # pragma: no cover - guarded by caller
        return ["the SOURCE does not parse as YAML: %s" % e]
    try:
        b = yaml.safe_load(stripped)
    except Exception as e:
        return ["the STRIPPED form does not parse as YAML: %s" % e]
    if a != b:
        bad.append("the stripped form parses to a DIFFERENT document than the source")
    ca, cb = components_of(a), components_of(b)
    if ca is None or cb is None:
        bad.append("no component list found (source=%r stripped=%r)" % (type(ca), type(cb)))
        return bad
    if len(ca) != len(cb):
        bad.append("component COUNT changed: %d -> %d" % (len(ca), len(cb)))
    na = [c.get("name") for c in ca if isinstance(c, dict)]
    nb = [c.get("name") for c in cb if isinstance(c, dict)]
    if na != nb:
        bad.append("the component NAMES or their ORDER changed")
    for x, y in zip(ca, cb):
        if x != y:
            bad.append("component %r differs field-for-field" % (x.get("name") if isinstance(x, dict) else x))
    return bad


SELF_TESTS = [
    ("a full-line comment goes", "# note\ncomponents:\n- name: a\n", "components:\n- name: a\n", True),
    ("a blank line goes", "components:\n\n- name: a\n", "components:\n- name: a\n", True),
    ("an indented comment goes", "components:\n- name: a\n  # why\n  version: '1'\n",
     "components:\n- name: a\n  version: '1'\n", True),
    ("a trailing comment STAYS, untouched", "components:\n- name: a\n  port: 1 # pinned\n",
     "components:\n- name: a\n  port: 1 # pinned\n", True),
    ("a # inside a quoted value is never touched", "components:\n- name: a\n  note: 'x # y'\n",
     "components:\n- name: a\n  note: 'x # y'\n", True),
    ("data lines are byte-identical, nesting included",
     "components:\n- name: a\n  deps:\n  - b\n", "components:\n- name: a\n  deps:\n  - b\n", True),
    ("a comment between nested data lines goes, nesting survives",
     "components:\n- name: a\n  # which deps and why\n  deps:\n  - b\n",
     "components:\n- name: a\n  deps:\n  - b\n", True),
]


def self_test():
    fails = 0
    for label, src, want, lossless in SELF_TESTS:
        got = strip(src)
        if got != want:
            print("FAIL  %-46s got %r want %r" % (label, got, want))
            fails += 1
            continue
        bad = check_lossless(src, got)
        if bool(bad) == lossless:
            print("FAIL  %-46s losslessness %s, expected %s (%s)" % (label, not bad, lossless, bad))
            fails += 1
            continue
        print("ok    %s" % label)

    # A FALSIFIER: the checker must REFUSE a strip that drops a data line. Without this the
    # self-test would pass just as well against a checker that always returns [].
    broken = "components:\n- name: a\n  version: '1'\n"
    mutated = "components:\n- name: a\n"
    bad = check_lossless(broken, mutated)
    if not bad:
        print("FAIL  falsifier: a strip that DELETED a field was called lossless")
        fails += 1
    else:
        print("ok    falsifier: a strip that deleted a field is refused -- %s" % bad[0][:60])

    # and a strip that REORDERS components must be refused too
    bad = check_lossless("components:\n- name: a\n- name: b\n", "components:\n- name: b\n- name: a\n")
    if not bad:
        print("FAIL  falsifier: reordered components were called lossless")
        fails += 1
    else:
        print("ok    falsifier: reordered components are refused")

    print("strip-pin-comments: %s" % ("OK -- %d cases + 2 falsifiers" % len(SELF_TESTS) if not fails
                                      else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if not argv:
        sys.stderr.write(__doc__.split("    python3")[0])
        sys.stderr.write("usage: strip-pin-comments.py <in> [-o <out>] | --self-test\n")
        return 2
    src = argv[0]
    out = None
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    text = io.open(src, encoding="utf-8").read()
    stripped = strip(text)
    bad = check_lossless(text, stripped)
    if bad:
        for b in bad:
            sys.stderr.write("strip-pin-comments: %s\n" % b)
        return 1
    if out:
        io.open(out, "w", encoding="utf-8").write(stripped)
        sys.stderr.write("strip-pin-comments: %s (%s bytes) -> %s (%s bytes), data identical\n"
                         % (src, format(len(text), ","), out, format(len(stripped), ",")))
    else:
        sys.stdout.write(stripped)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
