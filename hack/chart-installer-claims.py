#!/usr/bin/env python3
"""Surface the places where a pinned chart CLAIMS the installer does something for it.

WHY THIS EXISTS. On 2026-11-21 agentiko-mcp-server audited the installer-side claims in their own
two charts, at their own initiative, and found TWO OF FOUR FALSE -- both written ahead of
implementations that never landed:

    auth.mapping.oidcConfig / subjectAudiences
        values.yaml: "Under the Krateo installer it is derived from
        componentValues.krateo-keycloak-realm.oidcConfig.name, and it is not a componentValues key
        of this chart: setting it there is refused at chart load."
        REALITY: compositions.yaml contains no reference to oidcConfig, subjectAudiences or
        ideClient. No derivation, no refusal. Consequence: the chart's own `fail` aborts the render
        when the value is empty, and a chart render failure aborts the WHOLE umbrella.

    imagePullSecrets
        values.yaml: "The Krateo installer wires the credential from registryAuth via
        componentValues."
        REALITY: inst.privateImageComponents is an ENUMERATED LIST and that component is not in it,
        so nothing is injected. The image is private, so the pod is ImagePullBackOff -- and this one
        does NOT fail at render: the composition reconciles, the release installs, every condition
        reads green, and the failure appears as a pod that never starts.

NEITHER WAS FINDABLE FROM THIS REPO. Nothing compares a pinned chart's documented installer-side
behaviour against what compositions.yaml actually derives, and there was no reason to go looking.
Both were found because the chart's owner volunteered an audit of their own claims. There are 93
pinned components; that was a 50% failure rate in the one pair anyone has checked.

WHAT THIS CAN AND CANNOT DO. It CANNOT decide whether a claim is true -- that needs reading
compositions.yaml and _helpers.tpl and understanding the mechanism. So it REPORTS AND EXITS 0 by
default rather than failing a build: a gate that cannot pass teaches people to skip it, and a gate
that guesses teaches people to distrust it. Its job is to put the claim in front of a human at the
moment a pin is proposed, which is the only moment anyone is already reading that chart.

SCOPE: by default, only components whose pin CHANGED against the merge target. That is 1-3 charts
on a normal pin PR, so it is fast, and a NEW claim appears in the PR that introduces it. `--all`
sweeps every pinned component, for a periodic audit rather than for CI.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile

import yaml

PINS = "chart/files/component-pins.min.yaml"

# Phrases that assert the installer does something. Deliberately broad: a false positive costs a
# glance, a false negative costs an ImagePullBackOff nobody can explain.
# Phrases that ASSERT the installer does something. `componentValues.<x>` on its own was tried and
# dropped: it matched every schema description that merely REFERENCES a path, giving 19 hits on one
# chart where 2 mattered. A pattern nobody reads is the same as no pattern.
CLAIM = re.compile(
    r"(?i)("
    r"krateo installer"
    r"|installer (?:wires|sets|derives|injects|provides|fills|supplies|resolves)"
    r"|(?:wired|derived|set|injected|supplied|resolved) (?:from|by) (?:the )?installer"
    r"|derived from componentValues"
    r"|wired from registryAuth"
    r"|refused at chart load"
    r"|not a componentValues key"
    r")"
)

# Files worth reading. templates/ is excluded: a claim there is usually a comment ABOUT a template's
# own behaviour, and the signal-to-noise is poor.
TARGETS = ("values.yaml", "values.schema.json")


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def pins_at(ref=None):
    if ref:
        t = sh(["git", "show", "%s:%s" % (ref, PINS)]).stdout
    else:
        t = io.open(PINS, encoding="utf-8").read()
    d = yaml.safe_load(t) or {}
    return {c["name"]: c for c in d.get("components", [])}


def chart_ref(c):
    repo = c.get("repo") or "oci://ghcr.io/krateo-platformops/charts"
    return "%s/%s" % (repo.rstrip("/"), c.get("chart") or c["name"])


def claims_in(path, rel):
    """(rel_path, line_no, text) for every line asserting installer behaviour."""
    out = []
    try:
        raw = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return out
    for i, line in enumerate(raw.split("\n"), 1):
        s = line.strip().lstrip("#").strip()
        if not s:
            continue
        if CLAIM.search(s):
            out.append((rel, i, s))
    return out


def scan(name, comp, keep_dir=None):
    ref, ver = chart_ref(comp), comp.get("version")
    if not ver:
        return None, "no version pinned"
    with tempfile.TemporaryDirectory() as td:
        dest = keep_dir or td
        r = sh(["helm", "pull", ref, "--version", str(ver), "-d", dest, "--untar"], timeout=600)
        if r.returncode:
            return None, (r.stderr or r.stdout).strip()[:160]
        root = None
        for d in os.listdir(dest):
            p = os.path.join(dest, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "Chart.yaml")):
                root = p
                break
        if not root:
            return None, "no chart dir after untar"
        found = []
        for t in TARGETS:
            p = os.path.join(root, t)
            if os.path.exists(p):
                found += claims_in(p, t)
        return found, None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="sweep every pinned component (periodic audit, not CI)")
    ap.add_argument("--base", default="origin/main",
                    help="merge target to diff pins against (default origin/main)")
    ap.add_argument("--only", help="comma-separated component names, overrides --base")
    ap.add_argument("--json", dest="as_json", action="store_true")
    a = ap.parse_args(argv[1:])

    now = pins_at()
    if a.only:
        names = [n.strip() for n in a.only.split(",") if n.strip() in now]
    elif a.all:
        names = sorted(now)
    else:
        base = pins_at(a.base)
        if not base:
            print("REFUSED: cannot read %s at %s -- fetch first" % (PINS, a.base))
            return 2
        names = sorted(n for n in set(now) & set(base) if now[n] != base[n])
        names += sorted(set(now) - set(base))

    if not names:
        print("chart-installer-claims: no pin changed against %s -- nothing to read" % a.base)
        return 0

    print("chart-installer-claims: reading %d chart(s)" % len(names))
    print("  THIS REPORTS, IT DOES NOT VERIFY. A claim below may be true, false, or stale.")
    print("  To settle one, read compositions.yaml and _helpers.tpl for the mechanism it names.")
    print()

    results, failures, total = {}, {}, 0
    for n in names:
        found, err = scan(n, now[n])
        if err:
            failures[n] = err
            continue
        results[n] = found
        total += len(found)

    for n in sorted(results):
        rows = results[n]
        if not rows:
            continue
        print("  %s %s" % (n, now[n].get("version")))
        for rel, line, text in rows:
            print("    %s:%d" % (rel, line))
            print("      %s" % (text if len(text) <= 150 else text[:147] + "..."))
        print()

    clean = [n for n in sorted(results) if not results[n]]
    if clean:
        print("  no installer-side claim: %s" % ", ".join(clean))
    if failures:
        print()
        print("  COULD NOT READ (a gap in this report, not a statement about the chart):")
        for n, e in sorted(failures.items()):
            print("    %-26s %s" % (n, e))

    print()
    print("chart-installer-claims: %d claim(s) across %d chart(s); %d unreadable" %
          (total, len([n for n in results if results[n]]), len(failures)))
    if a.as_json:
        io.open("chart-installer-claims.json", "w", encoding="utf-8").write(
            json.dumps({"claims": results, "unreadable": failures}, indent=2) + "\n")
    # Always 0: see the module docstring. An unreadable chart is reported, not fatal.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
