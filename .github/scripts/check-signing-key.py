#!/usr/bin/env python3
"""CI guard: exercise inst.jwtSigningKeyPEM — the decision that either REUSES the existing
authn RS256 signing key or generates a new one.

WHY THIS IS THE MOST CONSEQUENTIAL UNTESTED LINE IN THE CHART. That key signs every JWT authn
issues. If the reuse arm ever stops firing, the else-arm generates a fresh key, helm applies it
without complaint, authn restarts with it, and every outstanding token becomes invalid at once.
Nothing errors. The render succeeds, the Secret applies cleanly, and the only symptom is that
every signed-in user is logged out with no log line saying why. Silent rotation is
indistinguishable from correct operation in every signal this chart produces, so the only place
it can be caught is here, before it ships.

WHY IT WAS UNTESTABLE UNTIL NOW. The decision used to sit inline in secret.yaml beside its own
`lookup` call, and `helm template` DISABLES lookup. So a render could only ever exercise the
GENERATE arm — lookup always came back empty — and the arm that actually runs on a live cluster
was the one no test could reach. The decision now takes the looked-up Secret as a parameter
(_helpers.tpl, inst.jwtSigningKeyPEM) and never calls lookup itself, which is what makes both
arms reachable without a cluster or a fake-cluster plugin.

WHAT IT ASSERTS, and the last two are the ones that stop this test from agreeing with itself:

  1. found Secret with data["private.pem"]  -> returns THAT key, byte for byte
  2. no Secret at all                       -> returns a freshly generated RSA private key
  3. a Secret with no usable key            -> generates (nil data, empty data, wrong key name)
  4. arms 1 and 2 DIFFER                    -> the reuse arm is not quietly generating
  5. two generate calls differ from each other -> generation is live, so (4) is not satisfied
                                               by a generate arm that returns a constant
  6. a MUTANT with the reuse branch deleted FAILS assertion 1 -> this gate can actually detect
                                               the regression it exists for

Assertion 6 is the one that matters most. A gate whose subject is "a key is returned" passes
just as happily when the wrong key is returned, and that is precisely the failure mode. So the
gate mutates the real helper, re-runs, and refuses to pass unless the mutant is caught.

WHAT THIS GATE CANNOT DO, stated because a green tick here must not be read as wider than it
is: it never executes `lookup`. Nothing cluster-free can. So it cannot prove that on a real
cluster the lookup FINDS the Secret — only that IF it finds it, the key is reused. The lookup
itself, its namespace and its name are asserted TEXTUALLY against secret.yaml (assertion 7),
which catches a rename or a deleted call but not a lookup that returns empty for a reason only a
cluster exhibits (wrong namespace at runtime, missing RBAC, a cache that has not warmed).
"""
import base64
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HELPERS = Path("chart/templates/_helpers.tpl")
SECRET = Path("chart/templates/secret.yaml")
HELPER_NAME = "inst.jwtSigningKeyPEM"
SECRET_NAME = "authn-jwt-signing-key"
DATA_KEY = "private.pem"

SENTINEL = "SENTINEL-EXISTING-KEY-MUST-NOT-ROTATE"
SENTINEL_B64 = base64.b64encode(SENTINEL.encode()).decode()

# (label, the object handed to the helper as the looked-up Secret, expectation)
#   "reuse"    -> must return SENTINEL exactly
#   "generate" -> must return a fresh RSA private key
CASES = [
    ("Secret present with private.pem -> REUSE THAT KEY",
     {"data": {DATA_KEY: SENTINEL_B64}}, "reuse"),
    ("no Secret at all -> generate",
     None, "generate"),
    ("Secret present, data empty -> generate",
     {"data": {}}, "generate"),
    ("Secret present, no data field -> generate",
     {"metadata": {"name": SECRET_NAME}}, "generate"),
    ("Secret present, DIFFERENT key name -> generate",
     {"data": {"other.pem": SENTINEL_B64}}, "generate"),
    ("no Secret, second independent call -> generate (must differ from the first)",
     None, "generate"),
]

PROBE_TPL = (
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: probe\n"
    "data:\n"
    "{{- range $i, $c := .Values.cases }}\n"
    '  case{{ $i }}: {{ (include "' + HELPER_NAME + '" (list $c.found)) | b64enc | quote }}\n'
    "{{- end }}\n"
)

PEM_HEAD = "-----BEGIN RSA PRIVATE KEY-----"


