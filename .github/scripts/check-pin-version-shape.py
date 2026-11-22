#!/usr/bin/env python3
"""CI guard: every pinned component version must be a PLAIN `N.N.N`.

WHY THIS EXISTS, AND WHY NOW. Diego's standing rule is "no -rc, stable only", and until today it
was enforced only by whoever was holding the work remembering it. Measured 2026-11-22 by planting
`authn 0.53.0-rc1` in the pins file and running every pin gate:

    check-pin-ci             exit 1   (that version is not in the registry)
    check-pin-crd-lockstep   exit 1   (authn-crd stayed at 0.52.0)
    check-same-version-line  exit 1   (declared line split)
    the other 7 pin gates    exit 0

All three refusals are INCIDENTAL. None tests for a pre-release: they fire because the sibling did
not move and because that exact version does not exist. A *published* pre-release, pinned on both
members of a declared line, would pass every gate in this repo.

WHAT CHANGES THE STAKES. core-provider #610 (shipping as 2.13.123) routes a chart version through
`versionname.ForChart`, which makes a non-plain version resolve to a SERVED apiVersion. Today
`1.0.0-RC1` yields the UNSERVED `v1-0-0-RC1` and everything derived from it 404s -- loud, and
self-limiting. After .123 it resolves to the served `v1-0-0-rc1` and simply works. So the
incidental protection stops being load-bearing exactly when .123 lands, and nothing announces
that.

core-provider also confirmed the blast radius is the whole pins file and not a subset: all 93 pins
declare a `kind`, so the installer renders a CompositionDefinition for every one of them and every
pin's version passes through ForChart. There is no exemption to carve out, which is what makes this
a single predicate rather than a table.

WHAT IS REFUSED, and why each shape rather than "whatever the regex says":

    1.0.0-RC1    pre-release      Diego's rule, and the shape #610 is about
    1.0.0+build  build metadata   same resolution path
    1.0.0-Rc1    uppercase        ForChart lowercases it, so the pin and the served
                                  name differ and a reader cannot predict one from the other
    v1.0.0       leading `v`      REFUSED AS POLICY, NOT BECAUSE IT BREAKS. ForChart keeps it
                                  working (served as `vv1-0-0`), which is precisely why it needs
                                  naming: after .123 it is another quietly functional shape, and
                                  a pin that reads v1.0.0 beside 92 that read N.N.N is a
                                  difference nobody can act on. core-provider raised this.

THIS GATE DOES NOT CHECK THAT A VERSION EXISTS. check-pin-ci does that, against every `sources`
repo. A gate whose subject is "this string looks like a version" must not be read as "this version
is published".

FAIL-CLOSED. A pins file that parses to fewer than MIN_PINS entries means the parse went wrong, not
that the fleet shrank -- and "0 pins, 0 violations" is indistinguishable from success.
"""
import pathlib
import re
import sys

import yaml

PINS = pathlib.Path("chart/files/component-pins.yaml")
MIN_PINS = 80  # 93 on 2026-11-22. Raise as the fleet grows; never lower to make a run pass.
PLAIN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def why(v: str) -> str:
    """Name the specific shape, so the error says more than 'does not match'."""
    if v.startswith("v") or v.startswith("V"):
        return ("has a leading 'v'. Refused as POLICY, not because it breaks: core-provider's "
                "versionname.ForChart keeps it working (served as 'vv1-0-0'), which is why it must "
                "be named rather than left to look like an accident")
    if "+" in v:
        return "carries build metadata (the '+' part)"
    if "-" in v:
        return ("is a pre-release (the '-' part). Diego's standing rule is stable versions only. "
                "After core-provider 2.13.123 a pre-release resolves to a SERVED apiVersion and "
                "works quietly, instead of 404ing loudly as it does today")
    if v != v.lower():
        return ("contains an uppercase letter. ForChart lowercases it, so the pinned string and "
                "the served version name differ")
    if re.match(r"^\d+\.\d+$", v):
        return "has only two parts; a pin must be a full N.N.N"
    if re.match(r"^0\d", v) or ".0" in re.sub(r"\.0(\.|$)", ".", v):
        return "has a leading zero in a numeric part"
    return "is not a plain N.N.N"


def load(text: str):
    d = yaml.safe_load(text)
    items = d if isinstance(d, list) else (d.get("components") or d.get("pins") or [])
    return [c for c in items if isinstance(c, dict) and c.get("name")]


