#!/usr/bin/env python3
"""Exercises the componentValues node for krateo-observability's ClickHouse users and profiles.

WHY THIS GATE EXISTS. `check-componentvalues-coverage.py` compares the KEY PATHS a pinned chart
offers against the paths this schema declares. It cannot see a TYPE mismatch, and that is exactly
what krateo-observability 0.1.59 introduced: `password_sha256_hex` became
`{'@from_env': '<ENV NAME>'}` for 12 of its 13 users, while this schema declared a 64-hex STRING.
The coverage gate stayed green on those 12 because the key path was unchanged. Found by validating
the chart's own values against this schema, which is what this gate now does on every PR.

WHY A TYPE MISMATCH IS WORSE THAN A MISSING KEY. The Installer CRD carries these nodes with
`additionalProperties` unset, so the apiserver PRUNES what it cannot place and reports SUCCESS. A
value typed differently from the declaration vanishes on the way into the CR, the chart's own
default wins, and nothing is red and nothing is logged. A refusal at `helm upgrade` is the loud
outcome and the one worth engineering for.

WHAT IT PROVES, each with its own case below:
  1. the chart's OWN values validate -- all 13 users and 8 profiles of 0.1.59, verbatim, and
     0.1.55's outgoing set too, so the declaration spans the pin's whole span;
  2. BOTH hash forms are accepted -- the literal (what 057 and demo carry today) and `@from_env`
     (what the chart adopted in 0.1.59). Declaring only one would refuse either the live clusters
     or the chart;
  3. a user declaring NEITHER authentication key is REFUSED. That refusal is the point: ClickHouse
     treats a user object with no authentication as passwordless and reachable, so a typo in the
     key name would silently create an open account;
  4. the closed shapes stay closed -- an invented profile key, `networks.cidr`, a `constraints`
     form the chart never ships, and an empty constraint object are each refused by name.

HOW TO BREAK IT ON PURPOSE, to prove it still detects: delete the `anyOf` from the users entry and
case 5 alone fails; change the `oneOf` back to a bare string type and cases 1, 2 and 6 fail.
"""
import json
import io
import os
import subprocess
import sys

try:
    import jsonschema
except ImportError:
    sys.stderr.write("jsonschema is required: pip install jsonschema\n")
    raise SystemExit(2)

import yaml

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA = os.path.join(HERE, "chart", "values.schema.json")
PATH = ["clickstack", "clickhouse", "cluster", "spec", "settings", "extraUsersConfig"]


def wrap(euc):
    """Put an extraUsersConfig under the componentValues path this schema declares."""
    node = euc
    for key in reversed(PATH):
        node = {key: node}
    return {"componentValues": {"krateo-observability": node}}


def dig(values):
    node = values
    for key in PATH:
        node = node[key]
    return node


HEX = "a" * 64

# (label, extraUsersConfig, must_validate)
CASES = [
    ("a literal 64-hex hash, as 057 and demo carry it",
     {"users": {"chops": {"password_sha256_hex": HEX, "profile": "x",
                          "grants": [{"query": "GRANT SELECT ON a.b"}]}}}, True),
    ("the @from_env form the chart adopted in 0.1.59",
     {"users": {"chops": {"password_sha256_hex": {"@from_env": "CLICKHOUSE_USER_CHOPS_SHA256"},
                          "profile": "x", "grants": [{"query": "GRANT SELECT ON a.b"}]}}}, True),
    ("exec_reader: no_password with networks, and no hash at all",
     {"users": {"exec_reader": {"no_password": True, "networks": {"ip": ["127.0.0.1", "::1"]},
                                "profile": "exec_reader",
                                "grants": [{"query": "GRANT SELECT ON default.*"}]}}}, True),
    ("a profile carrying every key the chart ships",
     {"profiles": {"p": {"readonly": 0, "max_execution_time": 30, "max_threads": 4,
                         "max_memory_usage": 4294967296, "max_concurrent_queries_for_user": 8,
                         "cancel_http_readonly_queries_on_client_close": 1,
                         "constraints": {"max_threads": {"max": 4},
                                         "max_concurrent_queries_for_user": {"const": True}}}}}, True),
    ("A USER WITH NEITHER password_sha256_hex NOR no_password",
     {"users": {"ghost": {"profile": "analyst"}}}, False),
    ("a 63-hex hash, one character short",
     {"users": {"g": {"password_sha256_hex": "a" * 63}}}, False),
    ("@from_env with a lowercase variable name",
     {"users": {"g": {"password_sha256_hex": {"@from_env": "bad_name"}}}}, False),
    ("@from_env carrying a second key",
     {"users": {"g": {"password_sha256_hex": {"@from_env": "X", "other": 1}}}}, False),
    ("a profile key the chart never ships",
     {"profiles": {"b": {"invented_key": 1}}}, False),
    ("networks.cidr instead of networks.ip",
     {"users": {"r": {"no_password": True, "networks": {"cidr": ["10.0.0.0/8"]}}}}, False),
    ("a constraints form the chart never ships (min)",
     {"profiles": {"p": {"constraints": {"max_threads": {"min": 2}}}}}, False),
    ("an EMPTY constraints entry",
     {"profiles": {"p": {"constraints": {"max_threads": {}}}}}, False),
]


