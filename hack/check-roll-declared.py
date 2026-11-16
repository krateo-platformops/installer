#!/usr/bin/env python3
"""Verify a `helm upgrade --dry-run=server` BEFORE the real roll: every component move is DECLARED,
and every value that must survive the roll actually survives it.

ADAPTED FROM acmp's check3b.py, which they ran for demo windows 3a/3b and sent as text. Two
properties of theirs are kept deliberately, because both caught real things:

  1. REFUSE TO VERIFY rather than report a pass when the evidence is absent. Their script exits 8
     if the self-cr patch carries no componentValues. A checker that cannot see the thing it checks
     must fail, not pass -- that exact failure cost me a verified RBAC change on 2026-11-15, where
     my post-patch read-back checked the keys I had changed and reported success while the
     mechanism that reverted them sat elsewhere.
  2. FAIL CLOSED ON AN UNEXPECTED DIFFERENCE, even a harmless-looking one. Their first dry-run
     failed on cert-manager-issuers moving in the CR although it renders nothing on demo. That is
     the script working: "it renders nothing, so the move is harmless" is a judgement the checker
     must not be allowed to make, because the component that renders nothing today is the one that
     renders something after a feature flips.

WHAT THIS ADDS: the DECLARED-MOVES property, and the CARRY-OVER property.

DECLARED MOVES. The caller states {name: [from, to]} and the check asserts the set of components
whose version differs from the live CR EQUALS that set -- failing on extra AND missing. Not "no
downgrades". On 2026-11-16 installer #984 moved three components upward without naming them and
`check-no-silent-downgrade` passed correctly, because it asks whether a move is a DOWNGRADE, not
whether it is INTENDED. An undeclared upgrade is the same class of defect and had no instrument.

CARRY-OVER. Values that must appear UNCHANGED in the rendered patch, by dotted path. This exists
for one measured near-wedge: demo's ClickHouse volume is 50Gi, and the 50Gi lives ONLY in
  componentValues.krateo-observability.clickstack.clickhouse.cluster.spec
    .dataVolumeClaimSpec.resources.requests.storage
while the chart's own default is 15Gi. (`clickstack.clickhouse.persistence.size: 50Gi` is read by
ZERO of the chart's 45 templates -- it looks like the knob and is inert.) A values file rebuilt FROM
SCRATCH to lift the holds drops that override, falls back to 15Gi, and a PVC CANNOT SHRINK while a
StatefulSet volumeClaimTemplate is IMMUTABLE. So the roll would wedge, not resize. Piping
`helm get values -o json` through jq is what prevents it; this check is what proves the pipe worked.

    python3 hack/check-roll-declared.py LIVE.json MOVES.json CARRY.json  < dry-run-output
    python3 hack/check-roll-declared.py --self-test

  LIVE.json   {"components": [...]}  read from the live CR AT THE SERVED VERSION
  MOVES.json  {"<component>": ["<from>", "<to>"], ...}   may be {} for a values-only upgrade
  CARRY.json  {"<dotted.path.into.spec>": <expected value>, ...}

Exit: 0 pass; 5 a declared/carry-over violation; 6 no installer-self-cr ConfigMap;
      7 no MANIFEST in the dry-run output; 8 the patch cannot carry the evidence.
"""
import json
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required")

MARKER = "MANIFEST:"


