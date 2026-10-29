#!/usr/bin/env python3
"""chart-root-schema-gate.py -- refuse a roll pinning a chart whose ROOT schema rejects injected values.

WHY. core-provider injects values into every composition chart that the chart's own CR does not
declare -- `global` is the one observed, and the CR's spec can be entirely empty while the
rendered values are not. A chart whose values.schema.json constrains its ROOT therefore rejects
values nobody wrote, chart-inspector returns 500 while templating, and the composition can NEVER
deploy. Not slowly, not intermittently: the schema rejects before anything installs.

TWO ROOT KEYS, ONE CLASS. Both have now wedged this installer:

  additionalProperties: false   krateo-audit-worm 0.1.1, WHEN the chart also fails to declare
      WITHOUT declaring         `global`. chart-inspector 500: "at '': additional properties
      `global`                  'global' not allowed". Diagnosed live on krateo-057, 2026-10-29.
  required: [...]               a blueprint chart's root `required:` wedges the umbrella the same
                                way, for the same reason -- see the memory note of that name.

THE PREDICATE IS A CONJUNCTION, AND GETTING THAT WRONG WOULD HAVE MADE THIS GATE USELESS. The
first version of this check refused any closed root. Run against a deliberately mixed set it
refused `frontend` 1.8.15 -- which is LIVE and Ready=True on 057. frontend closes its root AND
declares `global`, so the injected value validates:

  frontend 1.8.15         additionalProperties: false   declares global: YES   -> works
  krateo-audit-worm 0.1.1 additionalProperties: false   declares global: NO    -> 500
  builders-page 1.8.85    additionalProperties: true    declares global: NO    -> works

So neither condition alone predicts anything. A closed root is a perfectly good choice; closing
it while silently omitting the key your platform injects is the defect. Had this gate been
validated only against the chart that failed, it would have shipped refusing working charts --
which is how a gate teaches an operator to skip it.

WHY A GATE AND NOT A FIXED NOTE. The audit-worm defect was only found because the component was
NEW. A chart already deployed demonstrates its own root is open, so the latent cases are exactly
the ones nobody has installed yet -- a first install on a fresh cluster, or a feature enabled for
the first time years later. That is the worst possible moment to discover it.

A chart may legitimately close a SUB-object (`rdc`, `image`). This gate only examines the ROOT.

USAGE
  chart-root-schema-gate.py --pins <component-pins.yaml> [--only <name>]... [--all]
                            [--repo-default <oci://...>] [--self-test]

EXIT CODES
  0  every chart examined leaves its root permissive
  2  WITHHELD -- nothing was examined, or a chart could not be pulled. A gate that examined
     nothing must never report a pass.
  3  a chart's root schema would reject injected values: the roll is refused
"""
import argparse, json, pathlib, shutil, subprocess, sys, tempfile

try:
    import yaml
except ImportError:
    print("chart-root-schema-gate: PyYAML is required", file=sys.stderr)
    sys.exit(2)


def verdict(schema, defaults=None):
    """(ok, reason). Only the ROOT is examined; a closed sub-object is legitimate.

    `defaults` is the chart's own values.yaml. helm merges it BEFORE schema validation, so a
    root `required:` key the chart defaults itself is already satisfied and is not a defect.
    """
    if schema is None:
        return True, "no values.schema.json (nothing constrains the root)"
    defaults = defaults or {}
    props = schema.get("properties") or {}
    closed = schema.get("additionalProperties") is False
    declares_global = "global" in props
    bad = []
    if closed and not declares_global:
        bad.append("root additionalProperties: false AND does not declare 'global'")
    req = schema.get("required") or []
    unmet = [k for k in req if k not in defaults]
    if unmet:
        bad.append(f"root required keys not defaulted by the chart: {unmet}")
    if bad:
        return False, "; ".join(bad)
    note = (" / root required satisfied by chart defaults: " + str(req)) if req else ""
    if closed:
        return True, "root closed but declares 'global' (injected value validates)" + note
    return True, f"root open (additionalProperties={schema.get('additionalProperties')})" + note


