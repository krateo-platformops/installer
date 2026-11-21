#!/usr/bin/env python3
"""Report the strings in a pin's CRDs that will trip the sandbox capture's credential scan.

WHY THIS EXISTS. installer 0.3.692 pinned external-secrets, whose 25 CRDs carry a PEM example in a
schema `description` and an `external-secrets.io/component` label. The sandbox capture's credential
scan matched both, and marketing's run 12 STOPPED AT STEP 4 on two false positives -- a run spent
on a change of mine that nobody had looked at from their side.

The scan is right to stop: it cannot tell a placeholder in documentation from a leaked key. So the
fix is not to loosen it but to tell them BEFORE the roll which strings a pin introduces, so they can
pin an allowlist (object + path + pattern + sha256 of the matched string) in a reviewed PR.
The sha256 is what makes it safe: if the text later CHANGES, it flags again.

PATTERNS AND KEY_HINT ARE COPIED VERBATIM from agentiko-sandbox capture/dryrun.py. They are
duplicated rather than imported because that repo is another session's and is not a dependency of
this one -- so `--verify-patterns` re-reads the source file when it is available locally and refuses
if they have drifted. Never edit them to make an output nicer.

Usage:
  crd-scan-preview.py --chart <oci-ref> --old <ver> --new <ver>   what a pin ADDS
  crd-scan-preview.py --chart <oci-ref> --new <ver>               everything at that version
  crd-scan-preview.py --live --context <ctx> --group <apigroup>   what is on a cluster now
  crd-scan-preview.py --verify-patterns [path]                    check against the real source
"""
import argparse, hashlib, io, json, os, re, subprocess, sys, tempfile, glob

# --- verbatim from agentiko-sandbox capture/dryrun.py ---
PATTERNS = {
    "pem_private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "gcp_sa_json": re.compile(r'"private_key_id"\s*:'),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "github_token": re.compile(r"\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "basic_auth_url": re.compile(r"://[^/\s:@]+:(?!\$\()[^/\s@]+@"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),
}
KEY_HINT = re.compile(r"(?i)(password|passwd|secret|token|apikey|api_key|private_key|client_secret)")
SANDBOX_SRC = os.path.expanduser("~/krateo/agentiko-sandbox/capture/dryrun.py")


def verify_patterns(path):
    """Refuse if our copy has drifted from the scan's own source."""
    if not os.path.exists(path):
        print("  SKIP: %s not present; cannot verify. Patterns are the copy in this file." % path)
        return 0
    s = io.open(path, encoding="utf-8").read()
    bad = 0
    for name, rx in PATTERNS.items():
        if ('"%s"' % name) not in s:
            print("  DRIFT: pattern %r is not in the source any more" % name); bad = 1
    src_names = set(re.findall(r'^\s*"([a-z0-9_]+)":\s*re\.compile', s, re.M))
    for n in sorted(src_names - set(PATTERNS)):
        print("  DRIFT: the source has pattern %r and we do not" % n); bad = 1
    for name, rx in PATTERNS.items():
        if rx.pattern not in s:
            print("  DRIFT: %r regex text differs from the source" % name); bad = 1
    if KEY_HINT.pattern not in s:
        print("  DRIFT: KEY_HINT differs from the source"); bad = 1
    print("  patterns %s against %s" % ("DRIFTED" if bad else "match", path))
    return bad


def crds_from_dir(root):
    out = []
    for f in glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True):
        try:
            import yaml
            docs = list(yaml.safe_load_all(io.open(f, encoding="utf-8").read()))
        except Exception:
            continue
        for d in docs:
            if isinstance(d, dict) and d.get("kind") == "CustomResourceDefinition":
                out.append(d)
    return out


