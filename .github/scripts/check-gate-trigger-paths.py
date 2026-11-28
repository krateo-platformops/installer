#!/usr/bin/env python3
"""Refuse a gate whose workflow can be silently skipped by a PR that changes only a file the gate reads.

WHY THIS EXISTS. check-same-version-line.py reads chart/Chart.yaml (core-provider and
core-provider-crds are SUBCHARTS that exist only there), but its workflow's `paths:` filter omitted
that file. PR #1179 changed chart/Chart.yaml alone (core-provider 2.13.129 -> 2.13.130) and
`same-version-line` was ABSENT from its ~50 completed checks: the one gate that refuses a skewed
core-provider pair never ran on the one file that can skew it. A path-filtered workflow that does
not run produces no check at all -- not a red tick, not a skipped tick -- so the omission reads as
"all checks passed".

WHAT THIS ASSERTS, for every script in .github/scripts (a "gate"):
  every repo file the gate reads is covered by the `paths:` filter of the workflow that runs it, on
  every event (pull_request, pull_request_target, push) that has a filter -- or that workflow has no
  paths filter at all (it then runs on every change and cannot be skipped). It also asserts the gate
  script and its workflow file are themselves covered, so editing a gate re-runs it.

HOW "READS" IS FOUND (static, via ast; docstrings and comments are ignored):
  * a string constant that is exactly an existing repo file, e.g. "chart/Chart.yaml". This includes
    the DEFAULT of os.environ.get("CHART", "chart/Chart.yaml"): an env override can redirect the read
    in CI but the default is what runs, so the default counts;
  * the constant tail of os.path.join(<anything>, "templates", "_helpers.tpl"), resolved as given or
    under chart/ (>= 2 components, so a probe's own "Chart.yaml" write is not mistaken for a read);
  * glob.glob("literal/pattern") -- every matched file must be covered.
  A gate that points at the WHOLE chart directory (os.path.join(..., "chart") or CHART = "chart")
  reads a tree the filter cannot enumerate. That is not decidable from a path list, so each such
  gate must carry a reviewed entry in TREE_READS below, with the reason. An unreviewed tree read, or
  a reviewed entry that no longer applies, is a refusal.

FAILS CLOSED. An unparseable workflow, an unparseable/unreadable script, a gate that no workflow
runs, a workflow that has no push/pull_request trigger, `paths` together with `paths-ignore`, or a
denominator of zero is a REFUSAL, never a skip. The output states "N gates checked, M with paths
filters".

KNOWN LIMITS. Files named only through a computed string, read by a script it imports, or fetched by
a shell step in the workflow itself are not seen. Files WRITTEN under a probe are not distinguished
from reads except by the >= 2 component rule above.

    python3 .github/scripts/check-gate-trigger-paths.py             -> check this repo
    python3 .github/scripts/check-gate-trigger-paths.py --self-test -> fixtures; the detector must detect
"""

import ast
import os
import posixpath
import re
import sys

import yaml

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = ".github/scripts"
WORKFLOWS_DIR = ".github/workflows"
EVENTS = ("pull_request", "pull_request_target", "push")

# Gates that point at the whole chart directory. Each has been read and judged; the filter lists the
# specific files that matter, and widening it to chart/** would re-run these on every chart edit.
TREE_READS = {
    "check-componentvalues-unset.py":
        "renders the whole chart, but asserts only componentValuesUnset handling; triggers on the "
        "files that implement it (self-bootstrap.yaml, _helpers.tpl, values.schema.json). AMBIGUOUS, "
        "left as is.",
    "check-tls-feature-wiring.py":
        "copies and renders the whole chart; triggers on the files carrying TLS wiring. Chart.yaml "
        "is read only to substitute CHART_VERSION. AMBIGUOUS, left as is.",
    "check-jsonschema-cm-grant.py":
        "CHART is only the source dir for a probe; the sole file taken from it is "
        "templates/_helpers.tpl, which is in the filter.",
    "check-kog-urls.py":
        "CHART is only the source dir for a probe; the sole file taken from it is "
        "templates/_helpers.tpl, which is in the filter.",
    "check-subagents.py":
        "CHART is only the source dir for a probe; the sole file taken from it is "
        "templates/_helpers.tpl, which is in the filter.",
}

# Scripts that are run BY HAND and deliberately have no workflow. Same review rule as above: a reason,
# and a stale entry is a refusal.
HAND_RUN = {
    "check-tag-scope.py":
        "a pre-tag tool run by hand against the live cluster; its own docstring says it is "
        "deliberately not a CI gate (it compares against origin/main and would fail every PR).",
}

