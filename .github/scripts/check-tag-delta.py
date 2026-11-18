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
        # BOTH SIDES THROUGH ONE RENDERER. The claimed rows come from the tag message, where a
        # value is already a string; the derived rows come from the pins, where a non-scalar field
        # is a dict. str(dict) is Python repr, which is NOT what the generator wrote, so comparing
        # str() against the message's JSON made every non-scalar row mismatch even once the block
        # parsed. td.scalar is the generator's own renderer, so a value is compared in exactly the
        # representation it was claimed in.
        return sorted((n, f,
                       None if a is None else (a if isinstance(a, str) else td.scalar(a)),
                       None if b is None else (b if isinstance(b, str) else td.scalar(b)))
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


def _baseline_selftest():
    """The BASELINE SELECTION had no test, and that is where the defect lived.

    Every case below stubs td so it runs offline. The point is not that previous_released_tag works
    -- hack/tag-delta.py tests that -- but that main() ROUTES through it and handles all three of
    its returns. The gate shipped for weeks calling td.previous_tag(), and no self-test could tell.
    """
    fails = 0
    real_prt, real_pt = td.previous_released_tag, td.previous_tag
    real_pins, real_deps, real_msg = td.pins_at, td.deps_at, globals()["tag_message"]
    try:
        # A tag's delta is read from pins; stub them so only the baseline choice varies.
        td.pins_at = lambda t: {"c": {"version": "1" if t == "OLD" else "2"}}
        td.deps_at = lambda t: {}
        globals()["tag_message"] = lambda t: "h\n\n" + td.block(
            [("c", "version", "1", "2")], []) + "\n\nt"

        cases = []
        # 1. the normal path: no skips, exit 0
        td.previous_released_tag = lambda t: ("OLD", [], None)
        cases.append(("published predecessor -> OK", main(["NEW"]), 0, None))
        # 2. THE REAL DEFECT: an unpublished tag is skipped and the fact is REPORTED. Without the
        #    report, the wider delta looks like an undeclared change to whoever reads the log.
        td.previous_released_tag = lambda t: ("OLD", ["SKIPPED"], None)
        import io as _io, contextlib as _cl
        buf = _io.StringIO()
        with _cl.redirect_stdout(buf):
            rc = main(["NEW"])
        cases.append(("skipped baseline -> OK and SAYS SO", rc, 0,
                      "SKIPPED" if "SKIPPED" in buf.getvalue() else None))
        # 3. REFUSAL, not a guess. An unreachable registry must not silently become a baseline.
        td.previous_released_tag = lambda t: (None, [], "registry unreachable")
        cases.append(("registry error -> REFUSES with exit 2", main(["NEW"]), 2, None))
        # 4. a genuine first tag: nothing to compare, exit 0
        td.previous_released_tag = lambda t: (None, [], None)
        cases.append(("no predecessor -> exit 0", main(["NEW"]), 0, None))

        for label, got, want, extra in cases:
            ok = got == want and (extra is not None if "SAYS SO" in label else True)
            fails += 0 if ok else 1
            print("  %-5s baseline: %s" % ("ok" if ok else "FAIL", label))
            if not ok:
                print("        want exit=%r got=%r extra=%r" % (want, got, extra))
        # 5. THE ROUTING ASSERTION. If main() is ever changed back to the naive predecessor, every
        #    case above still passes -- they stub both. So assert previous_tag is NOT consulted.
        called = []
        td.previous_tag = lambda t: called.append(t) or "NAIVE"
        td.previous_released_tag = lambda t: ("OLD", [], None)
        main(["NEW"])
        ok = not called
        fails += 0 if ok else 1
        print("  %-5s baseline: main() does NOT consult the naive previous_tag" %
              ("ok" if ok else "FAIL"))
    finally:
        td.previous_released_tag, td.previous_tag = real_prt, real_pt
        td.pins_at, td.deps_at = real_pins, real_deps
        globals()["tag_message"] = real_msg
    return fails


def self_test():
    fails = _baseline_selftest()
    pd = [("snowplow", "version", "1.12.67", "1.12.70"),
          ("authn", "gatewayPath", "/auth", "/authn")]
    dd = [("core-provider", "version", "2.13.82", "2.13.95")]
    good = "header\n\n" + td.block(pd, dd) + "\n\ntrailer"

    # A NON-SCALAR FIELD. `resources` is a nested dict, and its arrival broke this gate on tag
    # 0.3.651: Python's repr of a dict contains spaces, BLOCK_RE requires \S* for a value, so the
    # block did not match AT ALL and the gate reported "declares no PINS-CHANGED block" about a tag
    # whose message carried a correct generated block. The generator and the parser disagreed about
    # their own format, which is worse than either being wrong alone -- the claim read as right to
    # a human and as absent to the check. Fixed by rendering a non-scalar as compact sorted JSON in
    # td.scalar, and by normalising BOTH sides of the comparison through that same renderer.
    nspd = [("portal", "resources",
             {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
             {"requests": {"cpu": "50m", "memory": "96Mi"}, "limits": {"memory": "512Mi"}}),
            ("agentgateway-controller", "resources", None,
             {"requests": {"cpu": "50m", "memory": "224Mi"}, "limits": {"memory": "512Mi"}})]
    nsgood = "header\n\n" + td.block(nspd, []) + "\n\ntrailer"
    # the same delta with ONE dict value altered: the check must still see the VALUE, not just the
    # field name, or a block could claim any resources and pass.
    nsother = [(nspd[0][0], nspd[0][1], nspd[0][2], {"requests": {"cpu": "50m", "memory": "999Mi"},
                                                     "limits": {"memory": "512Mi"}}),
               nspd[1]]

    cases = [
        ("an exact generated block is accepted", good, pd, dd, True),
        ("a NON-SCALAR (dict) row round-trips", nsgood, nspd, [], True),
        ("a dict row whose VALUE differs is refused", nsgood, nsother, [], False),
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

    print("check-tag-delta: %s" % ("OK -- %d cases + 2 falsifiers + 5 baseline-selection cases" % len(cases) if not fails
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
    # THE BASELINE MUST BE A PUBLISHED TAG, and this gate used td.previous_tag() -- the naive
    # immediate predecessor -- while hack/tag-delta.py used td.previous_released_tag(), which skips
    # tags whose chart was never published. The two implementations of one predicate diverged, and
    # the divergence had a consequence: 0.3.672's release failed at the org "Assert the tag is on
    # main" gate, so its chart never published, and this gate then compared 0.3.673 against it and
    # computed an EMPTY delta -- reading RED on a correctly-published tag whose message was right.
    #
    # A delta against an unpublished baseline describes a transition NO CLUSTER CAN MAKE, which is
    # why hack/ refuses it. Calling the shared function is the whole fix; duplicating the logic here
    # is what caused this in the first place.
    prev, skipped, perr = td.previous_released_tag(tag)
    if perr:
        # REFUSING IS CORRECT HERE. An unreachable registry and a long run of unpublished tags look
        # identical from here ("nothing published"), and the two call for opposite responses. A gate
        # that guesses silently produces the exact failure above; one that stops says so.
        print("::error::check-tag-delta: %s" % perr)
        return 2
    if not prev:
        print("check-tag-delta: %s has no predecessor tag — nothing to compare" % tag)
        return 0
    if skipped:
        # SAY WHEN THE BASELINE IS NOT THE PREDECESSOR. The declared delta is then wider than a
        # reader expects, and without this line the extra entries look like an undeclared change.
        print("check-tag-delta: baseline is %s, not the immediate predecessor — skipped %d tag(s) "
              "whose chart was never published: %s" % (prev, len(skipped), ", ".join(skipped)))
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
