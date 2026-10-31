#!/usr/bin/env python3
"""Offline fixtures for check-pin-ci's Makefile resolution. No network, no gh.

WHY THIS SUITE EXISTS. check-pin-ci reported incident-controller 0.5.1 as "CI is green but
NOTHING TESTED THE CODE". That was a FALSE NEGATIVE: .github/workflows/check.yaml runs
`make check`, whose chain is `check: build lint test check-generate lint-charts` and whose
`test:` recipe is `go test -race -cover ./...`. The job log at 8bfc229a shows the race-enabled
suite running with coverage on three packages. alert-provider 0.2.54 is the same shape.

Two blindnesses compounded. The guard's: it matched test COMMANDS in a step's `run:` and never
resolved `make <target>`, so a repo whose CI is one `make check` looked untested. And mine: I
confirmed the guard by grepping the workflows for `go test`, `gotestsum`, `ginkgo` and
`make test` -- four spellings, none of them the fifth one the repo uses -- then reported the
zero as a fact about the repo rather than about my pattern. The pin was blocked and the user
routed work to add test workflows that already existed.

So the resolution is split into a pure function and fixtured here, in BOTH directions: the
chain must be followed, AND a target that reaches no test command must still fail. Widening what
the guard can SEE must not widen what it will ACCEPT.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pc", ROOT / ".github" / "scripts" / "check-pin-ci.py")
pc = importlib.util.module_from_spec(spec)
sys.argv = ["fixtures"]
spec.loader.exec_module(pc)

# The real shape, reduced: krateo-platformops/incident-controller's Makefile at 8bfc229a.
REAL = """\
.PHONY: all
all: build

check-generate: generate ## fail if the committed generated files differ
\tgit diff --exit-code

build: ## build all packages
\tgo build ./...

test: ## run unit tests
\tgo test -race -cover ./...

lint: ## run go vet
\tgo vet ./...

lint-charts: ## helm lint every chart
\thelm lint charts/*

check: build lint test check-generate lint-charts ## everything CI runs
"""

# A repo whose `make ci` genuinely runs no tests. Must still be reported as untested.
NO_TESTS = """\
build:
\tgo build ./...

fmt:
\tgofmt -l .

ci: build fmt
"""

# A cycle, which a naive transitive walk would hang on.
CYCLIC = """\
a: b
\techo a
b: a
\techo b
"""

CASES = [
    ("the real chain: make check reaches go test",        REAL,     ["check"],          True),
    ("make test directly",                                REAL,     ["test"],           True),
    ("make lint reaches go vet, which is in scope",        REAL,     ["lint"],           True),
    ("make build reaches NO test command",                 REAL,     ["build"],          False),
    ("make check-generate reaches no test command",        REAL,     ["check-generate"], False),
    ("a target that does not exist",                       REAL,     ["nope"],           False),
    ("make ci where ci genuinely runs no tests",           NO_TESTS, ["ci"],             False),
    ("make build in a repo with no tests",                 NO_TESTS, ["build"],          False),
    ("a cyclic Makefile terminates and finds nothing",     CYCLIC,   ["a"],              False),
    ("several targets, one of which tests",                REAL,     ["build", "test"],  True),
    ("an empty Makefile",                                  "",       ["check"],          False),
]


def main():
    failures = []
    for label, text, targets, want in CASES:
        got = pc._resolve_make_targets(text, targets)
        ok = bool(got) == want
        if not ok:
            failures.append(label)
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<48} -> {got!r}")

    # The asymmetry this guard is built on, asserted rather than assumed: the widening must not
    # make an untested green job pass.
    assert pc._resolve_make_targets(NO_TESTS, ["ci"]) is None, \
        "a make target that runs no tests must NOT be reported as tested"

    print()
    if failures:
        print(f"::error::{len(failures)} fixture(s) failed: {', '.join(failures)}")
        return 1
    print(f"check-pin-ci-fixtures: OK — {len(CASES)} cases, both directions, no network")
    return 0


if __name__ == "__main__":
    sys.exit(main())
