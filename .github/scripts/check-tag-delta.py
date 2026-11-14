#!/usr/bin/env python3
"""CI guard: a release tag's message must declare, machine-readably, the pin delta it carries.

WHY. On 2026-11-14 tag 0.3.638's message said "THIS TAG CARRIES THE TWO PAIRS AND NOTHING ELSE".
It carried 31 field-level pin changes across 29 components. The pull request's difference set
against origin/main really was exactly two components and really was asserted field-for-field --
that is the right question for a PR and says nothing about a tag. A tag is cut from main and carries
everything main accumulated since the PREVIOUS TAG.

Nothing in this directory compared a tag against the previous tag. Every pin gate compares against
the MERGE TARGET. So the gap was structural, not an oversight in any one gate.

The damage was informational and that is what makes it worth a gate: five sessions and the user
reasoned about attribution on the claim. A regression was attributed to the one component I said had
moved; a latency comparison was planned as if it could isolate one image; and the user was asked to
decide when to ship something that had already shipped.

HOW. hack/tag-delta.py GENERATES the block, so the claim cannot be typed wrong:

    PINS-CHANGED: <n>
      <component> <field> <old> -> <new>
    CHART-DEPS-CHANGED: <n>
      <component> <field> <old> -> <new>

This re-derives the delta from the two refs and refuses a tag whose block is missing, miscounted or
disagrees. Prose cannot satisfy it -- that is asserted against 0.3.638's REAL message as a fixture.

    python3 .github/scripts/check-tag-delta.py <tag>        -> check that tag
    python3 .github/scripts/check-tag-delta.py --self-test  -> offline cases + falsifiers
"""
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TD = os.path.join(HERE, "..", "..", "hack", "tag-delta.py")

spec = importlib.util.spec_from_file_location("tag_delta", TD)
td = importlib.util.module_from_spec(spec)
spec.loader.exec_module(td)