DEFAULT_REPO = "oci://ghcr.io/krateo-platformops/charts"
COMPONENT = "krateo-observability"


def chart_extra_users():
    """Read the PINNED krateo-observability's own extraUsersConfig, from its own registry.

    The pin's `repo` key is the registry authority. Probing the default registry for a chart pinned
    elsewhere returns "not found" for every version including the one running, which is
    indistinguishable from an unpublished chart.
    """
    pins_path = os.path.join(HERE, "chart", "files", "component-pins.yaml")
    doc = yaml.safe_load(io.open(pins_path, encoding="utf-8").read()) or {}
    entries = doc.get("components") if isinstance(doc, dict) else doc
    pin = None
    for entry in (entries or []):
        if entry.get("name") == COMPONENT:
            pin = entry
            break
    if pin is None:
        return None, "%s is not in the pins file" % COMPONENT
    version = str(pin.get("version") or "")
    if not version:
        return None, "%s has no version in its pin" % COMPONENT
    repo = pin.get("repo") or DEFAULT_REPO
    chart = pin.get("chart") or COMPONENT
    run = subprocess.run(["helm", "show", "values", "%s/%s" % (repo, chart),
                          "--version", version],
                         capture_output=True, text=True, timeout=180)
    if run.returncode != 0:
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return None, "helm show values %s %s: %s" % (chart, version, tail[0][:120])
    try:
        values = yaml.safe_load(run.stdout) or {}
    except yaml.YAMLError as exc:
        return None, "unparseable values for %s %s: %s" % (chart, version, str(exc)[:100])
    node = values
    for key in PATH:
        node = (node or {}).get(key)
        if node is None:
            return None, None
    print("  read the PINNED chart: %s %s from %s" % (chart, version, repo))
    return node, None


def validates(schema, doc):
    try:
        jsonschema.validate(doc, schema)
        return True, ""
    except jsonschema.ValidationError as exc:
        return False, str(exc).split("\n")[0][:96]


def main():
    schema = json.load(io.open(SCHEMA, encoding="utf-8"))
    failures = []
    checked = 0

    # THE PINNED CHART'S OWN VALUES. This is the case that caught 0.1.59's type change, so it is
    # the one that must not be skippable: it pulls whatever version the pins file names, which means
    # the NEXT type change is caught by the pin PR that introduces it, with no edit here.
    #
    # A pull failure is reported, never passed over. An unreachable registry and a chart whose
    # values this schema cannot express are indistinguishable from a silent skip, and the silent
    # skip is the one that lets a regression through.
    euc, err = chart_extra_users()
    checked += 1
    if err:
        failures.append("could not read the pinned krateo-observability's values: %s" % err)
    elif euc is None:
        failures.append("the pinned krateo-observability's values have no %s path -- either the "
                        "chart moved it or this gate's PATH is stale; neither is a pass"
                        % "/".join(PATH))
    else:
        ok, msg = validates(schema, wrap(euc))
        users = sorted((euc.get("users") or {}).keys())
        profs = sorted((euc.get("profiles") or {}).keys())
        if ok:
            print("  ok   the PINNED chart's own values validate   %d user(s), %d profile(s)"
                  % (len(users), len(profs)))
        else:
            failures.append("THE PINNED CHART'S OWN VALUES ARE UNEXPRESSIBLE: %s" % msg)

    for label, euc, want in CASES:
        ok, msg = validates(schema, wrap(euc))
        checked += 1
        if ok == want:
            print("  ok   %-8s %s" % ("accepted" if ok else "refused", label))
        elif want:
            failures.append("should be ACCEPTED but was refused: %s -- %s" % (label, msg))
        else:
            failures.append("SHOULD BE REFUSED BUT WAS ACCEPTED: %s" % label)

    print()
    if failures:
        for f in failures:
            print("::error::%s" % f)
        print("::error::check-clickhouse-users-schema: %d of %d case(s) wrong."
              % (len(failures), checked))
        return 1
    print("check-clickhouse-users-schema: OK -- %d case(s), both hash forms, "
          "and a user with no authentication is refused." % checked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