def dig(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return (False, None)
        cur = cur[part]
    return (True, cur)


def evaluate(spec, live_components, moves, carry):
    """Pure, so the self-test can drive it without a cluster or a helm binary."""
    problems = []
    comps = {c["name"]: str(c.get("version"))
             for c in (spec.get("components") or []) if "name" in c}
    live = {c["name"]: str(c.get("version"))
            for c in (live_components or []) if "name" in c}

    moved = {n: (live.get(n), comps.get(n))
             for n in set(comps) | set(live) if comps.get(n) != live.get(n)}
    declared = {n: (str(v[0]), str(v[1])) for n, v in (moves or {}).items()}

    extra = {n: v for n, v in moved.items() if n not in declared}
    missing = {n: v for n, v in declared.items() if n not in moved}
    wrong = {n: (moved[n], declared[n]) for n in set(moved) & set(declared)
             if moved[n] != declared[n]}

    if extra:
        problems.append("UNDECLARED MOVES (this is the #984 class -- a move nobody named): %s"
                        % json.dumps(extra, sort_keys=True))
    if missing:
        problems.append("DECLARED BUT NOT MOVING (the roll would not do what was approved): %s"
                        % json.dumps(missing, sort_keys=True))
    if wrong:
        problems.append("MOVING TO THE WRONG VERSION: %s" % json.dumps(wrong, sort_keys=True))

    for path, want in (carry or {}).items():
        found, got = dig(spec, path)
        if not found:
            problems.append("CARRY-OVER LOST: %s is ABSENT from the rendered patch (want %r). "
                            "A values file rebuilt from scratch drops overrides; pipe "
                            "`helm get values -o json` through jq instead." % (path, want))
        elif got != want:
            problems.append("CARRY-OVER CHANGED: %s is %r, want %r" % (path, got, want))

    return problems, moved


def _self_test():
    LIVE = [{"name": "portal", "version": "1.8.128"},
            {"name": "krateo-observability", "version": "0.1.42"},
            {"name": "cert-manager-issuers", "version": "0.16.0"}]
    STORE = ("componentValues.krateo-observability.clickstack.clickhouse.cluster.spec"
             ".dataVolumeClaimSpec.resources.requests.storage")

    def spec(comps, storage="50Gi"):
        s = {"components": comps}
        if storage is not None:
            cur = s
            parts = STORE.split(".")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = storage
        return s

    cases = [
        ("the intended Window 4: both declared moves, storage carried",
         spec([{"name": "portal", "version": "1.8.129"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.16.0"}]),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 0),

        ("THE #984 CLASS: a third component moves upward, undeclared",
         spec([{"name": "portal", "version": "1.8.129"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.18.0"}]),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 1),

        ("THE WEDGE: storage override dropped, falls back to the chart's 15Gi",
         spec([{"name": "portal", "version": "1.8.129"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.16.0"}], storage=None),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 1),

        ("THE SILENT SHRINK: storage present but changed to 15Gi",
         spec([{"name": "portal", "version": "1.8.129"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.16.0"}], storage="15Gi"),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 1),

        ("a declared move that does NOT happen -- the roll would under-deliver",
         spec([{"name": "portal", "version": "1.8.128"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.16.0"}]),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 1),

        ("a move to an unexpected target version",
         spec([{"name": "portal", "version": "1.8.130"},
               {"name": "krateo-observability", "version": "0.1.45"},
               {"name": "cert-manager-issuers", "version": "0.16.0"}]),
         {"portal": ["1.8.128", "1.8.129"], "krateo-observability": ["0.1.42", "0.1.45"]},
         {STORE: "50Gi"}, 1),

        ("FALSIFIER: a values-only upgrade. No moves declared, none happen -> PASS. "
         "Without this the check would demand a move on every roll.",
         spec(LIVE), {}, {STORE: "50Gi"}, 0),

        ("FALSIFIER: an ADDED component is a move too (absent -> present)",
         spec(LIVE + [{"name": "kyverno", "version": "3.9.2"}]), {}, {STORE: "50Gi"}, 1),

        ("FALSIFIER: a REMOVED component is a move too (present -> absent)",
         spec([c for c in LIVE if c["name"] != "cert-manager-issuers"]), {}, {STORE: "50Gi"}, 1),

        ("FALSIFIER: no carry-over paths requested -> storage is not checked at all",
         spec(LIVE, storage=None), {}, {}, 0),
    ]
    bad = 0
    for label, sp, moves, carry, want_bad in cases:
        problems, _ = evaluate(sp, LIVE, moves, carry)
        got_bad = 1 if problems else 0
        ok = got_bad == want_bad
        bad += 0 if ok else 1
        print("  %-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            print("        want_bad=%d got=%s" % (want_bad, problems))
    print("self-test: %d case(s), %d failure(s)" % (len(cases), bad))
    return 1 if bad else 0


def main():
    if "--self-test" in sys.argv:
        return _self_test()
    if len(sys.argv) < 4:
        return print(__doc__) or 2

    live = json.load(open(sys.argv[1], encoding="utf-8")).get("components")
    moves = json.load(open(sys.argv[2], encoding="utf-8"))
    carry = json.load(open(sys.argv[3], encoding="utf-8"))
    raw = sys.stdin.read()

    for line in raw.splitlines()[:14]:
        if line.split(":")[0] in ("STATUS", "REVISION", "Error", "NAME"):
            print(line)

    if MARKER not in raw:
        print("REFUSED: no %s in the dry-run output, so there is nothing to verify. "
              "This is not a pass." % MARKER)
        return 7

    for doc in yaml.safe_load_all(raw.split(MARKER, 1)[1]):
        if not (isinstance(doc, dict) and doc.get("kind") == "ConfigMap"):
            continue
        if doc["metadata"]["name"] != "installer-self-cr":
            continue
        patch = yaml.safe_load(doc["data"]["components-patch.json"])
        spec = patch.get("spec", patch)
        if carry and "componentValues" not in spec:
            print("REFUSED: the self-cr patch carries no componentValues, so the carry-over "
                  "paths cannot be verified. This is not a pass.")
            return 8
        problems, moved = evaluate(spec, live, moves, carry)
        print("component versions differing from the live CR: %s"
              % json.dumps(moved, sort_keys=True))
        print("declared moves: %s" % json.dumps(moves, sort_keys=True))
        for path in (carry or {}):
            found, got = dig(spec, path)
            print("carry-over %s = %s" % (path, repr(got) if found else "ABSENT"))
        if problems:
            print()
            for p in problems:
                print("  %s" % p)
            print()
            print("NOT ROLLING.")
            return 5
        print("OK -- every move is declared and every carry-over survived.")
        return 0

    print("REFUSED: no installer-self-cr ConfigMap in the manifest. This is not a pass.")
    return 6


if __name__ == "__main__":
    sys.exit(main())
