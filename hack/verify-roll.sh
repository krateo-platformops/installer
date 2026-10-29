#!/usr/bin/env bash
# verify-roll.sh — assert a roll changed what it was EXPECTED to change, and nothing else.
#
# WHY THIS EXISTS. capture-compositions.sh gives you the before/after identity diff; what it
# cannot do is say whether that diff is the RIGHT one. Deciding that afterwards is how an
# expectation gets chosen to fit the outcome: two uids moved, both plausible, roll declared fine.
# So the expected set is declared on the COMMAND LINE, before the roll, with --expect-uid-change,
# and anything else that moved is a failure.
#
# WHY READINESS IS CHECKED SEPARATELY FROM IDENTITY. A composition reporting Ready=Unknown with
# reason ZeroEvidence (core-provider#165, 2.13.36+) is NOT unhealthy — it means the child-health
# rollup evaluated zero children and the engine declines to claim health it never measured. A
# check that tests `Ready == "True"` reads that as broken, which is the exact false negative
# installer#545 exists to avoid. So: Ready=False is a failure, Unknown/ZeroEvidence is reported
# and tolerated, and Unknown for ANY OTHER reason is a failure — because an unrecognised reason
# is the case nobody has thought about.
#
# THE CHECK THAT MATTERS MOST is the umbrella's own: the Installer composition must stay
# Available/Ready and must not be converging on a child. If the engine's ZeroEvidence branch
# misfires, the symptom is the Installer composition flipping to Creating and naming the
# zero-evidence children — a platform-wide stall, as opposed to a cosmetic status.
#
# USAGE
#   verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]...
#                  [--expect-zero-evidence <name>]... [--expect-managed-shrink <ns/name>]...
#
# EXIT CODES
#   0  the roll changed exactly what was expected, and nothing is unhealthy
#   1  usage error
#   2  the after-capture is incomplete — verdict withheld, NOT a pass
#   3  identity: an unexpected uid changed, or an expected one did not
#   4  health: a composition is Ready=False for a reason OTHER than Creating, or Unknown for an
#      unexpected reason, or a composition's own managed set shrank without being declared
#   5  WITHHELD: identity matched and nothing is unhealthy, but one or more compositions report
#      Ready=False/Creating -- still converging. Not a pass; re-run when they settle.
set -euo pipefail

CTX="${1:?usage: verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]...}"
BEFORE="${2:?usage: verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]...}"
shift 2

EXPECT_UID=()
EXPECT_ZE=()
EXPECT_SHRINK=()
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-uid-change)    EXPECT_UID+=("${2:?--expect-uid-change needs a name}"); shift 2 ;;
    --expect-zero-evidence) EXPECT_ZE+=("${2:?--expect-zero-evidence needs a name}"); shift 2 ;;
    --expect-managed-shrink) EXPECT_SHRINK+=("${2:?--expect-managed-shrink needs a <ns/name>}"); shift 2 ;;
    *) echo "verify-roll: unknown argument '$1'" >&2; exit 1 ;;
  esac
done

