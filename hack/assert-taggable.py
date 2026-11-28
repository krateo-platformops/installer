#!/usr/bin/env python3
"""Pre-tag gate: exit 0 only if it is safe to cut a tag from the current HEAD.

Usage:
  python3 hack/assert-taggable.py [--expect name=version ...] [--tag X] \
      [--remote origin] [--branch main] [--repo PATH]
  python3 hack/assert-taggable.py --self-test

Why it exists: tag 0.3.717 was cut at 617e1c5, ten commits behind origin/main. An
uncommitted local edit made `git pull --ff-only` refuse, and the pull had 2>/dev/null on
it, so the refusal was discarded and read as success. `git tag` does not care that HEAD is
behind. Ten of the eleven components the annotation claimed were at their OLD version.

Rules: never mutates the repo (no fetch, no write); never discards stderr; never reads an
exit status through a pipe; fails CLOSED (anything unreadable/unparseable/ambiguous is a
refusal). Stdlib + yaml only. Compatible with `python3 -I`.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

import yaml

PINS = "chart/files/component-pins.yaml"
PINS_MIN = "chart/files/component-pins.min.yaml"
BURNED = ".github/burned-tags.yaml"
TAG_RE = re.compile(r"^0\.3\.(\d+)$")


class Git:
    def __init__(self, repo):
        self.repo = repo

    def run(self, *args):
        """Return (returncode, stdout, stderr). stderr is always captured, never dropped."""
        try:
            p = subprocess.run(["git", "-C", self.repo] + list(args),
                               capture_output=True, text=True)
        except OSError as e:
            return 127, "", "cannot execute git: %s" % e
        return p.returncode, p.stdout, p.stderr.strip()


def gfail(args, rc, err):
    return "git %s failed (exit %d): %s" % (" ".join(args), rc, err or "<no stderr>")


def check_head(g, remote, branch, fails):
    ref = "%s/%s" % (remote, branch)
    rc1, head, e1 = g.run("rev-parse", "HEAD")
    rc2, rem, e2 = g.run("rev-parse", "--verify", "refs/remotes/%s" % ref)
    if rc1 != 0:
        fails.append("HEAD identity: " + gfail(["rev-parse", "HEAD"], rc1, e1))
    if rc2 != 0:
        fails.append("HEAD identity: " + gfail(["rev-parse", ref], rc2, e2))
    if rc1 != 0 or rc2 != 0:
        return None
    head, rem = head.strip(), rem.strip()
    if head == rem:
        return rem
    _, hs, _ = g.run("log", "-1", "--format=%h %s", head)
    _, rs, _ = g.run("log", "-1", "--format=%h %s", rem)
    rc, cnt, e3 = g.run("rev-list", "--count", "HEAD..%s" % ref)
    cnt = cnt.strip() if rc == 0 else "unknown (%s)" % gfail(["rev-list"], rc, e3)
    fails.append("HEAD identity: HEAD is not %s.\n    HEAD   : %s\n    %s: %s\n"
                 "    commits missing from HEAD: %s" % (ref, hs.strip(), ref, rs.strip(), cnt))
    return rem


def check_fresh(g, remote, branch, fails):
    ref = "%s/%s" % (remote, branch)
    rc, out, err = g.run("ls-remote", remote, "refs/heads/%s" % branch)
    if rc != 0:
        fails.append("Freshness: " + gfail(["ls-remote", remote, "refs/heads/" + branch], rc, err))
        return
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) != 1:
        fails.append("Freshness: ls-remote returned %d refs for refs/heads/%s (expected 1)"
                     % (len(lines), branch))
        return
    remote_sha = lines[0].split()[0]
    rc, local_sha, err = g.run("rev-parse", "--verify", "refs/remotes/%s" % ref)
    if rc != 0:
        fails.append("Freshness: " + gfail(["rev-parse", ref], rc, err))
        return
    if local_sha.strip() != remote_sha:
        fails.append("Freshness: local %s is %s but the remote's %s is %s. The local ref is "
                     "STALE: run `git fetch %s` and re-run."
                     % (ref, local_sha.strip()[:7], branch, remote_sha[:7], remote))


def check_clean(g, fails):
    rc, out, err = g.run("status", "--porcelain")
    if rc != 0:
        fails.append("Clean tree: " + gfail(["status", "--porcelain"], rc, err))
        return
    if out.strip():
        fails.append("Clean tree: working tree is not clean (a local edit can make a pull "
                     "refuse):\n" + "\n".join("    " + l for l in out.splitlines()))


def show_yaml(g, path, fails, label):
    """Read a file AT HEAD via git show and parse it. None on any failure (fail closed)."""
    rc, out, err = g.run("show", "HEAD:%s" % path)
    if rc != 0:
        fails.append("%s: cannot read %s at HEAD: %s" % (label, path, gfail(["show"], rc, err)))
        return None
    try:
        return yaml.safe_load(out)
    except yaml.YAMLError as e:
        fails.append("%s: %s at HEAD does not parse: %s" % (label, path, str(e).replace("\n", " ")))
        return None


def pin_map(doc, path, fails, label):
    """name -> version (str or None). Ambiguity (duplicate names, malformed items) refuses."""
    if not isinstance(doc, dict) or not isinstance(doc.get("components"), list):
        fails.append("%s: %s has no top-level `components:` list" % (label, path))
        return None
    m, ok = {}, True
    for i, item in enumerate(doc["components"]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            fails.append("%s: %s item #%d has no string `name`" % (label, path, i))
            ok = False
            continue
        n = item["name"]
        if n in m:
            fails.append("%s: %s lists component %r more than once (ambiguous)" % (label, path, n))
            ok = False
            continue
        v = item.get("version")
        m[n] = None if v is None else str(v)
    return m if ok else None


def check_pins(g, expects, fails):
    full_doc = show_yaml(g, PINS, fails, "Pins")
    full = pin_map(full_doc, PINS, fails, "Pins") if full_doc is not None else None
    if full is not None:
        for name, want in expects:
            if name not in full:
                fails.append("Pins: --expect %s=%s but %s has no component %r" % (name, want, PINS, name))
            elif full[name] != want:
                fails.append("Pins: %s is pinned at %s at HEAD, expected %s" % (name, full[name], want))
    elif expects:
        fails.append("Pins: cannot evaluate --expect without a readable pins file")
    min_doc = show_yaml(g, PINS_MIN, fails, "Min sync")
    mn = pin_map(min_doc, PINS_MIN, fails, "Min sync") if min_doc is not None else None
    if full is not None and mn is not None:
        for name in sorted(set(full) | set(mn)):
            if name not in mn:
                fails.append("Min sync: %r is in the full file (%s) but missing from the min file" % (name, full[name]))
            elif name not in full:
                fails.append("Min sync: %r is in the min file (%s) but missing from the full file" % (name, mn[name]))
            elif full[name] != mn[name]:
                fails.append("Min sync: %r is %s in the full file but %s in the min file" % (name, full[name], mn[name]))


def load_burned(g, fails):
    rc, out, err = g.run("ls-tree", "HEAD", "--", BURNED)
    if rc != 0:
        fails.append("Burned tags: " + gfail(["ls-tree"], rc, err))
        return None
    if not out.strip():
        return {}  # genuinely absent at HEAD: allowed
    doc = show_yaml(g, BURNED, fails, "Burned tags")
    if doc is None:
        return None
    if not isinstance(doc, dict):
        fails.append("Burned tags: %s must be a mapping of tag -> {reason: ...}" % BURNED)
        return None
    res, ok = {}, True
    for tag, ent in doc.items():
        reason = ent.get("reason") if isinstance(ent, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            fails.append("Burned tags: entry %r in %s has no `reason` string" % (str(tag), BURNED))
            ok = False
        else:
            res[str(tag)] = reason
    return res if ok else None


def check_tag(g, remote, tag, fails):
    m = TAG_RE.match(tag)
    if not m:
        fails.append("Tag: %r is not of the form 0.3.N" % tag)
        return
    burned = load_burned(g, fails)
    if burned is not None and tag in burned:
        fails.append("Tag: %s is BURNED and may never be cut: %s" % (tag, burned[tag]))
    rc, out, err = g.run("tag", "--list")
    if rc != 0:
        fails.append("Tag: " + gfail(["tag", "--list"], rc, err))
        return
    names = set(out.split())
    rc, out, err = g.run("ls-remote", "--tags", remote)
    if rc != 0:
        fails.append("Tag: " + gfail(["ls-remote", "--tags", remote], rc, err))
        return
    for l in out.splitlines():
        parts = l.split()
        if len(parts) == 2 and parts[1].startswith("refs/tags/"):
            t = parts[1][len("refs/tags/"):]
            names.add(t[:-3] if t.endswith("^{}") else t)
    if tag in names:
        fails.append("Tag: %s already exists (locally or on %s); a tag is never re-cut" % (tag, remote))
    nums = [int(TAG_RE.match(t).group(1)) for t in names if TAG_RE.match(t)]
    if nums and int(m.group(1)) <= max(nums):
        fails.append("Tag: %s is not greater than the highest existing 0.3.* tag, 0.3.%d "
                     "(a lower number reads as a downgrade to helm upgrade)" % (tag, max(nums)))


def gate(repo, remote, branch, expects, tag):
    """Return list of failure strings; empty means taggable."""
    g = Git(repo)
    fails = []
    check_head(g, remote, branch, fails)
    check_fresh(g, remote, branch, fails)
    check_clean(g, fails)
    check_pins(g, expects, fails)
    if tag:
        check_tag(g, remote, tag, fails)
    return fails


# ---------------------------------------------------------------- self-test

def _sh(cwd, *args):
    p = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
                                GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null"))
    if p.returncode != 0:
        raise RuntimeError("%s failed: %s" % (args, p.stderr))
    return p.stdout


PINS_OK = ("components:\n- name: alpha\n  kind: A\n  version: 1.0.0\n"
           "- name: beta\n  statusDataTemplate:\n  - x\n  - y\n  version: 2.0.0\n")


def _make(root, pins=PINS_OK, mn=PINS_OK, burned="'0.3.9':\n  reason: test burn\n"):
    bare = os.path.join(root, "remote.git")
    work = os.path.join(root, "work")
    _sh(root, "git", "init", "-q", "--bare", "-b", "main", bare)
    _sh(root, "git", "clone", "-q", bare, work)
    _sh(work, "git", "checkout", "-q", "-b", "main")
    os.makedirs(os.path.join(work, "chart/files"))
    os.makedirs(os.path.join(work, ".github"))
    for p, c in ((PINS, pins), (PINS_MIN, mn), (BURNED, burned)):
        with open(os.path.join(work, p), "w") as f:
            f.write(c)
    _sh(work, "git", "add", "-A")
    _sh(work, "git", "commit", "-q", "-m", "init")
    _sh(work, "git", "tag", "0.3.5")
    _sh(work, "git", "tag", "0.3.10")
    _sh(work, "git", "push", "-q", "origin", "main", "--tags")
    _sh(work, "git", "fetch", "-q", "origin")
    return work


def self_test():
    script = os.path.abspath(__file__)
    cases = []

    def case(name, setup, extra, want_rc, want_msg):
        cases.append((name, setup, extra, want_rc, want_msg))

    def behind(w):
        with open(os.path.join(w, "f"), "w") as f:
            f.write("x")
        _sh(w, "git", "add", "f")
        _sh(w, "git", "commit", "-q", "-m", "second")
        _sh(w, "git", "push", "-q", "origin", "main")
        _sh(w, "git", "fetch", "-q", "origin")
        _sh(w, "git", "reset", "-q", "--hard", "HEAD~1")

    def stale(w):  # remote moves, local ref not fetched
        _sh(w, "git", "clone", "-q", os.path.join(os.path.dirname(w), "remote.git"),
            os.path.join(os.path.dirname(w), "other"))
        o = os.path.join(os.path.dirname(w), "other")
        with open(os.path.join(o, "g"), "w") as f:
            f.write("x")
        _sh(o, "git", "add", "g")
        _sh(o, "git", "commit", "-q", "-m", "other")
        _sh(o, "git", "push", "-q", "origin", "main")

    def dirty(w):
        with open(os.path.join(w, PINS), "a") as f:
            f.write("# local edit\n")

    case("everything in order", None, ["--expect", "alpha=1.0.0", "--expect", "beta=2.0.0", "--tag", "0.3.11"], 0, "all checks passed")
    case("HEAD one commit behind remote", behind, [], 1, "commits missing from HEAD: 1")
    case("local remote ref stale vs ls-remote", stale, [], 1, "run `git fetch origin`")
    case("dirty working tree", dirty, [], 1, "working tree is not clean")
    case("--expect mismatch", None, ["--expect", "alpha=9.9.9"], 1, "alpha is pinned at 1.0.0")
    case("--expect unknown component", None, ["--expect", "nope=1"], 1, "no component 'nope'")
    case("min file out of sync", "min", [], 1, "'beta' is 2.0.0 in the full file but 3.0.0 in the min file")
    case("--tag already exists", None, ["--tag", "0.3.10"], 1, "already exists")
    case("--tag lower than highest", None, ["--tag", "0.3.7"], 1, "highest existing 0.3.* tag, 0.3.10")
    case("--tag on burned list", None, ["--tag", "0.3.9"], 1, "BURNED")
    case("burned entry with no reason", "noreason", ["--tag", "0.3.11"], 1, "has no `reason`")
    case("unparseable pins file", "badyaml", [], 1, "does not parse")

    n_fail = 0
    for name, setup, extra, want_rc, want_msg in cases:
        with tempfile.TemporaryDirectory() as root:
            kw = {}
            if setup == "min":
                kw["mn"] = PINS_OK.replace("2.0.0", "3.0.0")
            elif setup == "noreason":
                kw["burned"] = "'0.3.9': {}\n"
            elif setup == "badyaml":
                kw["pins"] = "components: [\n"
                kw["mn"] = "components: [\n"
            w = _make(root, **kw)
            if callable(setup):
                setup(w)
            p = subprocess.run([sys.executable, "-I", script, "--repo", w] + extra,
                               capture_output=True, text=True)
            out = p.stdout + p.stderr
            ok = p.returncode == want_rc and want_msg in out
            if not ok:
                n_fail += 1
            print("%s  %s (exit %d, want %d, message %s)" % (
                "PASS" if ok else "FAIL", name, p.returncode, want_rc,
                "found" if want_msg in out else "MISSING: " + want_msg))
            if not ok:
                print(out)
    print("self-test: %d cases, %d failed" % (len(cases), n_fail))
    return 1 if n_fail else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--expect", action="append", default=[], metavar="NAME=VERSION")
    ap.add_argument("--tag")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    expects = []
    for e in a.expect:
        if "=" not in e or not e.split("=", 1)[0] or not e.split("=", 1)[1]:
            print("REFUSED: bad --expect %r (want name=version)" % e)
            return 2
        expects.append(tuple(e.split("=", 1)))
    fails = gate(a.repo, a.remote, a.branch, expects, a.tag)
    if fails:
        print("NOT TAGGABLE: %d check(s) failed" % len(fails))
        for f in fails:
            print(" - " + f)
        return 1
    print("TAGGABLE: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