def tag_message(tag):
    r = subprocess.run(["git", "tag", "-l", tag, "--format=%(contents)"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


WITHHELD = "WITHHELD"


def errors(tag, msg, pin_delta, dep_delta):
    """Broken-rule sentences, or the string WITHHELD; [] when the claim matches what it carries.

    AN EMPTY MESSAGE IS WITHHELD, NOT A REFUSAL, and that distinction cost this gate its first real
    run. On tag 0.3.641 it passed locally and failed in CI with "declares no PINS-CHANGED block" --
    but the tag's message DID carry a correct generated block. actions/checkout leaves a tag as a
    lightweight ref pointing at the commit, so `git tag -l --format=%(contents)` returns NOTHING and
    the gate read an absent message as a missing block. Same shape as the sibling failure an hour
    earlier: a correct measurement of the wrong object -- there, the working tree instead of the
    commit; here, a lightweight ref instead of the annotated tag.
    So: an empty message means the gate could not READ the claim, which is a different fact from the
    claim being absent, and reporting the second when the first is true sends the reader to rewrite
    a tag message that is already right.
    """
    bad = []
    if not (msg or "").strip():
        return WITHHELD
    parsed = td.parse_block(msg)
    if parsed is None:
        bad.append("tag %s declares no PINS-CHANGED block. Generate one with "
                   "`python3 hack/tag-delta.py %s` and put it in the tag message. Prose such as "
                   "\"carries X and nothing else\" cannot be checked and has been wrong before."
                   % (tag, tag))
        return bad
    if parsed == "COUNT_MISMATCH":
        bad.append("tag %s's block has a count that disagrees with its own rows" % tag)
        return bad
    claimed_pins, claimed_deps = parsed
    def norm(rows):
        return sorted((n, f, None if a is None else str(a), None if b is None else str(b))
                      for n, f, a, b in rows)
    for label, claimed, actual in (("pin", claimed_pins, pin_delta),
                                   ("chart dependency", claimed_deps, dep_delta)):
        c, a = norm(claimed), norm(actual)
        if c == a:
            continue
        missing = [r for r in a if r not in c]
        extra = [r for r in c if r not in a]
        if missing:
            bad.append("tag %s CARRIES %d %s change(s) it does not declare: %s"
                       % (tag, len(missing), label,
                          ", ".join("%s %s %s->%s" % r for r in missing[:6])
                          + (" ..." if len(missing) > 6 else "")))
        if extra:
            bad.append("tag %s DECLARES %d %s change(s) it does not carry: %s"
                       % (tag, len(extra), label,
                          ", ".join("%s %s %s->%s" % r for r in extra[:6])
                          + (" ..." if len(extra) > 6 else "")))
    return bad


# 0.3.638's real message, abridged to the sentence that was false.
REAL_0_3_638 = ("0.3.638 - snowplow 1.12.70 (pair) + core-provider 2.13.95 (pair): the 057 unwedge\n"
                "\nTHIS TAG CARRIES THE TWO PAIRS AND NOTHING ELSE. The 27-change catch-up stays\n"
                "on main for a later tag.\n")
ACTUAL_0_3_638 = [("snowplow", "version", "1.12.67", "1.12.70"),
                  ("snowplow-crd", "version", "1.12.67", "1.12.70"),
                  ("authn", "version", "0.46.0", "0.47.0")]


def self_test():
    fails = 0
    pd = [("snowplow", "version", "1.12.67", "1.12.70"),
          ("authn", "gatewayPath", "/auth", "/authn")]
    dd = [("core-provider", "version", "2.13.82", "2.13.95")]
    good = "header\n\n" + td.block(pd, dd) + "\n\ntrailer"

    cases = [
        ("an exact generated block is accepted", good, pd, dd, True),
        ("a block missing one carried change is refused", "x\n" + td.block(pd[:1], dd), pd, dd, False),
        ("a block claiming a change not carried is refused", "x\n" + td.block(pd, dd), pd[:1], dd, False),
        ("a block with the wrong dep version is refused", "x\n" + td.block(pd, dd), pd,
         [("core-provider", "version", "2.13.82", "2.13.96")], False),
        ("an empty tag is fine when nothing moved", "x\n" + td.block([], []), [], [], True),
        ("an empty block is refused when something moved", "x\n" + td.block([], []), pd, dd, False),
    ]
    for label, msg, p, d, want_ok in cases:
        bad = errors("T", msg, p, d)
        ok = not bad
        if ok != want_ok:
            print("FAIL  %-52s ok=%s want=%s %s" % (label, ok, want_ok, bad[:1])); fails += 1
        else:
            print("ok    %-52s %s" % (label, "accepted" if ok else "refused"))

    # THE FIXTURE THAT MATTERS: the real prose claim must be refused
    bad = errors("0.3.638", REAL_0_3_638, ACTUAL_0_3_638, [])
    if not bad:
        print("FAIL  falsifier: 0.3.638's REAL message was ACCEPTED"); fails += 1
    else:
        print("ok    falsifier: 0.3.638's REAL message is refused -- %s" % bad[0][:64])

    # and a tag with no message at all -> WITHHELD, which is neither a pass nor a refusal
    got = errors("T", "", pd, dd)
    if got != WITHHELD:
        print("FAIL  falsifier: an empty message gave %r, wanted WITHHELD" % (got,)); fails += 1
    else:
        print("ok    falsifier: an EMPTY message is WITHHELD, not refused (it is unreadable, not absent)")
    for blank in ("   ", "\n\n", None):
        if errors("T", blank, pd, dd) != WITHHELD:
            print("FAIL  falsifier: %r was not WITHHELD" % (blank,)); fails += 1
    else:
        print("ok    falsifier: whitespace-only and None are WITHHELD too")
    # ...but PROSE is a refusal, because the message WAS readable and carries no block
    if errors("0.3.638", REAL_0_3_638, ACTUAL_0_3_638, []) == WITHHELD:
        print("FAIL  falsifier: readable prose was WITHHELD instead of refused"); fails += 1
    else:
        print("ok    falsifier: readable PROSE is refused, not withheld — the two are distinct")

    print("check-tag-delta: %s" % ("OK -- %d cases + 2 falsifiers" % len(cases) if not fails
                                   else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if not argv:
        # NO ARGUMENT RUNS THE SELF-TEST, and that is deliberate rather than a convenience. Every
        # other gate in this directory is runnable with no arguments, and a reader who runs this one
        # bare must get a REAL verdict, not a usage line (exit 2 reads as "broken") and not exit 0
        # with nothing checked (a gate that cannot fail). The live tag comparison needs a tag, which
        # the workflow passes explicitly; bare invocation checks the gate's own rules instead.
        sys.stderr.write("check-tag-delta: no tag given — running the self-test. "
                         "For a live check: check-tag-delta.py <tag>\n")
        return self_test()
    tag = argv[0]
    prev = td.previous_tag(tag)
    if not prev:
        print("check-tag-delta: %s has no predecessor tag — nothing to compare" % tag)
        return 0
    po, pn = td.pins_at(prev), td.pins_at(tag)
    if po is None or pn is None:
        print("::error::check-tag-delta: no pins file at %s or %s" % (prev, tag))
        return 1
    pd = td.delta(po, pn)
    dd = td.delta(td.deps_at(prev), td.deps_at(tag)) or []
    msg = tag_message(tag)
    bad = errors(tag, msg, pd, dd)
    if bad == WITHHELD:
        print("::error::check-tag-delta: the message of tag %s is EMPTY as read by "
              "`git tag -l --format=%%(contents)`, so the claim could not be READ. This is WITHHELD, "
              "not a refusal: the tag may well declare its delta correctly. The usual cause is that "
              "the checkout holds a LIGHTWEIGHT ref for the tag rather than the annotated tag "
              "object, which carries the message. Fix the fetch, not the tag:\n"
              "    git fetch --force origin refs/tags/%s:refs/tags/%s\n"
              "then re-run. The delta this tag should declare is printed below." % (tag, tag, tag))
        print("")
        print(td.block(pd, dd))
        return 2
    for b in bad:
        print("::error::check-tag-delta: %s" % b)
    if bad:
        print("")
        print("The block this tag should declare, generated from the two refs:")
        print(td.block(pd, dd))
        return 1
    print("check-tag-delta: OK — %s declares exactly what it carries against %s "
          "(%d pin field change(s), %d chart dependency change(s); message %d chars)"
          % (tag, prev, len(pd), len(dd), len(msg or "")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