[ -r "$BEFORE" ] || { echo "verify-roll: cannot read before-capture '$BEFORE'" >&2; exit 1; }
grep -q 'INCOMPLETE' "$BEFORE" && { echo "verify-roll: the BEFORE capture is marked INCOMPLETE" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
# The Ready-condition decision lives in ONE place and is exercised by CI
# (.github/scripts/check-ready-classify.py). Keeping a second copy inline here would mean the test
# proves a file nothing uses -- the exact failure it exists to prevent.
# shellcheck source=hack/ready-classify.sh
. "$HERE/ready-classify.sh"
AFTER="$(mktemp -t verify-roll-after)"
trap 'rm -f "$AFTER"' EXIT

echo "== capturing AFTER state =="
"$HERE/capture-compositions.sh" "$CTX" "$AFTER" || { echo "verify-roll: after-capture failed — verdict WITHHELD" >&2; exit 2; }

rc=0

echo
echo "== identity: which compositions were recreated =="
# Done in python, NOT with join(1). BSD/macOS join has no -j flag — it prints a usage error and
# emits ZERO rows, so an identity check built on it reports "no unexpected changes" because it
# compared nothing, and prints a green line while being structurally blind. That is exactly what
# this script's own detector proof caught before it was ever used on a roll.
# Python also lets added/removed rows be named rather than inferred from a count mismatch.
( python3 - "$BEFORE" "$AFTER" ${EXPECT_UID[@]+"${EXPECT_UID[@]}"} <<'PYIDENT'
import sys
before_path, after_path, *expected = sys.argv[1:]

def load(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            f = line.rstrip("\n").split("\t")
            if len(f) > 3 and f[1] and f[3]:
                out[f[1]] = f[3]
    return out

b, a = load(before_path), load(after_path)
moved   = sorted(n for n in (b.keys() & a.keys()) if b[n] != a[n])
added   = sorted(a.keys() - b.keys())
removed = sorted(b.keys() - a.keys())
exp     = sorted(set(expected))

print(f"  rows: {len(b)} before, {len(a)} after, {len(b.keys() & a.keys())} comparable")
print(f"  expected to change : {' '.join(exp) or '<none>'}")
print(f"  actually changed   : {' '.join(moved) or '<none>'}")

rc = 0
for want in exp:
    if want not in a:
        print(f"  FAIL {want} was declared with --expect-uid-change but is not present after the roll"); rc = 3
    elif want not in moved:
        print(f"  FAIL expected {want} to be recreated; its uid did not change "
              f"({b.get(want, '<absent before>')})"); rc = 3
for got in moved:
    if got not in exp:
        print(f"  FAIL {got} was recreated and was NOT expected to be "
              f"({b[got]} -> {a[got]}) — an uninstall/reinstall nobody asked for"); rc = 3
for n in added:
    print(f"  FAIL {n} APPEARED during the roll and was not there before"); rc = 3
for n in removed:
    print(f"  FAIL {n} DISAPPEARED during the roll"); rc = 3

# The comparison must have had something to compare. An empty intersection would produce no
# FAIL lines at all and read as a pass.
if not (b.keys() & a.keys()):
    print("  FAIL the two captures share no composition names — nothing was compared, verdict WITHHELD")
    sys.exit(2)
if rc == 0:
    print("  ok  identity matches the declared expectation exactly, over every row in both captures")
sys.exit(rc)
PYIDENT
) && irc=0 || irc=$?
# `|| irc=$?` is load-bearing, not style. Under `set -e` a bare command that exits non-zero kills
# the script immediately — so the identity block exiting 3 meant health was NEVER CHECKED and no
# VERDICT was printed at all. A gate that stops at its first finding reports less than it looked
# at, and the operator cannot tell "healthy" from "never examined".
#
# This bit twice over: the invocation that found it piped the script to `tail`, so the `$?` I read
# was tail's status (0) rather than the script's. Two instruments agreeing on a wrong answer.
if [ "$irc" = 2 ]; then
  echo "VERDICT WITHHELD: the identity comparison could not be made."
  exit 2
fi
[ "$irc" = 0 ] || rc=3

echo
echo "== the umbrella's managed-children count =="
# core-provider#230: `status.managed` is replaced WHOLESALE from each render and is the only input
# to the health rollup, so a render yielding fewer resources shrinks the denominator, the rollup
# finds the smaller set healthy, and the composition reports Available with nothing in the
# condition saying the set moved. The identity check above cannot see it either — the pruned
# components' CRs are gone, so they are absent from BOTH captures and 83 == 83 holds while 14
# children are missing from the render.
#
# Measured on krateo-057 during the 0.3.465 roll: 122 -> 108 -> 122, Ready=True throughout. A
# verification that ran at 108 reported the 14 as DISAPPEARED, which was true of that instant and
# false as a verdict. This check exists so the SHRINK is named rather than inferred from a
# downstream symptom.
BEFORE_MANAGED=$(grep -oE '^#MANAGED-CHILDREN [0-9]+' "$BEFORE" | grep -oE '[0-9]+' || true)
AFTER_MANAGED=$(grep -oE '^#MANAGED-CHILDREN [0-9]+' "$AFTER" | grep -oE '[0-9]+' || true)
if [ -z "$BEFORE_MANAGED" ]; then
  echo "  -- the BEFORE capture predates this check and records no count."
  echo "     Cannot compare, so NOT asserting it. Re-capture to get this arm; a missing"
  echo "     comparison is reported here rather than silently passing."
elif [ -z "$AFTER_MANAGED" ]; then
  echo "  FAIL the AFTER capture has no managed-children count — the umbrella's Ready message did"
  echo "       not carry one, which is itself worth looking at"
  rc=4
elif [ "$AFTER_MANAGED" -lt "$BEFORE_MANAGED" ]; then
  echo "  FAIL the umbrella manages FEWER children than before: $BEFORE_MANAGED -> $AFTER_MANAGED."
  echo "       Components are missing from the render. The composition can still report Available,"
  echo "       because the rollup only evaluates what was rendered (core-provider#230). Either the"
  echo "       roll is still mid-prune, or components were pruned and not re-emitted."
  rc=4
elif [ "$AFTER_MANAGED" -gt "$BEFORE_MANAGED" ]; then
  echo "  ok   the umbrella manages MORE children than before: $BEFORE_MANAGED -> $AFTER_MANAGED"
  echo "       (expected when a roll adds components; stated so it is not mistaken for noise)"
else
  echo "  ok   managed children unchanged at $AFTER_MANAGED — the render set is the same size"
fi

echo
echo "== each composition's OWN managed set =="
# The umbrella arm above is the umbrella's count and catches core-provider#230. This arm is the
# other 82, and it exists because NO readiness check can cover them: core-provider#248 means a
# composition that loses its `status.managed` reports Ready=True/Available over everything it
# renders and never evaluates. The condition is green BECAUSE the evidence is gone.
#
# Confirmed on krateo-057 at installer-0.3.466: 7 of 83 are already in that state. They are named
# as a baseline rather than failed, so a core-provider fix moving them to Unknown/ZeroEvidence
# reads as the transition it is instead of as a regression here.
#
# The comparison is its own file so CI can exercise it against fixtures with no cluster -- the same
# reason ready-classify.sh is its own file. .github/scripts/check-managed-drift.py shells into THIS
# script rather than restating its rules.
# Build the flag/value PAIRS as separate array elements. `"${ARR[@]/#/--expect-shrink }"` looks
# equivalent and is not: it yields ONE argument per name with a space inside it, so the script
# receives `--expect-shrink ns/name` as a single argv entry and dies on an unknown argument. Same
# class of bug as putting flags in a scalar -- it is the quoting that keeps them together.
SHRINK_ARGS=()
for n in ${EXPECT_SHRINK[@]+"${EXPECT_SHRINK[@]}"}; do SHRINK_ARGS+=(--expect-shrink "$n"); done
( python3 "$HERE/managed-drift.py" "$BEFORE" "$AFTER" \
    ${SHRINK_ARGS[@]+"${SHRINK_ARGS[@]}"} ) && mrc=0 || mrc=$?
if [ "$mrc" = 2 ]; then
  echo "  (this arm asserted nothing — see above. It does NOT contribute to the verdict.)"
elif [ "$mrc" != 0 ]; then
  rc=4
fi

echo
echo "== health: every composition's Ready condition =="
kubectl --context "$CTX" get compositiondefinitions -A \
  -o jsonpath='{range .items[*]}{.status.apiVersion}{" "}{.status.resource}{"\n"}{end}' 2>/dev/null \
  | awk 'NF==2' | sort -u > "$AFTER.gvks" || true

bad=0; ze=0; converging=0
while read -r apiver res; do
  [ -n "$res" ] || continue
  kubectl --context "$CTX" get "$res.${apiver%%/*}" -A \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{"\t"}{.reason}{end}{"\n"}{end}' 2>/dev/null \
  | awk -F'\t' 'NF>=2' || true
done < "$AFTER.gvks" | sort -u > "$AFTER.ready" || true

while IFS=$'\t' read -r name status reason; do
  [ -n "$name" ] || continue
  case "$(classify_ready "$status" "$reason")" in
    healthy) ;;
    converging)
      # In flight: NOT a fault and NOT a pass. Observed live on the 0.3.466 roll, where the
      # umbrella read Ready=False/Creating seconds after a child was recreated while the roll was
      # completing normally. This used to be reported as UNHEALTHY COMPOSITION.
      echo "  .... $name Ready=False reason=Creating — still converging, not a fault"
      converging=$((converging+1)) ;;
    zeroevidence)
      ze=$((ze+1))
      # Print ONE line per object, not an ok immediately contradicted by a FAIL. The old form
      # emitted both for an undeclared ZeroEvidence composition, which reads as the check
      # disagreeing with itself.
      if printf '%s\n' ${EXPECT_ZE[@]+"${EXPECT_ZE[@]}"} | grep -qx "$name"; then
        echo "  ok   $name Ready=Unknown/ZeroEvidence — declared, and zero children evaluated"
      else
        echo "  FAIL $name reports Unknown/ZeroEvidence and was NOT declared with"
        echo "       --expect-zero-evidence. Health was never claimed for it, so this is not"
        echo "       necessarily a fault — but an undeclared one means the roll changed which"
        echo "       compositions can evaluate their children, which wants a decision not a shrug."
        bad=1
      fi ;;
    fail)
      echo "  FAIL $name Ready=$status reason=${reason:-<none>}"; bad=1 ;;
  esac