# This script's own fixtures name real repo paths as DATA; they are not reads. It is exempt from read
# extraction but still held to the trigger assertion for itself and its workflow.
SELF_NAME = "check-gate-trigger-paths.py"


# ---------------------------------------------------------------- glob

def _glob_to_re(pat):
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            if pat.startswith("/", i):
                i += 1
                out[-1] = "(?:.*/)?"
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def covered(path, patterns, ignore=()):
    """GitHub `paths` semantics: patterns in order, a leading `!` un-matches."""
    if ignore:
        return not any(_glob_to_re(p).match(path) for p in ignore)
    hit = False
    for p in patterns:
        if p.startswith("!"):
            if _glob_to_re(p[1:]).match(path):
                hit = False
        elif _glob_to_re(p).match(path):
            hit = True
    return hit


# ---------------------------------------------------------------- workflows

def parse_workflow(text):
    """({event: {"paths": [...]|None, "ignore": [...]|None}}, [run strings]). Raises ValueError."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError("unparseable YAML: %s" % str(exc).splitlines()[0])
    if not isinstance(doc, dict):
        raise ValueError("not a mapping")
    # YAML 1.1 parses the bare key `on` as boolean True.
    on = doc[True] if True in doc else doc.get("on")
    if on is None:
        raise ValueError("no `on:` key")
    events = {}
    if isinstance(on, str):
        events[on] = {"paths": None, "ignore": None}
    elif isinstance(on, list):
        for e in on:
            events[e] = {"paths": None, "ignore": None}
    elif isinstance(on, dict):
        for e, cfg in on.items():
            cfg = cfg if isinstance(cfg, dict) else {}
            if "paths" in cfg and "paths-ignore" in cfg:
                raise ValueError("event %s has both paths and paths-ignore" % e)
            for k in ("paths", "paths-ignore"):
                if k in cfg and not (isinstance(cfg[k], list) and all(isinstance(x, str) for x in cfg[k])):
                    raise ValueError("event %s: %s is not a list of strings" % (e, k))
            events[e] = {"paths": cfg.get("paths"), "ignore": cfg.get("paths-ignore")}
    else:
        raise ValueError("`on:` has an unexpected shape")
    runs = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                runs.append(step["run"])
    return events, runs


# ---------------------------------------------------------------- reads

def _is_join(n):
    f = n.func
    return isinstance(f, ast.Attribute) and f.attr == "join" and (
        (isinstance(f.value, ast.Attribute) and f.value.attr == "path")
        or (isinstance(f.value, ast.Name) and f.value.id in ("path", "posixpath")))


def _is_glob(n):
    f = n.func
    return isinstance(f, ast.Attribute) and f.attr in ("glob", "iglob") and \
        isinstance(f.value, ast.Name) and f.value.id == "glob"


def extract_reads(source, files, dirs):
    """(file_reads: set, tree_read: bool, glob_patterns: set). Raises SyntaxError."""
    tree = ast.parse(source)
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if k is not None:
                    skip.add(id(k))
    reads, tree_read, globs = set(), False, set()

    def strc(a):
        return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None

    consumed = set()
    assigned = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant):
            assigned.add(id(n.value))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if _is_glob(n) and n.args and strc(n.args[0]):
            globs.add(strc(n.args[0]))
            consumed.add(id(n.args[0]))
        if _is_join(n):
            tail = []
            for a in reversed(n.args):
                if strc(a) is None:
                    break
                tail.append(strc(a))
                consumed.add(id(a))
            tail.reverse()
            while tail and tail[0] == "..":
                tail.pop(0)
            if not tail:
                continue
            t = posixpath.join(*tail)
            if t in files:
                reads.add(t)
            elif t in dirs and strc(n.args[-1]) is not None and t == "chart":
                tree_read = True
            elif len(tail) >= 2 and "chart/" + t in files:
                reads.add("chart/" + t)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip \
                and id(n) not in consumed:
            v = n.value
            if v in files:
                reads.add(v)
            elif v == "chart" and v in dirs and id(n) in assigned:
                tree_read = True
    return reads, tree_read, globs


# ---------------------------------------------------------------- analysis

def analyse(scripts, workflows, files, tree_reads, hand_run=None, self_name=None):
    """scripts {name: source|None}, workflows {name: text|None}. Returns a dict, never raises."""
    hand_run = hand_run or {}
    dirs = {posixpath.dirname(f) for f in files}
    refusals, gaps, rows = [], [], []
    parsed = {}
    for wname, text in sorted(workflows.items()):
        if text is None:
            refusals.append("%s: unreadable workflow" % wname)
            continue
        try:
            parsed[wname] = parse_workflow(text)
        except ValueError as exc:
            refusals.append("%s: %s" % (wname, exc))
    runs_by_script = {}
    for wname, (events, runs) in parsed.items():
        blob = "\n".join(runs)
        for s in scripts:
            if re.search(r"(?<![\w./-])(?:\./)?%s/%s(?![\w.-])" % (re.escape(SCRIPTS_DIR), re.escape(s)), blob):
                runs_by_script.setdefault(s, []).append(wname)
    reviewed_used, hand_used = set(), set()
    n_filtered = 0
    for s in sorted(scripts):
        src = scripts[s]
        if src is None:
            refusals.append("%s: unreadable script" % s)
            continue
        try:
            reads, tree_read, globs = extract_reads(src, files, dirs)
            if s == self_name:
                reads, tree_read, globs = set(), False, set()
        except SyntaxError as exc:
            refusals.append("%s: unparseable script (%s)" % (s, exc.msg))
            continue
        for g in sorted(globs):
            rx = _glob_to_re(g)
            reads |= {f for f in files if rx.match(f)}
        wfs = runs_by_script.get(s, [])
        if not wfs and s in hand_run:
            hand_used.add(s)
            continue
        if not wfs:
            refusals.append("%s: no workflow runs this gate (a gate nothing runs is not a gate)" % s)
            continue
        row = {"gate": s, "workflows": wfs, "reads": sorted(reads), "filtered": False, "tree": tree_read,
               "notes": []}
        for w in wfs:
            events, _ = parsed[w]
            trig = [e for e in EVENTS if e in events]
            if not trig:
                refusals.append("%s: workflow %s has no push/pull_request trigger" % (s, w))
                continue
            for e in trig:
                pat, ign = events[e]["paths"], events[e]["ignore"]
                if pat is None and ign is None:
                    continue
                row["filtered"] = True
                need = set(reads) | {"%s/%s" % (SCRIPTS_DIR, s)}
                if e != "push":
                    need.add("%s/%s" % (WORKFLOWS_DIR, w))
                for r in sorted(need):
                    if not covered(r, pat or [], ign or []):
                        gaps.append((s, w, e, r))
        if row["filtered"]:
            n_filtered += 1
            if tree_read:
                if s in tree_reads:
                    reviewed_used.add(s)
                    row["notes"].append("tree read (reviewed): " + tree_reads[s])
                else:
                    refusals.append("%s: reads the whole chart tree under a paths filter and has no "
                                    "reviewed entry in TREE_READS" % s)
        rows.append(row)
    for s in sorted(set(tree_reads) - reviewed_used):
        refusals.append("%s: stale TREE_READS entry (no longer a filtered tree-reading gate)" % s)
    for s in sorted(set(hand_run) - hand_used):
        refusals.append("%s: stale HAND_RUN entry (a workflow runs it now, or the script is gone)" % s)
    if not scripts:
        refusals.append("denominator is zero: no gate scripts found under %s" % SCRIPTS_DIR)
    return {"rows": rows, "gaps": sorted(set(gaps)), "refusals": sorted(refusals),
            "checked": len(scripts), "filtered": n_filtered}


def render(res):
    out = ["%d gates checked, %d with paths filters" % (res["checked"], res["filtered"])]
    for r in res["rows"]:
        if r["filtered"]:
            out.append("  filtered   %-42s %s" % (r["gate"], ", ".join(r["reads"]) or "(no file reads found)"))
            for n in r["notes"]:
                out.append("             note: " + n)
    for s, w, e, p in res["gaps"]:
        out.append("GAP: %s (%s, on %s) reads or needs %s but the paths filter does not cover it; a PR "
                   "changing only that file skips the gate silently." % (s, w, e, p))
    for r in res["refusals"]:
        out.append("REFUSED: " + r)
    return "\n".join(out)


def load_repo(root):
    files = set()
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x != ".git"]
        for f in fs:
            files.add(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    scripts, workflows = {}, {}
    sd, wd = os.path.join(root, SCRIPTS_DIR), os.path.join(root, WORKFLOWS_DIR)
    for base, target, ext in ((sd, scripts, (".py",)), (wd, workflows, (".yaml", ".yml"))):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            if n.endswith(ext):
                try:
                    with open(os.path.join(base, n), encoding="utf-8") as fh:
                        target[n] = fh.read()
                except (OSError, UnicodeDecodeError):
                    target[n] = None
    return scripts, workflows, files


# ---------------------------------------------------------------- self-test

def _wf(on_block, script):
    return "name: t\non:\n%s\njobs:\n  j:\n    runs-on: x\n    steps:\n      - run: python3 .github/scripts/%s\n" % (
        on_block, script)


PR_PATHS = "  pull_request:\n    paths:\n%s\n"


def _paths(*ps):
    return PR_PATHS % "\n".join("      - '%s'" % p for p in ps)


FILES = {"chart/Chart.yaml", "chart/files/component-pins.yaml", "chart/templates/_helpers.tpl",
         ".github/scripts/g.py", ".github/workflows/g.yaml", "chart/values.yaml"}
SELF = [".github/scripts/g.py", ".github/workflows/g.yaml"]


def self_test():
    ok = True

    def case(name, scripts, workflows, want_gaps, want_ref=(), tree=None, files=FILES):
        nonlocal ok
        res = analyse(scripts, workflows, files, tree or {})
        got_g = {(s, e, p) for s, _, e, p in res["gaps"]}
        want_g = set(want_gaps)
        got_r = res["refusals"]
        good = got_g == want_g and len(got_r) == len(want_ref) and all(
            any(w in g for g in got_r) for w in want_ref)
        print("%s  %s" % ("ok  " if good else "FAIL", name))
        if not good:
            ok = False
            print("      gaps want %s got %s" % (sorted(want_g), sorted(got_g)))
            print("      refusals want %s got %s" % (list(want_ref), got_r))

    reads_chart = 'import os\nCHART = os.environ.get("CHART", "chart/Chart.yaml")\n'
    reads_pins = 'PINS = "chart/files/component-pins.yaml"\n'
    covered_wf = _paths("chart/Chart.yaml", *SELF)
    case("covered read passes", {"g.py": reads_chart}, {"g.yaml": _wf(covered_wf, "g.py")}, [])
    case("missing read is a GAP (the #1179 shape: Chart.yaml absent from paths)",
         {"g.py": reads_chart}, {"g.yaml": _wf(_paths(*SELF), "g.py")},
         [("g.py", "pull_request", "chart/Chart.yaml")])
    case("no paths filter at all passes", {"g.py": reads_chart},
         {"g.yaml": _wf("  pull_request:\n  push:\n    branches: [main]", "g.py")}, [])
    case("env override: the DEFAULT counts as a read",
         {"g.py": 'import os\nX = os.environ.get("X", "chart/files/component-pins.yaml")\n'},
         {"g.yaml": _wf(_paths(*SELF), "g.py")}, [("g.py", "pull_request", "chart/files/component-pins.yaml")])
    case("WHOLE gap set: two reads missing, one covered, plus the gate script itself uncovered",
         {"g.py": reads_chart + reads_pins + 'H = os.path.join(os.path.dirname(__file__), "templates", "_helpers.tpl")\n'},
         {"g.yaml": _wf(_paths("chart/templates/_helpers.tpl", ".github/workflows/g.yaml"), "g.py")},
         [("g.py", "pull_request", "chart/Chart.yaml"),
          ("g.py", "pull_request", "chart/files/component-pins.yaml"),
          ("g.py", "pull_request", ".github/scripts/g.py")])
    case("a gap on EACH event with a filter is reported on each",
         {"g.py": reads_chart},
         {"g.yaml": _wf(_paths(*SELF) + "  push:\n    paths:\n      - '.github/scripts/g.py'\n", "g.py")},
         [("g.py", "pull_request", "chart/Chart.yaml"), ("g.py", "push", "chart/Chart.yaml")])
    case("glob support: ** covers a subtree", {"g.py": reads_chart},
         {"g.yaml": _wf(_paths("chart/**", *SELF), "g.py")}, [])
    case("glob read expands to its matches",
         {"g.py": 'import glob\nglob.glob("chart/files/*.yaml")\n'},
         {"g.yaml": _wf(_paths(*SELF), "g.py")}, [("g.py", "pull_request", "chart/files/component-pins.yaml")])
    case("a docstring or dict key mentioning a path is NOT a read",
         {"g.py": '"""reads chart/Chart.yaml"""\nD = {"chart/Chart.yaml": 1}\n'},
         {"g.yaml": _wf(_paths(*SELF), "g.py")}, [])
    case("paths-ignore that excludes a read is a GAP",
         {"g.py": reads_chart},
         {"g.yaml": _wf("  pull_request:\n    paths-ignore:\n      - 'chart/**'", "g.py")},
         [("g.py", "pull_request", "chart/Chart.yaml")])
    case("unreviewed whole-chart read under a filter is REFUSED",
         {"g.py": 'import os\nC = os.path.join("..", "..", "chart")\n'},
         {"g.yaml": _wf(_paths(*SELF), "g.py")}, [], ["no reviewed entry"])
    case("reviewed whole-chart read passes",
         {"g.py": 'import os\nC = os.path.join("..", "..", "chart")\n'},
         {"g.yaml": _wf(_paths(*SELF), "g.py")}, [], [], tree={"g.py": "x"})
    case("stale TREE_READS entry is REFUSED", {"g.py": reads_chart},
         {"g.yaml": _wf(covered_wf, "g.py")}, [], ["stale TREE_READS"], tree={"g.py": "x"})
    res = analyse({"g.py": reads_chart}, {"g.yaml": _wf(covered_wf, "g.py")}, FILES, {}, {"g.py": "x"})
    good = len(res["refusals"]) == 1 and "stale HAND_RUN" in res["refusals"][0]
    print("%s  stale HAND_RUN entry is REFUSED" % ("ok  " if good else "FAIL"))
    ok = ok and good
    res = analyse({"g.py": reads_chart}, {"g.yaml": _wf("  workflow_dispatch:", "h.py")}, FILES, {}, {"g.py": "x"})
    good = res["refusals"] == [] and res["gaps"] == []
    print("%s  a reviewed hand-run script with no workflow passes" % ("ok  " if good else "FAIL"))
    ok = ok and good
    case("FAIL CLOSED: unparseable workflow", {"g.py": reads_chart},
         {"g.yaml": "on: [\n  : :", "h.yaml": _wf(_paths("chart/Chart.yaml", ".github/scripts/g.py", ".github/workflows/h.yaml"), "g.py")},
         [], ["g.yaml: unparseable YAML"])
    case("FAIL CLOSED: unparseable script", {"g.py": "def (:\n"},
         {"g.yaml": _wf(covered_wf, "g.py")}, [], ["unparseable script"])
    case("FAIL CLOSED: unreadable script", {"g.py": None},
         {"g.yaml": _wf(covered_wf, "g.py")}, [], ["unreadable script"])
    case("FAIL CLOSED: gate no workflow runs", {"g.py": reads_chart, "orphan.py": reads_chart},
         {"g.yaml": _wf(covered_wf, "g.py")}, [], ["orphan.py: no workflow runs this gate"])
    case("FAIL CLOSED: workflow with no push/pull_request trigger", {"g.py": reads_chart},
         {"g.yaml": _wf("  workflow_dispatch:", "g.py")}, [], ["no push/pull_request trigger"])
    case("FAIL CLOSED: zero gates", {}, {"g.yaml": _wf(covered_wf, "g.py")}, [], ["denominator is zero"])
    case("a script named only in a COMMENT is not run by that workflow", {"g.py": reads_chart},
         {"g.yaml": "name: t\non:\n  pull_request:\n# python3 .github/scripts/g.py\njobs:\n  j:\n    runs-on: x\n    steps:\n      - run: echo hi\n"},
         [], ["no workflow runs this gate"])
    # YAML 1.1 parses the bare key `on` as True; a quoted "on" must work too.
    ev, _ = parse_workflow('name: t\n"on":\n  pull_request:\n    paths: ["a"]\njobs: {}\n')
    good = ev == {"pull_request": {"paths": ["a"], "ignore": None}}
    print("%s  quoted \"on\" key parsed" % ("ok  " if good else "FAIL"))
    ok = ok and good
    ev, _ = parse_workflow('name: t\non:\n  pull_request:\n    paths: ["a"]\njobs: {}\n')
    good = ev == {"pull_request": {"paths": ["a"], "ignore": None}}
    print("%s  bare on key (YAML 1.1 boolean True) parsed" % ("ok  " if good else "FAIL"))
    ok = ok and good
    return ok


def main(argv):
    if "--self-test" in argv:
        ok = self_test()
        print("self-test: %s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    scripts, workflows, files = load_repo(HERE)
    res = analyse(scripts, workflows, files, TREE_READS, HAND_RUN, SELF_NAME)
    print(render(res))
    if res["gaps"] or res["refusals"]:
        print("FAIL: %d gap(s), %d refusal(s)" % (len(res["gaps"]), len(res["refusals"])))
        return 1
    print("OK: every file a gate reads is covered by its workflow's paths filter, or the workflow has none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