def check(items):
    bad = []
    for c in items:
        v = c.get("version")
        if v is None:
            continue  # a pin with no version is check-pin-fields' subject, not this gate's
        v = str(v)
        if not PLAIN.match(v):
            bad.append((c["name"], v, why(v)))
    return bad


def main() -> int:
    if not PINS.is_file():
        print("::error::%s not found -- run from the repo root" % PINS)
        return 1
    items = load(PINS.read_text(encoding="utf-8"))
    print("  pins parsed: %d" % len(items))
    if len(items) < MIN_PINS:
        print("::error::parsed only %d pin(s), expected at least %d. Either the pins file was "
              "truncated or this parse went stale -- a gate that inspects nothing must FAIL, "
              "because '0 pins, 0 violations' is indistinguishable from success."
              % (len(items), MIN_PINS))
        return 1

    bad = check(items)
    if bad:
        print("::error::%d pinned version(s) are not a plain N.N.N:" % len(bad))
        for name, v, reason in bad:
            print("::error::  %s pinned at %r -- it %s" % (name, v, reason))
        print("::error::Every pin becomes a CompositionDefinition's spec.chart.version (all 93 "
              "declare a `kind`), so its shape decides the served apiVersion every derived object "
              "uses. check-pin-ci checks that a version EXISTS; this gate checks its shape.")
        return 1
    print("check-pin-version-shape: OK -- %d pin(s), every version a plain N.N.N" % len(items))
    return 0


def self_test() -> int:
    """Plant each refused shape and require it to be caught; plant valid ones and require silence.

    The second half is the half that matters. A predicate that refuses everything passes the first
    half perfectly, and would block every pin bump in the repo.
    """
    REFUSE = [
        ("1.0.0-RC1", "pre-release"),
        ("1.0.0-rc1", "lowercase pre-release"),
        ("1.0.0+build.5", "build metadata"),
        ("1.0.0-Rc1", "uppercase pre-release"),
        ("v1.0.0", "leading v"),
        ("V1.0.0", "leading uppercase V"),
        ("1.0", "two parts only"),
        ("1.0.0.1", "four parts"),
        ("latest", "not a version at all"),
        ("", "empty string"),
        ("01.0.0", "leading zero"),
        ("1.0.0 ", "trailing space"),
    ]
    ACCEPT = ["0.0.0", "1.0.0", "0.52.0", "1.12.74", "2.13.123", "10.20.30", "0.3.697"]

    bad = 0
    for v, label in REFUSE:
        hits = check([{"name": "probe", "version": v}])
        ok = len(hits) == 1
        bad += 0 if ok else 1
        print("  %-4s REFUSES %-16s (%s)%s" % ("ok" if ok else "FAIL", repr(v), label,
                                               "" if ok else "  <-- ACCEPTED, gate is blind"))
        if ok:
            # the reason must be specific, not the fallback, for the shapes we name
            reason = hits[0][2]
            if label in ("pre-release", "build metadata", "leading v") and "is not a plain" in reason:
                print("       FAIL: reason fell through to the generic message: %s" % reason)
                bad += 1
    for v in ACCEPT:
        hits = check([{"name": "probe", "version": v}])
        ok = not hits
        bad += 0 if ok else 1
        print("  %-4s ACCEPTS %-16s%s" % ("ok" if ok else "FAIL", repr(v),
                                          "" if ok else "  <-- REFUSED, gate would block every bump"))

    # a pin with no version at all is another gate's subject and must not be reported here
    ok = not check([{"name": "probe"}])
    bad += 0 if ok else 1
    print("  %-4s a pin with NO version is left to check-pin-fields" % ("ok" if ok else "FAIL"))

    # and the fail-closed floor must actually fire
    import io as _io
    tiny = yaml.safe_dump([{"name": "only", "version": "1.0.0"}])
    ok = len(load(tiny)) < MIN_PINS
    bad += 0 if ok else 1
    print("  %-4s a 1-pin file is below MIN_PINS=%d, so the floor fires" % ("ok" if ok else "FAIL", MIN_PINS))

    # THE REAL FILE must pass, or this gate cannot be merged
    if PINS.is_file():
        real = check(load(PINS.read_text(encoding="utf-8")))
        ok = not real
        bad += 0 if ok else 1
        print("  %-4s the REAL pins file passes (%d violation(s))" % ("ok" if ok else "FAIL", len(real)))
        for n, v, r in real:
            print("       %s %r -- %s" % (n, v, r))

    print("self-test: %d case(s), %d failure(s)" % (len(REFUSE) + len(ACCEPT) + 3, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
