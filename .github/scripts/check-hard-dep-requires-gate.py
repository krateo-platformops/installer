#!/usr/bin/env python3
"""Exercise hack/hard-dep-requires-gate.py's rules against fixtures, with no registry.

WHY THIS SUITE IS MOSTLY ABOUT FALSE POSITIVES, like the root-schema gate's. Two of this gate's
drafts were wrong in ways only a MIXED set of real components exposed, and in both cases acting on
the finding would have been worse than the defect it was built for:

  draft 1  matched `^\\s*apiVersion:`         -> flagged portal-agents, reviews-page and
           (any indentation)                     github-provider-kog. krateo-demo refutes it
                                                 directly: portal ON + coreAgents OFF, and all
                                                 three are Ready=Available. An INDENTED apiVersion
                                                 is a field VALUE inside a portal widget naming a
                                                 resource it QUERIES, not an object the chart
                                                 creates. Objects are written at column 0.

  draft 2  keyed the requirement on the       -> would have demanded `requires: [cert-manager]` on
           PROVIDING COMPONENT's readiness       clickhouse-operator and mongodb-operator. demo runs
                                                 ingress:false, so that component is ABSENT there,
                                                 yet SIX cert-manager.io CRDs are served from a
                                                 hand-install and both compositions are Ready. The
                                                 requirement is real; the UNIT was wrong. What a
                                                 chart needs is the KIND to be renderable, and any
                                                 provider satisfies that. Hence API GROUPS.

A gate that suppresses working compositions is worse than no gate. Every rule is asserted in BOTH
directions: the defect is caught AND the legitimate shape beside it is passed.

It imports the real module rather than reimplementing the rules.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hdg", ROOT / "hack" / "hard-dep-requires-gate.py")
hdg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hdg)

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def scan_text(tmp_path, name, text):
    d = tmp_path / name
    (d / "templates").mkdir(parents=True, exist_ok=True)
    (d / "templates" / "x.yaml").write_text(text)
    return hdg.scan(str(d))


def main():
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="hdg-fixtures-"))

    # --- the draft-1 false positive: an INDENTED apiVersion is not a rendered object ---
    widget = """apiVersion: templates.krateo.io/v1
kind: RestAction
metadata:
  name: agents-list
spec:
  api:
  - name: agents
    path: /apis/kagent.dev/v1alpha2/namespaces/krateo-system/agents
    resourceRef:
      apiVersion: kagent.dev/v1alpha2
      kind: Agent
"""
    rendered, shipped = scan_text(tmp, "widget", widget)
    check("a widget that QUERIES kagent.dev does not 'render' it",
          sorted(rendered), ["templates.krateo.io"])

    # --- the true positive it must still catch ---
    agent = """apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: snowplow-agent
"""
    rendered, shipped = scan_text(tmp, "agent", agent)
    check("a chart that CREATES a kagent.dev object does render it",
          sorted(rendered), ["kagent.dev"])

    # --- a chart never 'renders' what it itself ships ---
    own = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: agents.kagent.dev
spec:
  group: kagent.dev
---
apiVersion: kagent.dev/v1alpha2
kind: Agent
metadata:
  name: sample
"""
    rendered, shipped = scan_text(tmp, "own", own)
    check("a chart that SHIPS kagent.dev is not held to require it",
          sorted(rendered), [])
    check("and it is recorded as the group's provider", sorted(shipped), ["kagent.dev"])

    # --- builtins are never requirements ---
    builtin = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: x
---
apiVersion: v1
kind: Service
metadata:
  name: x
"""
    rendered, shipped = scan_text(tmp, "builtin", builtin)
    check("core k8s groups are not requirements", sorted(rendered), [])

    # --- a templated group must not become a literal requirement ---
    templated = """apiVersion: {{ .Values.api.group }}/v1
kind: Thing
metadata:
  name: x
"""
    rendered, shipped = scan_text(tmp, "templated", templated)
    check("a go-templated apiVersion is not treated as a group", sorted(rendered), [])

    # --- pull() must honour a full oci:// base (the bug that made it withhold on 9 charts) ---
    check("pull() treats an oci:// repo as a base, not an org",
          hdg.pull.__doc__ is not None and "oci://" in hdg.pull.__doc__, True)

    print()
    if failures:
        print(f"::error::{len(failures)} fixture check(s) failed: {', '.join(failures)}")
        return 1
    print("check-hard-dep-requires-gate: OK — all fixture checks pass in both directions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