def pull(repo, name, version, into):
    r = subprocess.run(["helm", "pull", f"{repo}/{name}", "--version", version,
                        "-d", str(into), "--untar"], capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().split("\n")[-1][:160]
    d = into / name
    if not d.is_dir():
        found = [p for p in into.iterdir() if p.is_dir()]
        if not found:
            return None, "pulled but no chart directory appeared"
        d = found[0]
    f = d / "values.schema.json"
    vf = d / "values.yaml"
    try:
        defaults = yaml.safe_load(vf.read_text()) if vf.is_file() else {}
    except Exception as e:
        return None, f"values.yaml is not valid YAML: {e}"
    if not f.is_file():
        return ("NO_SCHEMA", defaults or {}), None
    try:
        return (json.loads(f.read_text()), defaults or {}), None
    except Exception as e:
        return None, f"values.schema.json is not valid JSON: {e}"


# (schema, defaults, expected-ok)
SELF_TEST = [
    # The conjunction, both ways round. These two are the whole point of the gate.
    ("closed + no global -> refuse", {"additionalProperties": False, "properties": {}}, False),
    ("closed + declares global -> ok",
     {"additionalProperties": False, "properties": {"global": {"type": "object"}}}, True),
    ("open + no global -> ok", {"additionalProperties": True, "properties": {}}, True),
    ("root required NOT defaulted -> refuse", {"required": ["clickhouse"], "properties": {}}, False),
    ("root required defaulted by the chart -> ok",
     {"required": ["gateway"], "properties": {}}, True, {"gateway": {}}),
    ("unspecified root passes", {"properties": {}}, True),
    ("closed SUB-object passes", {"properties": {"rdc": {"additionalProperties": False}}}, True),
    ("empty required passes", {"required": [], "properties": {}}, True),
]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--repo-default", default="oci://ghcr.io/krateo-platformops/charts")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        print("-- self-test: does the verdict function discriminate? --")
        ok = True
        for case in SELF_TEST:
            label, schema, want = case[0], case[1], case[2]
            defaults = case[3] if len(case) > 3 else {}
            got, reason = verdict(schema, defaults)
            mark = "ok  " if got == want else "FAIL"
            if got != want:
                ok = False
            print(f"  {mark} {label:<26} -> ok={got} (want {want})  [{reason}]")
        return 0 if ok else 3

    if not args.pins:
        print("WITHHELD: --pins is required unless --self-test")
        return 2
    pins = yaml.safe_load(pathlib.Path(args.pins).read_text())
    comps = pins.get("components") or []
    if args.only:
        comps = [c for c in comps if c["name"] in set(args.only)]
    elif not args.all:
        print("WITHHELD: pass --only <name> (repeatable) or --all. Examining a chart set nobody "
              "named is how a gate ends up reporting a pass over an empty set.")
        return 2
    if not comps:
        print("WITHHELD: no component matched. Nothing was examined, so nothing passed.")
        return 2

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="root-schema-gate-"))
    refused, unreadable, examined = [], [], 0
    try:
        for c in comps:
            name, version = c["name"], str(c.get("version") or "")
            repo = c.get("repo") or args.repo_default
            chart = c.get("chart") or name
            if not version:
                unreadable.append((name, "no version pinned"))
                continue
            into = tmp / name
            into.mkdir(parents=True, exist_ok=True)
            got, err = pull(repo, chart, version, into)
            if err:
                unreadable.append((name, err))
                continue
            schema, defaults = got
            if schema == "NO_SCHEMA":
                examined += 1
                print(f"  ok   {name:<26} {version:<10} no values.schema.json")
                continue
            examined += 1
            good, reason = verdict(schema, defaults)
            if good:
                print(f"  ok   {name:<26} {version:<10} {reason}")
            else:
                print(f"  REFUSE {name:<24} {version:<10} {reason}")
                refused.append((name, version, reason))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\ncharts examined: {examined}   unreadable: {len(unreadable)}   refused: {len(refused)}")
    if unreadable:
        for n, e in unreadable:
            print(f"  could not examine {n}: {e}")
        print("WITHHELD: a chart could not be examined, so this run cannot report a pass.")
        return 2
    if not examined:
        print("WITHHELD: zero charts examined.")
        return 2
    if refused:
        print("\nREFUSED: these charts constrain their ROOT and will reject values that "
              "core-provider injects. chart-inspector returns 500 while templating and the "
              "composition never deploys -- deterministically, not intermittently.")
        for n, v, r in refused:
            print(f"  {n} {v}: {r}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
