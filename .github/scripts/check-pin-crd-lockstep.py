#!/usr/bin/env python3
"""Refuse a component pin that is AHEAD of its paired -crd pin in a way that loses data.

WHY THIS EXISTS. installer#632 bumped `alert-provider` 0.2.57 -> 0.2.59 and left
`alert-provider-crd` at 0.2.57. The two fields 0.2.59 adds -- spec.ratio and
spec.thresholdMax -- are declared ONLY in the CRD chart, and the 0.2.57 CRD sets neither
`additionalProperties: false` nor `x-kubernetes-preserve-unknown-fields`. So the apiserver
PRUNES them: an Alert carrying either is accepted, returns no error, emits no event, and comes
back without the field.

The FIELD LOSS is silent; the CONSEQUENCE, for this component, is not. Measured from
alert-provider's code (main 8d83011) and HyperDX 2.35.0: a `between` Alert missing thresholdMax is
refused by HyperDX's own schema with a 400, so the push fails and the Alert does not go Synced. So
this particular skew fails visibly. That is NOT guaranteed in general -- whether a dropped field
becomes a loud failure or a silently wrong computation depends entirely on what reads it
downstream, which this gate cannot see. It refuses the skew either way.

All 25 CI checks passed on that PR, because nothing compared a component against its own CRD chart.
A whole-suite green says nothing about a dimension none of the checks measure: 25 of 25 is a
denominator problem, and the only answer to a denominator problem is to add the missing dimension.
Tag 0.3.495 was cut from it 38 seconds after merge and published. installer#634 fixed the instance;
this closes the class.

COVERAGE, stated because it is NOT universal. This gate sees the CRDs of charts reachable
through an `<x>`/`<x>-crd` PAIR. Per a survey of all 68 pins (2026-11-04): 11 components ship
CRDs via a separate `-crds` chart and are covered here; `portal` ships its CRD inside
`templates/` and has NO `-crd` sibling, so it is NOT covered by this gate and must be diffed by
hand until it is paired or a second gate renders it; the other 56 pins ship no CRDs at all.
A new CRD-shipping chart outside a pair would be invisible here -- that is a known hole, not an
oversight, and it is why the coverage is written down.

Discovery is by `kind: CustomResourceDefinition` over the RENDERED documents, never by filename.
frontend's own equivalent gate globbed `templates/*.crd.yaml` and thereby skipped the one
hand-written CRD in a subdirectory with a different extension -- the file a person is most likely
to get wrong. A filename convention is a naming habit, not a contract.

WHAT IT CHECKS, per `<x>` / `<x>-crd` pair in files/component-pins.yaml:
  versions equal                      -> OK, nothing to compare.
  pair DECLARED independent            -> SKIP, named, from
                                         .github/pin-crd-independent-lines.yaml. The declaration
                                         is VERIFIED when the chart is readable: if the CRD chart
                                         does exist at the app's version the declaration is stale
                                         and this FAILS. A 404 is NOT used as the evidence -- an
                                         authenticated reader sees 404 where an anonymous CI
                                         runner sees 401 for the same private package, so that
                                         inference depended on the caller's credentials.
  CRD chart present at both versions  -> render both and compare. FAIL if the app's version
                                         declares spec properties the PINNED crd version lacks,
                                         or adds a CRD, or changes an immutable field.

WHY IT MUST NOT FAIL OPEN. A gate that cannot complete its check reads exactly like one that
found nothing wrong. Any render failure, pull failure or unparseable chart is an ERROR that
fails the job, never a skip -- the only skip is the measured 404 above, and it is named.
"""
import json, os, subprocess, sys, tempfile

PINS = os.environ.get("PINS", "chart/files/component-pins.yaml")
DEFAULT_REPO = os.environ.get("OCI_REPO", "oci://ghcr.io/krateo-platformops/charts")
IMMUTABLE = ("group", "scope", "conversion", "preserveUnknownFields")
DECL_FILE = os.environ.get("DECL", ".github/pin-crd-independent-lines.yaml")

