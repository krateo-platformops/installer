#!/usr/bin/env python3
"""rd-immutable-gate.py -- refuse a roll that changes an oasgen-immutable RestDefinition field.

WHY THE FIELD LIST IS NOT WRITTEN DOWN HERE. oasgen-provider enforces post-generation
immutability as CEL transition rules on the RestDefinition CRD, and that set GROWS: it was
`kind` + `additionalStatusFields` for a long time, `identifiers` has always been there, and
`configurationFields`, `excludedSpecFields`, `includedSpecFields` and `resourceGroup` were
news to this repo in 2026-10 (oasgen-provider#290). A list in THIS file would be a copy of
someone else's invariant, correct on the day it was typed -- which is exactly the failure this
gate exists to stop. So the list is derived from the live CRD's x-kubernetes-validations and
the gate tracks oasgen's releases by construction.

WHY THIS GATE EXISTS AT ALL. github-provider-kog's pullrequest RestDefinition has now refused
an in-place upgrade THREE times on THREE different immutable fields:
  0.3.2        spec.resource.additionalStatusFields   (resolved by oasgen 0.27.0, append-only)
  0.6.0 fresh  spec.resource.kind                     (the apps/v1 `Deployment` collision)
  0.6.0 live   spec.resource.identifiers              ['number'] -> ['head','base']
Each time the pre-roll check had been written for the field that bit LAST time. The 0.3.2 note
in files/component-pins.yaml even records verifying "ALL 29 rd-*.yaml templates" -- breadth
across templates, while the axis that mattered was the FIELD. A gate keyed on one field name
is the same mistake with better spelling.

DO NOT GREP THE CRD FOR `oldSelf`. It appears 12 times in a 7-rule CRD: each append-only rule
mentions it twice, and the doc comment explaining the prefix comparison is rendered into
`description` text. A grep over-counts, you hunt for fields that do not exist, and the natural
conclusion is that the extraction is broken rather than the method. Walk `properties` and read
`x-kubernetes-validations[].rule` structurally.

APPEND IS LEGAL FOR TWO FIELDS, REORDER IS NOT. The append-only CEL is
    size(self) >= size(oldSelf) && oldSelf.all(i, v, self[i] == v)
-- an INDEXED prefix compare. Appending at the end passes; removing, renaming, editing or
REORDERING an existing entry fails. Reorder is the case most likely to be waved through by a
human reviewer as a false positive, because the set is unchanged, so this compares order.

IDENTIFIERS CANNOT BE WIDENED, and the reason is not that nobody implemented it. identifiers
drive resolution through IdentifiersMatchPolicy (default OR), so appending broadens matching
under OR -- a findby can resolve a CR to a DIFFERENT external object -- and narrows it under
AND, where CRs that matched on the old identifier match nothing. Either way live CRs silently
repoint, which is worse than a refused upgrade (oasgen-provider#290). There is no cheap path.

SCOPE THE LIVE SET TO THE RELEASE BEING ROLLED. A cluster's RestDefinitions belong to many
components -- 057 has 33 across kog, the portal builders and the feedback-form controllers.
Comparing a one-component render against ALL of them reports every other component's objects
as "dropped by the release", which is both false and the kind of noise that teaches an operator
to skim a gate's output. --release filters live by meta.helm.sh/release-name, which is the
ownership marker helm itself writes.

USAGE
  rd-immutable-gate.py --rendered <rendered.yaml> --live <live-rds.json> --crd <crd.json>
      --release <helm-release-name>
      [--expect-rules N]          how many immutability rules the CRD must carry (default 7)
      [--allow <name>:<field>]... declare a change you have a migration for
      [--self-test]               prove the comparator can see a change, then exit

EXIT CODES
  0  no immutable-field change, or every change was declared with --allow
  2  WITHHELD -- nothing was compared, or the CRD carries a different number of rules than
     --expect-rules. A gate that compared nothing must never report a pass.
  3  an undeclared immutable-field change: the roll will be refused by oasgen in place
"""
import argparse, copy, json, sys

try:
    import yaml
except ImportError:
    print("rd-immutable-gate: PyYAML is required", file=sys.stderr)
    sys.exit(2)


def extract_rules(crd):
    """Field path -> 'strict' | 'append-only', read from the CRD's CEL transition rules."""
    found = {}

    def walk(schema, path):
        if not isinstance(schema, dict):
            return
        for v in schema.get("x-kubernetes-validations", []) or []:
            rule = v.get("rule", "")
            if "oldSelf" not in rule:
                continue
            found[path] = "append-only" if "size(self)" in rule else "strict"
        for name, sub in (schema.get("properties") or {}).items():
            walk(sub, f"{path}.{name}" if path else name)
        if "items" in schema:
            walk(schema["items"], path + "[]")

    for ver in crd.get("spec", {}).get("versions", []):
        walk((ver.get("schema") or {}).get("openAPIV3Schema") or {}, "")
    return found


