#!/usr/bin/env python3
"""Fixtures for check-feature-render.py's verdict rules.

WHY THIS EXISTS. That file had `def main()` only, so every rule in it was exercised ONLY against
whatever the live chart happened to render -- a sample of one, and one that passes. On 2026-11-17 I
CHANGED one of those rules (the installer#526 assertion, made namespace-agnostic because a pinned
component may now declare its own `namespace`) with nothing but the live chart to tell me whether
the new version still caught anything. A rule that no longer detects reports a protection that does
not exist, which is worse than no rule at all.

EACH CASE NAMES THE RULE IT PINS, and the suite asserts BOTH directions: that a violation is caught
AND that a legitimate render is not flagged. A one-directional suite is how a rule that fires on
everything, or on nothing, passes.
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cfr", HERE / "check-feature-render.py")
cfr = importlib.util.module_from_spec(spec)
sys.modules["cfr"] = cfr
spec.loader.exec_module(cfr)

NS = "krateo-system"
OTHER = "kyverno"            # a component declaring its own namespace -- the kyverno pin's case


def obj(kind, name, ns=NS):
    return (kind, ns, name)


_umb_kind, _umb_name = next(iter(cfr.UMBRELLA))
CASES = []


def case(label, feat, comps, base, off, want_substr):
    """want_substr: a string the failure must contain, or None for NO failure."""
    CASES.append((label, feat, comps, base, off, want_substr))


# ---------------------------------------------------------------------------------------------
# RULE: non-vacuity A -- the feature's components must be in the baseline.
case("non-vacuity A: a feature whose components never rendered proves nothing",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "portal")},
     {obj("CompositionDefinition", "portal")},
     "cannot be proven either way")
case("CONTROL: a feature whose component IS in the baseline passes rule A",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager")},
     set(),
     None)

# ---------------------------------------------------------------------------------------------
# RULE: nothing may APPEAR when a feature is turned off.
case("turning a feature OFF must not ADD anything",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager")},
     {obj("CompositionDefinition", "cert-manager"), obj("ConfigMap", "surprise")},
     "ADDED objects")

# ---------------------------------------------------------------------------------------------
# RULE: non-vacuity B -- something must go.
case("non-vacuity B: turning it OFF must remove SOMETHING",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager")},
     {obj("CompositionDefinition", "cert-manager")},
     "removed NOTHING")

# ---------------------------------------------------------------------------------------------
# RULE: every removal must be attributable.
case("a removed object nobody declared is UNDECLARED, not ignored",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager"), obj("ConfigMap", "mystery")},
     {obj("CompositionDefinition", "cert-manager")},
     "not declared in COMPANIONS")
case("an UMBRELLA object must never be feature-gated",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager"),
      obj(_umb_kind, _umb_name)},
     {obj("CompositionDefinition", "cert-manager")},
     "NOT gated on it")

# ---------------------------------------------------------------------------------------------
# RULE: the installer#526 assertion -- a component's declared companion must go with it.
# THE CASE THE 2026-11-17 CHANGE EXISTS FOR, and the one that fails if the namespace is
# reintroduced into the match.
_comp_kind, _comp_name = next(iter(cfr.COMPANIONS))
_owner = cfr.COMPANIONS[(_comp_kind, _comp_name)]
case("#526: a companion LEFT BEHIND when its owner goes is a failure",
     "ingress", {_owner},
     {obj("CompositionDefinition", _owner), obj(_comp_kind, _comp_name)},
     {obj(_comp_kind, _comp_name)},                     # the companion SURVIVED
     "companion object(s) SURVIVED")
case("#526 CONTROL: the companion going WITH its owner is NOT a failure",
     "ingress", {_owner},
     {obj("CompositionDefinition", _owner), obj(_comp_kind, _comp_name)},
     set(),                                             # both went
     None)
case("#526 IN A NON-DEFAULT NAMESPACE: a companion removed from ns `kyverno` must still count "
     "as removed. This is the case the namespace-agnostic match exists for -- with the old "
     "hardcoded (kind, 'krateo-system', name) tuple it reads as SURVIVED, a FALSE POSITIVE on "
     "the one assertion whose false positives teach an operator to ignore this gate.",
     "ingress", {_owner},
     {obj("CompositionDefinition", _owner, OTHER), obj(_comp_kind, _comp_name, OTHER)},
     set(),
     None)

# ---------------------------------------------------------------------------------------------
# RULE: an OFF render that could not be produced is a failure, never a pass.
case("an unproducible OFF render is a FAILURE, not silence",
     "ingress", {"cert-manager"},
     {obj("CompositionDefinition", "cert-manager")},
     None,
     "could not be produced")


def main():
    bad = 0
    for label, feat, comps, base, off, want in CASES:
        got = cfr.verdict(feat, comps, base, off)
        if want is None:
            ok = not got
        else:
            ok = any(want in f for f in got)
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want=%r" % want)
            print("        got =%s" % (got or "[] (no failures)"))
    print("check-feature-render self-test: %d case(s), %d failure(s)" % (len(CASES), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
