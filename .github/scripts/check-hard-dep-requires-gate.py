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


def scan_text(tmp_path, name, text, crds=None, values=None):
    """Build a minimal chart, RENDER it, and scan the output -- which is what the guard now does.

    The suite used to call hdg.scan() on template TEXT. The guard no longer reads template text at
    all: it renders, because a template body under a false `{{- if }}` is not an object the chart
    creates (installer#614). So these fixtures render too, or they would be testing a code path
    that no longer exists."""
    d = tmp_path / name
    (d / "templates").mkdir(parents=True, exist_ok=True)
    (d / "Chart.yaml").write_text(f"apiVersion: v2\nname: {name}\nversion: 0.0.1\n")
    (d / "templates" / "x.yaml").write_text(text)
    if crds:
        (d / "crds").mkdir(parents=True, exist_ok=True)
        (d / "crds" / "c.yaml").write_text(crds)
    out, err = hdg.render(name, str(d), values)
    if out is None:
        raise AssertionError(f"fixture {name!r} did not render, so it tests nothing: {err}")
    rendered, shipped, errs = hdg.scan_rendered(out, str(d))
    if errs:
        raise AssertionError(f"fixture {name!r} produced parse errors: {errs}")
    return rendered, shipped


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

    # --- a value-driven apiVersion is RESOLVED, not skipped ---
    # The old fixture here asserted that a `{{ }}`-containing apiVersion was NOT treated as a
    # group, because the guard read template text and could only have taken the literal string.
    # That behaviour no longer exists: the guard renders, so the value is resolved and the real
    # group is detected. Asserting the old expectation would be asserting a bug.
    templated = """apiVersion: {{ .Values.api.group }}/v1
kind: Thing
metadata:
  name: x
"""
    rendered, shipped = scan_text(tmp, "templated", templated,
                                  values={"api": {"group": "vendor.example"}})
    check("a value-driven apiVersion is resolved to its real group",
          sorted(rendered), ["vendor.example"])

    # And the other half: a chart that cannot render must not read as "renders nothing". The guard
    # withholds on that, and here we assert the primitive it withholds on actually fails.
    d = tmp / "unrenderable"
    (d / "templates").mkdir(parents=True, exist_ok=True)
    (d / "Chart.yaml").write_text("apiVersion: v2\nname: unrenderable\nversion: 0.0.1\n")
    (d / "templates" / "x.yaml").write_text(templated)   # same template, NO values
    out, err = hdg.render("unrenderable", str(d))
    check("a template whose value is missing FAILS to render rather than rendering empty",
          out is None and err is not None, True)

    # --- a `group:` FIELD is not a shipped CRD (the builders-page false positive) ---
    # An earlier version of the render-based guard matched any `group:` line in the output, so a
    # RESTAction naming the group it QUERIES was recorded as PROVIDING it. builders-page was
    # credited with shipping ogen.krateo.io on three RESTAction templates and zero CRDs, which made
    # github-provider-kog's real requirement look satisfied and disabled this suite's own self-test.
    restaction = """apiVersion: templates.krateo.io/v1
kind: RestAction
metadata:
  name: kog-restdefinitions
spec:
  api:
  - name: rds
    path: /apis/ogen.krateo.io/v1alpha1/restdefinitions
    filter:
      group: ogen.krateo.io
"""
    rendered, shipped = scan_text(tmp, "restaction", restaction)
    check("a `group:` field inside a RestAction is NOT a shipped CRD group",
          sorted(shipped), [])
    check("and the RestAction's own group is what it renders",
          sorted(rendered), ["templates.krateo.io"])

    # --- a CRD in crds/ IS shipped, even though helm never renders that directory ---
    crdsdir = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: things.vendor.example
spec:
  group: vendor.example
"""
    rendered, shipped = scan_text(tmp, "crdsdir", """apiVersion: vendor.example/v1
kind: Thing
metadata:
  name: x
""", crds=crdsdir)
    check("a CRD under crds/ is recorded as shipped though helm does not render it",
          sorted(shipped), ["vendor.example"])
    check("so the chart is not held to require its own group", sorted(rendered), [])

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