def die(msg):
    print("::error::%s" % msg, file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    die("PyYAML is required by check-pin-crd-lockstep.py")

def sh(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def render(repo, chart, version, fatal=True):
    """(docs, None) | (None, 'absent') | (None, 'unreadable'). Dies on anything else when fatal."""
    p = sh(["helm", "template", "probe", "%s/%s" % (repo.rstrip("/"), chart),
            "--version", version, "--namespace", "probe", "--include-crds"])
    if p.returncode != 0:
        blob = (p.stdout + p.stderr).lower()
        # 401/403 must NOT be read as "absent": a private package answers 401 to an anonymous
        # reader and 404 to an authenticated one, so conflating them makes the verdict depend on
        # who is asking.
        if "401" in blob or "403" in blob or "unauthorized" in blob or "denied" in blob:
            return None, "unreadable"
        if "not found" in blob or "404" in blob:
            return None, "absent"
        if not fatal:
            return None, "unreadable"
        die("could not render %s/%s %s -- this gate cannot vouch for a chart it could not "
            "render, so it fails rather than skipping: %s"
            % (repo, chart, version, p.stderr.strip()[:300]))
    try:
        docs = [d for d in yaml.safe_load_all(p.stdout) if d]
    except Exception as e:
        die("rendered %s/%s %s but could not parse it: %s" % (repo, chart, version, e))
    return docs, None

def crds_of(docs):
    return {d["metadata"]["name"]: d for d in docs
            if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition"}

def prop_paths(crd):
    """{version_name: set(full nested property paths)}.

    spec_props() below reads only TOP-LEVEL spec properties, which its own docstring is honest
    about. That is enough for the app-ahead direction it was written for, but it cannot see a
    NESTED addition -- authn 0.47.0 adds spec.delegation.subjectTokens and
    spec.delegation.audiences while `delegation` itself already existed, so the top-level set is
    unchanged and the pair reads identical. This walks properties by NAME and array items as `[]`,
    the same shape as hack/crd-tightening-diff.py.
    """
    out = {}
    for v in crd["spec"]["versions"]:
        root = (v.get("schema") or {}).get("openAPIV3Schema") or {}
        acc = set()

        def walk(node, path):
            if not isinstance(node, dict):
                return
            for k, child in (node.get("properties") or {}).items():
                acc.add("%s.%s" % (path, k))
                walk(child, "%s.%s" % (path, k))
            if isinstance(node.get("items"), dict):
                walk(node["items"], path + "[]")

        walk((root.get("properties") or {}).get("spec") or {}, "spec")
        out[v["name"]] = acc
    return out


def vorder(v):
    """A comparable tuple for a plain-semver version. None when it is not plain semver.

    Returns None rather than guessing: the reverse-direction check below runs only when the
    direction is KNOWN, and an undetermined direction is REPORTED rather than silently skipped.
    """
    parts = str(v).split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def spec_props(crd):
    """{version_name: sorted(spec.properties)} for every version the CRD declares."""
    out = {}
    for v in (crd.get("spec", {}).get("versions") or []):
        root = (v.get("schema") or {}).get("openAPIV3Schema") or {}
        spec = (root.get("properties") or {}).get("spec") or {}
        out[v["name"]] = sorted(spec.get("properties") or {})
    return out

def guards(crd):
    """Whether an undeclared spec field would be REJECTED rather than pruned."""
    for v in (crd.get("spec", {}).get("versions") or []):
        root = (v.get("schema") or {}).get("openAPIV3Schema") or {}
        spec = (root.get("properties") or {}).get("spec") or {}
        if spec.get("additionalProperties") is False: return True
        if spec.get("x-kubernetes-preserve-unknown-fields"): return True
    return False

CONSTRAINT_KEYS = ("required", "enum", "type", "format",
                   "x-kubernetes-preserve-unknown-fields", "additionalProperties")

def unsatisfiable(crd):
    """Every `required` entry naming a property its SIBLING `properties` does not define.

    This is a STANDALONE assertion: it needs no pair, no baseline, no fixtures and no cluster.
    A structural schema prunes unknown fields BEFORE validation, so supplying the field does not
    help -- it is pruned, then `required` fails. Such an item can NEVER validate. It is not a
    tightening, it is unsatisfiable, and no amount of data makes it pass.

    WHY THIS EXISTS (frontend 1.8.20, 2026-11-04). 44 of 45 widget CRDs shipped
    `required: ['id']` under spec.resourcesRefsTemplate.items with properties only
    {iterator, template}. Three of our instruments missed it IN A ROW and each was individually
    correct: schema LENGTH changed on 44 of 45 (noise), top-level spec properties read +[] -[]
    (this file's old check), and a deep walk of nested property PATHS read 42 added / 0 removed.
    A `required` entry is a CONSTRAINT, not a path, so the path walk could not see it -- and
    "purely additive, safe to pin" followed validly from an instrument blind to the thing that
    mattered. 29 live widget CRs on each cluster carry a resourcesRefsTemplate and would have
    been rejected on the next apply, surfacing at the PORTAL step, after frontend reported success.
    """
    out = []
    def walk(node, path):
        if isinstance(node, dict):
            req, props = node.get("required"), node.get("properties")
            if isinstance(req, list):
                if isinstance(props, dict):
                    for r in req:
                        if r not in props:
                            out.append((path or ".", r, sorted(props)))
                else:
                    for r in req:
                        out.append((path or ".", r, "NO sibling `properties` at all"))
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
    walk(crd.get("spec", {}), "")
    return out

def constraints(crd):
    """{path: {key: value}} for every CONSTRAINT-bearing schema node.

    The property-path set is not enough: a pin can tighten a schema without adding or removing a
    single path -- a new `required`, a narrowed `enum`, a changed `type`, or pruning switched on.
    Those reject or drop data that validated before, and the old check could not see any of them.
    """
    out = {}
    def walk(node, path):
        if isinstance(node, dict):
            got = {k: node[k] for k in CONSTRAINT_KEYS if k in node}
            if got:
                out[path or "."] = got
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
    walk(crd.get("spec", {}), "")
    return out

def tightened(old, new):
    """Constraint changes in `new` that would REJECT or DROP data `old` accepted."""
    bad = []
    for path, nc in new.items():
        oc = old.get(path, {})
        on, nn = set(oc.get("required") or []), set(nc.get("required") or [])
        if nn - on:
            bad.append("%s: `required` GAINS %s" % (path, sorted(nn - on)))
        oe, ne = oc.get("enum"), nc.get("enum")
        if isinstance(oe, list) and isinstance(ne, list) and set(oe) - set(ne):
            bad.append("%s: `enum` LOSES %s" % (path, sorted(set(oe) - set(ne))))
        if "type" in oc and "type" in nc and oc["type"] != nc["type"]:
            bad.append("%s: `type` %r -> %r" % (path, oc["type"], nc["type"]))
        if oc.get("x-kubernetes-preserve-unknown-fields") and not nc.get("x-kubernetes-preserve-unknown-fields"):
            bad.append("%s: preserve-unknown-fields WITHDRAWN -- unknown fields now pruned" % path)
        if oc.get("additionalProperties") is not False and nc.get("additionalProperties") is False:
            bad.append("%s: additionalProperties now false -- unknown fields now REJECTED" % path)
    return bad

DECLARED = {}
if os.path.exists(DECL_FILE):
    dd = yaml.safe_load(open(DECL_FILE, encoding="utf-8")) or {}
    for e in (dd.get("independent") or []):
        DECLARED[(e["app"], e["crd"])] = (e.get("reason") or "").strip()
    print("declared independent-line pairs: %d (from %s)" % (len(DECLARED), DECL_FILE))
else:
    print("no %s present; every differing pair must be comparable" % DECL_FILE)

doc = yaml.safe_load(open(PINS, encoding="utf-8"))
items = doc["components"] if isinstance(doc, dict) else doc
if not isinstance(items, list) or len(items) < 20:
    die("FLOOR: parsed %s pins from %s -- refusing to report on a list this short"
        % (len(items) if isinstance(items, list) else "?", PINS))
by = {c["name"]: c for c in items if isinstance(c, dict) and c.get("name")}

pairs = []
for name, c in sorted(by.items()):
    if name.endswith(("-crd", "-crds")):
        continue
    for suf in ("-crd", "-crds"):
        o = by.get(name + suf)
        if o:
            pairs.append((name, name + suf, c, o))
            break

if not pairs:
    die("FLOOR: found 0 app/-crd pairs in %s. This gate's whole population is empty, which "
        "means the pairing rule stopped matching -- that is a defect in the gate, not a pass."
        % PINS)

print("pins parsed: %d    app/-crd pairs found: %d" % (len(items), len(pairs)))
failures, skips, checked = [], [], 0

for app_name, crd_name, app, crd in pairs:
    av, cv = str(app.get("version")), str(crd.get("version"))
    crd_chart = crd.get("chart", crd_name)
    repo = crd.get("repo") or DEFAULT_REPO
    if av == cv:
        print("  OK      %-26s %-10s == %-26s %s" % (app_name, av, crd_name, cv))
        checked += 1
        continue

    declared = DECLARED.get((app_name, crd_name))
    at_app, why = render(repo, crd_chart, av, fatal=not declared)
    if declared:
        if at_app is not None:
            failures.append(
                "%s/%s is DECLARED on independent version lines in %s, but %s %s DOES exist -- the "
                "declaration is stale and must be removed, then the pair compared properly. Reason "
                "on file: %s" % (app_name, crd_name, DECL_FILE, crd_chart, av, declared))
            continue
        skips.append((app_name, av, crd_name, cv, crd_chart, why))
        print("  SKIP    %-26s %-10s vs %-26s %-10s  -- declared independent lines (%s); probe "
              "said %s, declaration holds"
              % (app_name, av, crd_name, cv, DECL_FILE, why))
        continue
    if why == "unreadable":
        failures.append(
            "%s is pinned at %s and %s at %s, and %s/%s could not be READ (401/403) so the pair "
            "cannot be compared. Either grant this job read access to that registry, or -- if the "
            "two charts are genuinely on independent version lines -- declare the pair in %s with "
            "the registry evidence. This is NOT skipped: an unreadable pair is indistinguishable "
            "from the data-losing one." % (app_name, av, crd_name, cv, repo, crd_chart, DECL_FILE))
        continue
    if why == "absent":
        failures.append(
            "%s is pinned at %s but %s/%s does not exist at %s. If the two charts are on "
            "independent version lines, declare the pair in %s with the registry evidence rather "
            "than leaving the gate to infer it from a 404 -- that inference changes answer "
            "depending on whether the reader is authenticated."
            % (app_name, av, repo.rstrip("/"), crd_chart, av, DECL_FILE))
        continue

    at_pin, absent2 = render(repo, crd_chart, cv)
    if absent2:
        failures.append("%s: the PINNED %s %s does not exist in %s, so the pin cannot install"
                        % (app_name, crd_name, cv, repo))
        continue

    ca, cp = crds_of(at_app), crds_of(at_pin)
    vo_app, vo_pin = vorder(av), vorder(cv)
    if vo_app is None or vo_pin is None:
        print("  NOTE    %s: %r / %r is not plain semver, so the REVERSE direction (CRDs ahead of "
              "the app) was NOT evaluated for this pair. Reported rather than skipped silently."
              % (app_name, av, cv))

    # --- STANDALONE: is the CRD this pin would INSTALL even satisfiable?
    # Needs no pair, no baseline, no cluster. Runs on the PINNED side, because that is what the
    # cluster gets. Also on the app side, since that is what the pin is being compared against.
    for side, label, ver in ((cp, crd_name, cv), (ca, crd_chart, av)):
        for cname, cobj in sorted(side.items()):
            bad = unsatisfiable(cobj)
            if bad:
                shown = "; ".join("%s requires %r but siblings are %s" % (pth, r, sib)
                                  for pth, r, sib in bad[:3])
                failures.append(
                    "%s %s ships an UNSATISFIABLE CRD %s: %d `required` entry/entries name a "
                    "property their sibling `properties` does not define -- %s%s. Pruning strips "
                    "unknown fields BEFORE validation, so supplying the field does not help: it is "
                    "pruned, then `required` fails. No object can ever validate against this."
                    % (label, ver, cname, len(bad), shown,
                       " (+%d more)" % (len(bad) - 3) if len(bad) > 3 else ""))

    # --- CONSTRAINT TIGHTENING between the two sides, which the property-path set cannot see
    for cname in sorted(set(ca) & set(cp)):
        t = tightened(constraints(cp[cname]), constraints(ca[cname]))
        if t:
            failures.append(
                "%s: %s TIGHTENS constraints between pinned %s and app %s, which rejects or drops "
                "data the pinned schema accepts: %s%s"
                % (app_name, cname, cv, av, "; ".join(t[:4]),
                   " (+%d more)" % (len(t) - 4) if len(t) > 4 else ""))

    added_crds = sorted(set(ca) - set(cp))
    if added_crds:
        failures.append("%s %s ships CRD(s) the pinned %s %s does not: %s"
                        % (crd_chart, av, crd_name, cv, added_crds))

    for cname in sorted(set(ca) & set(cp)):
        pa, pp = spec_props(ca[cname]), spec_props(cp[cname])
        for vn in sorted(set(pa) & set(pp)):
            missing = sorted(set(pa[vn]) - set(pp[vn]))
            if missing:
                how = ("REJECTED (the pinned CRD guards spec)" if guards(cp[cname])
                       else "PRUNED SILENTLY: accepted, no error, no event, value dropped")
                failures.append(
                    "%s is pinned at %s but %s is held at %s. %s/%s declares spec properties the "
                    "pinned CRD lacks: %s. An object written with them would be %s."
                    % (app_name, av, crd_name, cv, cname, vn, missing, how))
        # --- REVERSE DIRECTION: the PINNED CRD is NEWER than the app.
        # Added 2026-11-14 at acmp's request after they described the harm precisely: a newer CRD
        # admits Alert/Metric SPEC fields that the older controller accepts and then silently never
        # acts on -- a write that succeeds with no effect. Their CRDs carry spec fields, not only
        # status, and the controller has no unknown-field rejection, so nothing surfaces.
        # Measured on authn's pair the same morning: this direction passed this gate
        # unconditionally, correctly by its old contract, because nothing is LOST when the CRDs are
        # the newer side. Nothing is lost; something is INERT, which is a different harm.
        # BY CONTENT and only when the direction is KNOWN, so an app-only release whose CRD content
        # is byte-identical still passes -- acmp's -crds chart exists to decouple the CRD lifecycle
        # from app-only bumps (its Chart.yaml cites core-provider#89), and holding -crds at the
        # older version in that case is the INTENDED state. A version-equality rule would forbid it,
        # which is why alert-provider is deliberately NOT a declared version line. acmp agreed and
        # withdrew the request -- "my yes was the wrong tool for the harm I described" -- and their
        # verdict, verbatim: "holding alert-provider-crds behind the app when the CRD content is
        # unchanged is an intended state. A CRD ahead of the app is harmful only when it adds SPEC
        # paths the app's own version lacks. Status-only additions ahead of the app, like 0.2.83's
        # stateSince, are fine."
        # WHY THE NESTED WALK IS THE LOAD-BEARING PART, in their words: Alert's spec is deeply
        # nested (howToFix.applyAction, groupBy, threshold forms), so a top-level-only compare
        # "would have missed exactly the additions I'd make". The live proof is authn's pair, where
        # delegation already existed and all four new paths sit under it.
        if vo_app is not None and vo_pin is not None and vo_pin > vo_app:
            qa, qp = prop_paths(ca[cname]), prop_paths(cp[cname])
            for vn in sorted(set(qa) & set(qp)):
                inert = sorted(qp[vn] - qa[vn])
                if inert:
                    failures.append(
                        "%s is pinned at %s but %s is AHEAD at %s. %s/%s declares %d spec "
                        "propert(ies) the app's own version does not implement: %s%s. An object "
                        "written with them is ACCEPTED and then silently never acted on -- a write "
                        "that succeeds with no effect."
                        % (app_name, av, crd_name, cv, cname, vn, len(inert),
                           inert[:6], " (+%d more)" % (len(inert) - 6) if len(inert) > 6 else ""))
            extra_crds = sorted(set(cp) - set(ca))
            if extra_crds:
                failures.append(
                    "%s is pinned at %s but %s is AHEAD at %s and ships CRD(s) the app's version "
                    "does not know: %s. Those kinds would be installed and never reconciled."
                    % (app_name, av, crd_name, cv, extra_crds))
        sa, sb = ca[cname]["spec"], cp[cname]["spec"]
        for f in IMMUTABLE:
            if sa.get(f) != sb.get(f):
                failures.append("%s: %s immutable field %r differs between %s and %s: %r vs %r"
                                % (app_name, cname, f, av, cv, sa.get(f), sb.get(f)))
        if sa.get("names") != sb.get("names"):
            failures.append("%s: %s `names` differs between %s and %s" % (app_name, cname, av, cv))
    checked += 1
    print("  CHECKED %-26s %-10s vs %-26s %-10s  (%d CRD(s) compared)"
          % (app_name, av, crd_name, cv, len(set(ca) & set(cp))))

print()
print("both directions compared by CONTENT: app-ahead (fields PRUNED) and crds-ahead (fields INERT)")
print("pairs: %d    compared or equal: %d    skipped as independent lines: %d"
      % (len(pairs), checked, len(skips)))
if checked == 0:
    die("FLOOR: every pair was skipped, so this run compared nothing. A gate that checked "
        "nothing must not report success.")
if failures:
    print()
    for f in failures:
        print("::error::%s" % f)
    print("::error::%d lockstep violation(s). Bump the -crd pin to match, or state in the pin "
          "comment why holding it is safe -- and diff the charts to establish that rather than "
          "inferring it from the component's history." % len(failures), file=sys.stderr)
    sys.exit(1)
print("no lockstep violations.")
