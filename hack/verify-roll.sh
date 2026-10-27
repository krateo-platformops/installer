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
#   verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]... [--expect-zero-evidence <name>]...
#
# EXIT CODES
#   0  the roll changed exactly what was expected, and nothing is unhealthy
#   1  usage error
#   2  the after-capture is incomplete — verdict withheld, NOT a pass
#   3  identity: an unexpected uid changed, or an expected one did not
#   4  health: a composition is Ready=False, or Unknown for an unexpected reason
set -euo pipefail

CTX="${1:?usage: verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]...}"
BEFORE="${2:?usage: verify-roll.sh <kube-context> <before.tsv> [--expect-uid-change <name>]...}"
shift 2

EXPECT_UID=()
EXPECT_ZE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-uid-change)    EXPECT_UID+=("${2:?--expect-uid-change needs a name}"); shift 2 ;;
    --expect-zero-evidence) EXPECT_ZE+=("${2:?--expect-zero-evidence needs a name}"); shift 2 ;;
    *) echo "verify-roll: unknown argument '$1'" >&2; exit 1 ;;
  esac
done

[ -r "$BEFORE" ] || { echo "verify-roll: cannot read before-capture '$BEFORE'" >&2; exit 1; }
grep -q 'INCOMPLETE' "$BEFORE" && { echo "verify-roll: the BEFORE capture is marked INCOMPLETE" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
AFTER="$(mktemp -t verify-roll-after)"
trap 'rm -f "$AFTER"' EXIT

echo "== capturing AFTER state =="
"$HERE/capture-compositions.sh" "$CTX" "$AFTER" || { echo "verify-roll: after-capture failed — verdict WITHHELD" >&2; exit 2; }

rc=0

echo
echo "== identity: which compositions were recreated =="
# Column 2 is name, column 4 is uid. Compare name->uid maps.
moved="$(join -t'	' -j1 \
  <(awk -F'\t' 'NF>3 {print $2"\t"$4}' "$BEFORE" | sort) \
  <(awk -F'\t' 'NF>3 {print $2"\t"$4}' "$AFTER"  | sort) \
  -o 0,1.2,2.2 2>/dev/null | awk -F'\t' '$2 != $3 {print $1}' || true)"

printf '  expected to change : %s\n' "${EXPECT_UID[*]:-<none>}"
printf '  actually changed   : %s\n' "$(echo "$moved" | tr '\n' ' ' | sed 's/ *$//;s/^$/<none>/')"

for want in ${EXPECT_UID[@]+"${EXPECT_UID[@]}"}; do
  echo "$moved" | grep -qx "$want" || { echo "  FAIL expected $want to be recreated; its uid did not change"; rc=3; }
done
for got in $moved; do
  printf '%s\n' ${EXPECT_UID[@]+"${EXPECT_UID[@]}"} | grep -qx "$got" \
    || { echo "  FAIL $got was recreated and was NOT expected to be — an uninstall/reinstall nobody asked for"; rc=3; }
done
[ "$rc" = 0 ] && echo "  ok  identity matches the declared expectation exactly"

# Rows present in only one capture are neither "moved" nor "same" — join drops them silently.
nb="$(awk -F'\t' 'NF>3' "$BEFORE" | wc -l | tr -d ' ')"
na="$(awk -F'\t' 'NF>3' "$AFTER"  | wc -l | tr -d ' ')"
if [ "$nb" != "$na" ]; then
  echo "  FAIL composition COUNT moved: $nb -> $na. join compares the intersection, so added or"
  echo "       removed rows would otherwise be invisible to the identity check above."
  rc=3
else
  echo "  ok  composition count unchanged ($nb), so the identity diff covered every row"
fi

echo
echo "== health: every composition's Ready condition =="
kubectl --context "$CTX" get compositiondefinitions -A \
  -o jsonpath='{range .items[*]}{.status.apiVersion}{" "}{.status.resource}{"\n"}{end}' 2>/dev/null \
  | awk 'NF==2' | sort -u > "$AFTER.gvks" || true

bad=0; ze=0
while read -r apiver res; do
  [ -n "$res" ] || continue
  kubectl --context "$CTX" get "$res.${apiver%%/*}" -A \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{"\t"}{.reason}{end}{"\n"}{end}' 2>/dev/null \
  | awk -F'\t' 'NF>=2' || true
done < "$AFTER.gvks" | sort -u > "$AFTER.ready" || true

while IFS=$'\t' read -r name status reason; do
  [ -n "$name" ] || continue
  case "$status" in
    True) ;;
    False)
      echo "  FAIL $name Ready=False reason=$reason"; bad=1 ;;
    Unknown)
      if [ "$reason" = "ZeroEvidence" ]; then
        echo "  ok   $name Ready=Unknown/ZeroEvidence — zero children evaluated, health not claimed"
        ze=$((ze+1))
        printf '%s\n' ${EXPECT_ZE[@]+"${EXPECT_ZE[@]}"} | grep -qx "$name" \
          || { echo "  FAIL $name reports ZeroEvidence and was NOT declared with --expect-zero-evidence"; bad=1; }
      else
        echo "  FAIL $name Ready=Unknown reason=$reason — an unrecognised reason is the case nobody planned for"; bad=1
      fi ;;
    *) echo "  FAIL $name Ready='$status' reason=$reason — not one of True/False/Unknown"; bad=1 ;;
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
echo "  -- evaluated $nready compositions, $ze reporting ZeroEvidence"
[ "$bad" = 0 ] || rc=4

rm -f "$AFTER.gvks" "$AFTER.ready"

echo
case $rc in
  0) echo "VERDICT: the roll changed exactly what was declared, and nothing is unhealthy." ;;
  3) echo "VERDICT: IDENTITY MISMATCH — see the FAIL lines above." ;;
  4) echo "VERDICT: UNHEALTHY COMPOSITION — see the FAIL lines above." ;;
esac
exit $rc