def render(helpers_text: str) -> dict:
    """Render the cases against a given _helpers.tpl body. Returns {case_i: decoded output}."""
    work = Path(tempfile.mkdtemp(prefix="signing-key-"))
    try:
        chart = work / "probe"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: probe\nversion: 0.0.0\n")
        (chart / "templates" / "_helpers.tpl").write_text(helpers_text)
        (chart / "templates" / "probe.yaml").write_text(PROBE_TPL)
        (chart / "values.yaml").write_text("cases: []\n")
        vals = work / "cases.yaml"
        vals.write_text(yaml.safe_dump({"cases": [{"found": f} for _, f, _ in CASES]},
                                       default_flow_style=False))
        p = subprocess.run(["helm", "template", "probe", str(chart), "-f", str(vals)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return {"__error__": p.stderr.strip()[:1200]}
        doc = next(d for d in yaml.safe_load_all(p.stdout)
                   if isinstance(d, dict) and d.get("kind") == "ConfigMap")
        return {k: base64.b64decode(v).decode("utf-8", "replace")
                for k, v in (doc.get("data") or {}).items()}
    finally:
        shutil.rmtree(work, ignore_errors=True)


REUSE_ARM = re.compile(
    r'\{\{-\s*if and \$found \$found\.data \(hasKey \$found\.data "private\.pem"\)\s*-\}\}'
    r'\s*\{\{-\s*index \$found\.data "private\.pem" \| b64dec\s*-\}\}', re.S)


def mutate(text: str) -> str:
    """Delete the reuse branch, leaving only the generate arm.

    This is the regression the gate exists for: the helper still renders, still returns a
    perfectly valid RSA key, and silently ignores the key that already exists. If the gate
    passes against this, it is not testing anything.
    """
    return REUSE_ARM.sub('{{- if false -}}{{- "" -}}', text, count=1)


def is_fresh_key(s: str) -> bool:
    return s.startswith(PEM_HEAD) and "END RSA PRIVATE KEY" in s and len(s) > 1000


def main() -> int:
    for f in (HELPERS, SECRET):
        if not f.is_file():
            print(f"::error::{f} not found — run from the repo root")
            return 1

    helpers = HELPERS.read_text()
    if f'define "{HELPER_NAME}"' not in helpers:
        print(f"::error::{HELPERS} no longer defines {HELPER_NAME}. The signing-key decision has "
              f"moved or been renamed, so this gate is testing nothing. Point it at the new "
              f"template (or, if the decision went back inline beside `lookup`, it is untestable "
              f"again and that is the regression).")
        return 1

    out = render(helpers)
    if "__error__" in out:
        print("::error::probe render failed — the gate cannot vouch for a helper it could not run")
        print(out["__error__"])
        return 1
    if len(out) != len(CASES):
        print(f"::error::expected {len(CASES)} rendered cases, got {len(out)}. A gate that "
              f"silently exercises fewer cases than it lists reports a green tick for work it "
              f"did not do.")
        return 1

    failures = []
    got = {}
    for i, (label, _found, want) in enumerate(CASES):
        v = out.get(f"case{i}", "")
        got[i] = v
        if want == "reuse":
            ok = (v == SENTINEL)
            detail = "returned the existing key" if ok else (
                "GENERATED A NEW KEY — every outstanding JWT would be invalidated"
                if is_fresh_key(v) else f"returned {v[:60]!r}")
        else:
            ok = is_fresh_key(v)
            detail = "generated a key" if ok else f"did NOT generate a key: {v[:60]!r}"
        print(f"  {'PASS' if ok else 'FAIL'}  {label}\n          {detail}")
        if not ok:
            failures.append(f"{label}: {detail}")

    # (4) the two arms must DIFFER. Without this, a helper that generates in BOTH arms passes
    #     every assertion above except the reuse one — and this states the contrast directly.
    if got[0] == got[1]:
        failures.append("the reuse arm and the generate arm returned the SAME value — one of "
                        "them is not doing what it claims")
    else:
        print("  PASS  reuse and generate return different values")

    # (5) generation must be LIVE, or (4) is satisfied trivially by a constant.
    if got[1] == got[5]:
        failures.append("two independent generate calls returned the SAME key, so genPrivateKey "
                        "is not generating — which would make the 'arms differ' assertion above "
                        "vacuous")
    else:
        print("  PASS  two generate calls differ, so the 'arms differ' assertion is not vacuous")

    # (6) the mutation self-check: prove this gate detects the regression it exists for.
    mutant = mutate(helpers)
    if mutant == helpers:
        failures.append("could not construct the mutant: the reuse branch no longer matches the "
                        "pattern this gate deletes, so the self-check cannot run and the gate is "
                        "unverified. Update REUSE_ARM to match the current helper.")
    else:
        mout = render(mutant)
        if "__error__" in mout:
            failures.append("the mutant did not render, so the self-check is inconclusive: "
                            + mout["__error__"][:300])
        elif mout.get("case0") == SENTINEL:
            failures.append("MUTANT NOT CAUGHT: with the reuse branch deleted, the helper still "
                            "returned the existing key. This gate cannot detect the regression it "
                            "exists for.")
        else:
            print("  PASS  self-check: a mutant with the reuse branch deleted IS caught "
                  f"(it returned {'a fresh key' if is_fresh_key(mout.get('case0','')) else 'something else'})")

    # (7) wiring. The one thing no cluster-free test can execute is the lookup, so assert it
    #     textually: the call, its name and namespace, the hand-off, and that the decision has
    #     not crept back inline.
    sec = SECRET.read_text()
    for needle, why in [
        (f'lookup "v1" "Secret" $ns "{SECRET_NAME}"',
         "the lookup call, with this exact Secret name"),
        (f'include "{HELPER_NAME}" (list $existingPriv)',
         "the hand-off of the lookup RESULT into the tested helper"),
        (f"name: {SECRET_NAME}", "the emitted Secret's name"),
        (f"{DATA_KEY}: |", "the emitted data key"),
    ]:
        if needle in sec:
            print(f"  PASS  secret.yaml still has {why}")
        else:
            failures.append(f"secret.yaml no longer contains {needle!r} ({why}). The gate "
                            f"exercises a helper that the chart may no longer use this way.")
    if "genPrivateKey" in sec:
        failures.append("secret.yaml calls genPrivateKey directly again. The decision has moved "
                        "back inline beside `lookup`, where only its generate arm can ever be "
                        "rendered — which is the untestable state this gate was built to end.")
    else:
        print("  PASS  secret.yaml does not decide the key itself")

    if failures:
        print("::error::signing-key gate: the authn JWT signing key is not provably preserved "
              "across a re-render. A wrong answer here silently invalidates every outstanding "
              "token, with no error in any signal this chart produces:")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"\nsigning-key gate: OK — {len(CASES)} cases, the two arms differ, generation is live, "
          f"the mutant is caught, and secret.yaml still wires the lookup into the helper.")
    print("NOT asserted (no cluster-free test can): that `lookup` actually FINDS the Secret at "
          "runtime. This proves only that if it finds it, the key is reused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