def findings(crd):
    """Every (path, pattern, matched_string) in one CRD that the scan would flag."""
    name = crd["metadata"]["name"]
    hits = []

    def walk(node, path):
        """Mirrors capture/dryrun.py scan() EXACTLY, including its path format.

        THE CONDITION THAT MATTERS IS `isinstance(v, str) and v`. My first version applied KEY_HINT
        to every dict key and produced a flood: every schema PROPERTY NAME containing "secret"
        (secretKey, secretStoreRef, externalSecretSpec, ...) looked like a finding. The real scan
        only flags a hint-shaped key whose VALUE IS A NON-EMPTY STRING, i.e. an actual key/value
        pair such as a label or an annotation -- never a schema node, whose value is a dict.
        That is why external-secrets produced exactly 25 KEY lines (one label per CRD) and not
        hundreds. Copying the regexes without their scope is not copying the check."""
        if isinstance(node, dict):
            for k, v in node.items():
                p = "%s.%s" % (path, k)
                if KEY_HINT.search(str(k)) and isinstance(v, str) and v:
                    hits.append((name, p, "KEY_HINT", "%s=%s" % (k, v)))
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(node, str):
            for pname, rx in PATTERNS.items():
                for m in rx.finditer(node):
                    hits.append((name, path, pname, m.group(0)))

    walk(crd, "")
    return hits


def pull(chart, version, dest):
    r = subprocess.run(["helm", "pull", chart, "--version", version, "-d", dest, "--untar"],
                       capture_output=True, text=True, timeout=600)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def report(rows, header):
    print("  %s" % header)
    if not rows:
        print("    nothing the scan would flag")
        return
    seen = set()
    for obj, path, pat, s in sorted(rows):
        key = (obj, path, pat, s)
        if key in seen:
            continue
        seen.add(key)
        sha = hashlib.sha256(s.encode("utf-8")).hexdigest()
        shown = s if len(s) <= 60 else s[:57] + "..."
        print("    object : %s" % obj)
        print("    path   : %s" % path)
        print("    pattern: %s" % pat)
        print("    string : %r" % shown)
        print("    sha256 : %s" % sha)
        print()
    print("    %d finding(s)" % len(seen))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chart"); ap.add_argument("--old"); ap.add_argument("--new")
    ap.add_argument("--live", action="store_true"); ap.add_argument("--context"); ap.add_argument("--group")
    ap.add_argument("--verify-patterns", nargs="?", const=SANDBOX_SRC, default=None)
    a = ap.parse_args(argv[1:])

    if a.verify_patterns is not None:
        return verify_patterns(a.verify_patterns)

    if a.live:
        if not a.context:
            print("--live needs --context"); return 2
        cmd = ["kubectl", "--context", a.context, "--request-timeout=60s", "get", "crd", "-o", "json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode:
            print("  kubectl failed: %s" % (r.stderr or "")[:200]); return 2
        items = json.loads(r.stdout).get("items", [])
        if a.group:
            items = [c for c in items if (c.get("spec") or {}).get("group") == a.group]
        rows = []
        for c in items:
            rows += findings(c)
        report(rows, "LIVE CRDs%s: %d object(s) scanned" % (" in %s" % a.group if a.group else "", len(items)))
        return 0

    if not (a.chart and a.new):
        print("need --chart and --new (and --old for a delta)"); return 2

    with tempfile.TemporaryDirectory() as td:
        ok, err = pull(a.chart, a.new, os.path.join(td, "new"))
        if not ok:
            print("  pull %s %s failed: %s" % (a.chart, a.new, err[:160])); return 2
        new_rows = []
        for c in crds_from_dir(os.path.join(td, "new")):
            new_rows += findings(c)
        if not a.old:
            report(new_rows, "%s %s: all findings" % (a.chart, a.new))
            return 0
        ok, err = pull(a.chart, a.old, os.path.join(td, "old"))
        if not ok:
            print("  pull %s %s failed: %s" % (a.chart, a.old, err[:160])); return 2
        old_rows = set()
        for c in crds_from_dir(os.path.join(td, "old")):
            old_rows |= set(findings(c))
        added = [r for r in new_rows if r not in old_rows]
        removed = [r for r in old_rows if r not in set(new_rows)]
        report(added, "%s %s -> %s: findings ADDED (these need allowlisting)" % (a.chart, a.old, a.new))
        if removed:
            print()
            report(removed, "findings REMOVED (allowlist entries that will stop matching)")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
