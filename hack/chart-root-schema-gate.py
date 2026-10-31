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


def verdict(schema, defaults=None, cv_props=None):
    """(ok, reason). Only the ROOT is examined; a closed sub-object is legitimate.

    THE `defaults` EXEMPTION WAS WRONG AND THIS IS THE CORRECTION. This function used to read:

        `defaults` is the chart's own values.yaml. helm merges it BEFORE schema validation, so a
        root `required:` key the chart defaults itself is already satisfied and is not a defect.

    True for `helm install`. FALSE for the path every pinned component actually takes. A pin is
    deployed as a COMPOSITION: core-provider's crdgen builds a CRD from the chart's
    values.schema.json -- `required:` included -- and the CR's spec is built from the INSTALLER's
    componentValues. The chart's own values.yaml never participates in that validation.

    PROVEN on the one instance that reached a cluster, installer#489. cert-manager-issuers 0.10.0
    DOES default the key in its own values.yaml:

        values.yaml:  acme = {enabled: false, email: "", server: "https://...", gatewayRef: {...}}
        schema:       root required = [internalCA, acme],  acme.required = [enabled]

    and the composition was STILL rejected, because componentValues.cert-manager-issuers declared
    `acme: {type: object, additionalProperties: false}` with NO properties, so the installer could
    only emit `acme: {}`. Chart defaults present, CR rejected anyway.

    THE EXEMPTION HID A RECURRING CLASS -- all three instances default the key in values.yaml,
    which is exactly why it passed all three:
      Config Connector      spec.operator: Required value
      cert-manager-issuers  acme.enabled                      (installer#489, live)
      agentgateway 0.10.0   controllerName, gateway, gatewayClassName
    The last was reported `ok ... root required satisfied by chart defaults` by this gate.

    `defaults` is still accepted and still REPORTED, because knowing which keys the chart defaults
    is useful when deciding what the installer should declare. It no longer excuses anything.
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
    if req:
        if cv_props is None:
            # The component has no componentValues block, so nothing can supply these keys.
            bad.append(f"root required {req} and the installer declares NO componentValues block "
                       f"for this component — nothing can supply them")
        else:
            # cv_props may be a plain set (self-test shorthand) or the loader's detail dict.
            detail = cv_props if isinstance(cv_props, dict) else {k: {"default": True, "inexpressible": False} for k in cv_props}
            missing = [k for k in req if k not in detail]
            inexpr = [k for k in req if k in detail and detail[k]["inexpressible"]]
            nodefault = [k for k in req if k in detail and not detail[k]["inexpressible"]
                         and not detail[k]["default"]]
            if missing:
                bad.append(f"root required keys componentValues does not declare at all: {missing}")
            if inexpr:
                bad.append(f"root required keys componentValues declares as EMPTY objects, so the "
                           f"installer can only emit {{}}: {inexpr} (installer#489)")
            if nodefault:
                dflt = [k for k in nodefault if k in defaults]
                extra = (f"; the chart defaults {dflt} in its own values.yaml, which does NOT help -- "
                         f"crdgen keeps `required` in the CRD and the CR is built from "
                         f"componentValues, not values.yaml") if dflt else ""
                bad.append(f"root required keys declared with NO default, so an install that sets "
                           f"no componentValues emits a CR missing them: {nodefault}{extra}")
    if bad:
        return False, "; ".join(bad)
    note = (" / root required " + str(req) + " expressible via componentValues") if req else ""
    if closed:
        return True, "root closed but declares 'global' (injected value validates)" + note
    return True, f"root open (additionalProperties={schema.get('additionalProperties')})" + note


def componentvalues_props(component, installer_schema_path="chart/values.schema.json"):
    """Root property names the INSTALLER's componentValues declares for `component`.

    This is the surface a Composition CR's spec is built from, so it -- not the component chart's
    values.yaml -- decides whether a root `required:` key can be supplied. Returns None when the
    component has no componentValues block at all, which verdict() refuses.
    """
    try:
        sch = json.loads(pathlib.Path(installer_schema_path).read_text())
    except Exception:
        return None
    cv = (((sch.get("properties") or {}).get("componentValues") or {}).get("properties") or {})
    block = cv.get(component)
    if block is None:
        return None
    out = {}
    for k, v in (block.get("properties") or {}).items():
        v = v if isinstance(v, dict) else {}
        has_default = "default" in v
        # An object declared with NO properties is inexpressible whatever its default says: the
        # installer can only ever emit `{}` for it. That is the installer#489 shape.
        inexpressible_object = v.get("type") == "object" and not (v.get("properties") or {}) \
            and not v.get("additionalProperties", False) \
            and not v.get("x-kubernetes-preserve-unknown-fields", False)
        out[k] = {"default": has_default, "inexpressible": inexpressible_object}
    return out


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
    # THE CORRECTION, with each case's premise taken from the real schemas rather than invented.
    # An earlier version of this list gave agentgateway an EMPTY componentValues surface and so
    # "passed" on fiction -- the live --all run, which refused nothing, is what exposed it.

    # installer#489: componentValues declares the keys as objects with NO properties, so the
    # installer can only ever emit `{}`. Live-verified rejection.
    ("cert-manager-issuers#489",
     {"required": ["internalCA", "acme"],
      "properties": {"acme": {"type": "object"}, "internalCA": {"type": "object"},
                     "global": {"type": "object"}},
      "additionalProperties": False},
     False,
     {"acme": {"enabled": False}, "internalCA": {"enabled": True}},
     {"acme": {"default": False, "inexpressible": True},
      "internalCA": {"default": False, "inexpressible": True}}),

    # agentgateway 0.10.0: the keys ARE declared and expressible, each with default: null. So an
    # install setting no componentValues emits a CR missing all three.
    ("agentgateway 0.10.0",
     {"required": ["gatewayClassName", "controllerName", "gateway"],
      "properties": {"gatewayClassName": {"type": "string"}, "controllerName": {"type": "string"},
                     "gateway": {"type": "object"}, "global": {"type": "object"}},
      "additionalProperties": False},
     False,
     {"gatewayClassName": "agentgateway", "controllerName": "agentgateway.dev/agentgateway",
      "gateway": {"name": "krateo-gateway"}},
     {"gatewayClassName": {"default": False, "inexpressible": False},
      "controllerName": {"default": False, "inexpressible": False},
      "gateway": {"default": False, "inexpressible": False}}),

    # THE CONTROL that keeps the fix honest: a root required key that componentValues declares
    # WITH a default must still pass, or the gate refuses working charts.
    ("root required, declared with a default",
     {"required": ["foo"], "properties": {"foo": {"type": "string"}, "global": {"type": "object"}},
      "additionalProperties": False},
     True, {}, {"foo": {"default": True, "inexpressible": False}}),

    # A SECOND CONTROL: the shorthand set form must still mean "expressible and defaulted".
    ("root required, shorthand set form",
     {"required": ["bar"], "properties": {"bar": {"type": "string"}, "global": {"type": "object"}},
      "additionalProperties": False},
     True, {}, {"bar"}),

    # The two arms already validated, which must not regress.
    ("closed root, declares global",
     {"additionalProperties": False, "properties": {"global": {"type": "object"}}}, True, {}, set()),
    ("closed root, NO global (audit-worm)",
     {"additionalProperties": False, "properties": {}}, False, {}, set()),
    ("open root, no global (builders-page)",
     {"additionalProperties": True, "properties": {}}, True, {}, set()),
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
            cvp = case[4] if len(case) > 4 else set()
            got, reason = verdict(schema, defaults, cvp)
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
            good, reason = verdict(schema, defaults, componentvalues_props(name))
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
