#!/usr/bin/env python3
"""Refuse a component pin that is AHEAD of its paired -crd pin in a way that loses data.

WHY THIS EXISTS. installer#632 bumped `alert-provider` 0.2.57 -> 0.2.59 and left
`alert-provider-crd` at 0.2.57. The two fields 0.2.59 adds -- spec.ratio and
spec.thresholdMax -- are declared ONLY in the CRD chart, and the 0.2.57 CRD sets neither
`additionalProperties: false` nor `x-kubernetes-preserve-unknown-fields`. So the apiserver
PRUNES them: an Alert carrying either is accepted, returns no error, emits no event, and comes
back without the field. `thresholdMax` is required for thresholdType between/not_between, so a
range alert is silently malformed rather than refused. All 25 CI checks passed on that PR,
because nothing compared a component against its own CRD chart. Tag 0.3.495 was cut from it 38
seconds after merge and published. Fixed by installer#634 / 0.3.496; this closes the class.

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