done < "$AFTER.ready"

for want in ${EXPECT_ZE[@]+"${EXPECT_ZE[@]}"}; do
  awk -F'\t' -v n="$want" '$1==n && $2=="Unknown" && $3=="ZeroEvidence"' "$AFTER.ready" | grep -q . \
    || { echo "  FAIL expected $want to report Unknown/ZeroEvidence and it does not"; bad=1; }
done

# The instrument must have found something. A readiness sweep that evaluated zero compositions
# would print no failures and look like a pass.
nready="$(wc -l < "$AFTER.ready" | tr -d ' ')"
if [ "$nready" -lt 1 ]; then
  echo "  FAIL the readiness sweep evaluated 0 compositions — verdict WITHHELD, not a pass"
  rm -f "$AFTER.gvks" "$AFTER.ready"; exit 2
fi
echo "  -- evaluated $nready compositions, $ze reporting ZeroEvidence, $converging converging"
[ "$bad" = 0 ] || rc=4
# A real fault outranks "still converging": if anything is genuinely broken, say so rather than
# withholding. Only withhold when converging is the ONLY thing standing between this and a pass.
if [ "$bad" = 0 ] && [ "$converging" -gt 0 ] && [ "$rc" = 0 ]; then rc=5; fi

rm -f "$AFTER.gvks" "$AFTER.ready"

echo
case $rc in
  0) echo "VERDICT: the roll changed exactly what was declared, and nothing is unhealthy." ;;
  3) echo "VERDICT: IDENTITY MISMATCH — see the FAIL lines above." ;;
  4) echo "VERDICT: UNHEALTHY COMPOSITION — see the FAIL lines above." ;;
  5) echo "VERDICT: WITHHELD — $converging composition(s) report Ready=False/Creating, i.e. still"
     echo "         converging. Identity matched and nothing is unhealthy, but the roll has not"
     echo "         finished, so this is NOT a pass. Re-run when they settle."
     echo "         A sample taken mid-convergence is not the level." ;;
esac
exit $rc