def dig(obj, path):
    """Read a dotted path, tolerating absence -- an ABSENT field is not an empty one."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def violates(rule, live, want):
    """True if moving live->want is refused. Absent-vs-absent is not a change."""
    if live == want:
        return False
    if rule == "strict":
        return True
    # append-only: an INDEXED prefix compare, so order matters.
    if live is None:
        return False
    if want is None or not isinstance(live, list) or not isinstance(want, list):
        return True
    if len(want) < len(live):
        return True
    return any(want[i] != v for i, v in enumerate(live))


def parse_rendered(path):
    out = {}
    with open(path) as fh:
        for doc in yaml.safe_load_all(fh):
            if not doc or doc.get("kind") != "RestDefinition":
                continue
            out[doc["metadata"]["name"]] = doc
    return out


def parse_live(path, release):
    """Live RestDefinitions owned by one helm release, by helm's own release-name annotation."""
    with open(path) as fh:
        payload = json.load(fh)
    out, skipped = {}, 0
    for item in payload.get("items", []):
        anns = item["metadata"].get("annotations") or {}
        if anns.get("meta.helm.sh/release-name") != release:
            skipped += 1
            continue
        out[item["metadata"]["name"]] = item
    return out, skipped


def compare(rules, live, rendered, allowed):
    findings, declared_unseen = [], set(allowed)
    paired = sorted(set(live) & set(rendered))
    for name in paired:
        for path, rule in sorted(rules.items()):
            a, b = dig(live[name], path), dig(rendered[name], path)
            if not violates(rule, a, b):
                continue
            key = f"{name}:{path}"
            if key in allowed:
                declared_unseen.discard(key)
                continue
            findings.append((name, path, rule, a, b))
    return paired, findings, declared_unseen


def self_test(rules):
    """A gate that cannot see a change must not be trusted to report its absence."""
    live = {"probe": {"spec": {"resource": {"identifiers": ["number"],
                                            "additionalStatusFields": ["a", "b"]}}}}
    cases = [
        ("strict field changed", "identifiers", ["head", "base"], True),
        ("append at end", "additionalStatusFields", ["a", "b", "c"], False),
        ("reorder, same set", "additionalStatusFields", ["b", "a"], True),
        ("entry removed", "additionalStatusFields", ["a"], True),
        ("identical", "identifiers", ["number"], False),
    ]
    ok = True
    for label, field, value, must_flag in cases:
        rendered = copy.deepcopy(live)
        rendered["probe"]["spec"]["resource"][field] = value
        _, findings, _ = compare(rules, live, rendered, set())
        got = bool(findings)
        mark = "ok  " if got == must_flag else "FAIL"
        if got != must_flag:
            ok = False
        print(f"  {mark} {label:<22} -> flagged={got} (want {must_flag})")
    return ok


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--crd", required=True)
    ap.add_argument("--rendered")
    ap.add_argument("--live")
    ap.add_argument("--release")
    ap.add_argument("--expect-rules", type=int, default=7)
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    with open(args.crd) as fh:
        rules = extract_rules(json.load(fh))

    print(f"immutability rules read from the live CRD: {len(rules)}")
    for path, rule in sorted(rules.items()):
        print(f"  {rule:<11} {path}")

    if len(rules) != args.expect_rules:
        print(f"\nWITHHELD: CRD carries {len(rules)} immutability rules, expected "
              f"{args.expect_rules}. oasgen may have added one -- read it, decide whether the "
              f"gate covers it, then raise --expect-rules. Do NOT lower it to pass.")
        return 2

    if args.self_test:
        print("\n-- self-test: can the comparator see a change at all? --")
        return 0 if self_test(rules) else 3

    if not args.rendered or not args.live or not args.release:
        print("\nWITHHELD: --rendered, --live and --release are required unless --self-test")
        return 2

    rendered = parse_rendered(args.rendered)
    live, other_owners = parse_live(args.live, args.release)
    print(f"\nlive set scoped to helm release {args.release!r}: "
          f"{len(live)} owned, {other_owners} belonging to other releases ignored")
    if not live:
        print(f"WITHHELD: no live RestDefinition carries "
              f"meta.helm.sh/release-name={args.release!r}. Either the release name is wrong or "
              f"the component is not installed -- both make a 'no change' result meaningless.")
        return 2
    paired, findings, unseen = compare(rules, live, rendered,
                                       set(a.replace("/", ":") for a in args.allow))

    print(f"RestDefinitions: rendered={len(rendered)} live={len(live)} PAIRED={len(paired)}")
    if not paired:
        print("WITHHELD: zero names paired, so nothing was compared. The usual cause is the "
              "helm RELEASE NAME differing between the render and the cluster, which renames "
              "every object and reads as a clean result.")
        return 2

    only_new = sorted(set(rendered) - set(live))
    only_live = sorted(set(live) - set(rendered))
    for n in only_new:
        print(f"  new in the release (fresh CRD, no immutability risk): {n}")
    for n in only_live:
        print(f"  dropped by the release (oasgen refuses to drop a CRD with live CRs): {n}")

    if unseen:
        print(f"\nWITHHELD: declared with --allow but NOT observed: {sorted(unseen)}. A stale "
              f"--allow silences a future change under a name nobody rechecked.")
        return 2

    if not findings:
        print("\nok  no immutable-field change on any existing RestDefinition")
        return 0

    print(f"\nREFUSED: {len(findings)} immutable-field change(s) on EXISTING RestDefinitions.")
    print("oasgen will reject these in place; the composition retries forever at Ready=False.")
    for name, path, rule, a, b in findings:
        print(f"\n  {name}")
        print(f"    {path}   [{rule}]")
        print(f"      live     {a}")
        print(f"      release  {b}")
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
