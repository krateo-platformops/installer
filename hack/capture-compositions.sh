#!/usr/bin/env bash
# capture-compositions.sh — snapshot the identity and version state of EVERY composition
# on a cluster, for before/after diffing across a roll.
#
# WHY IDENTITY AND NOT READINESS. A readiness roll-up tells you the cluster is healthy
# after a roll; it does not tell you whether the SAME objects are healthy. A component
# version bump legitimately recreates its composition CR under the new served version —
# the uid changes — and anything else changing uid is an uninstall/reinstall nobody asked
# for. The negative half (rows whose uid did NOT change) is the evidence, so capture all.
#
# WHY IT REFUSES TO PRODUCE A PARTIAL FILE. A capture that silently missed a CRD would
# diff clean against a later capture that also missed it, or invent churn against one that
# did not. Either way the diff lies, so a read error makes the whole capture unusable and
# this says so on stderr AND in the file.
#
# USAGE
#   capture-compositions.sh <kube-context> <output-file>
#
# Diff two captures on identity alone (the first four columns):
#   diff <(cut -f1-4 before.tsv) <(cut -f1-4 after.tsv)
#
# COLUMNS
#   namespace  name  kind  uid  generation  creationTimestamp
#   composition-version  composition-origin-version  ownerRefs  helm-release-name
#
# A trailing `#MANAGED-CHILDREN <n>` line records the umbrella's own child count (see below).
#
# The last four are the core-provider#199 classifier: a composition with NEITHER a
# `meta.helm.sh/release-name` NOR a `krateo.io/composition-name` is STANDALONE, and a
# standalone composition whose write-once origin stamp trails its CompositionDefinition
# is in the armed configuration where a delete orphans every release the umbrella owns.
# Cross-tabbed on all 83 compositions of krateo-057: 59 both, 24 neither, 0 with exactly one.
#
# EXIT CODES
#   0  complete capture, safe to diff
#   1  usage error, or no composition CRDs found
#   2  INCOMPLETE capture — do NOT diff it
set -euo pipefail

CTX="${1:?usage: capture-compositions.sh <kube-context> <output-file>}"
OUT="${2:?usage: capture-compositions.sh <kube-context> <output-file>}"

# `|| crdrc=$?` and capturing stderr are both LOAD-BEARING, not style.
#
# This block used to read `CRDS=$(kubectl ... 2>/dev/null | jq ...)` followed by an `if [ -z ... ]`
# that printed FATAL. Under `set -euo pipefail` a failing command substitution in an assignment
# exits the script IMMEDIATELY, so the `if` — and the only diagnostic this script had for a bad
# context, expired credentials or an unreachable cluster — was UNREACHABLE. Combined with the
# `2>/dev/null`, a wrong --context produced a SILENT exit 1 with no output whatsoever. Hit live on
# 2026-10-28 while gating a roll: the script said nothing at all, and an empty result is
# indistinguishable from a broken instrument through a silenced stderr.
CRDERR=$(mktemp -t capture-crderr); trap 'rm -f "$CRDERR"' EXIT
crdrc=0
CRDS=$(kubectl --context="$CTX" get crds -o json 2>"$CRDERR" \
  | jq -r '.items[] | select(.spec.group=="composition.krateo.io") | "\(.spec.names.plural).composition.krateo.io"') || crdrc=$?
if [ "$crdrc" -ne 0 ]; then
  echo "FATAL: could not list CRDs on context '$CTX' (exit $crdrc). kubectl said:" >&2
  sed 's/^/  /' "$CRDERR" >&2
  exit 1
fi
if [ -z "$CRDS" ]; then
  echo "FATAL: context '$CTX' answered, but serves NO composition.krateo.io CRDs." >&2
  echo "  The cluster is reachable and this is a real (empty) answer, not a read failure --" >&2
  echo "  check you named the right cluster before concluding the platform is not installed." >&2
  exit 1
fi

ERRORS=0
SEEN=0
CRD_COUNT=$(printf '%s\n' "$CRDS" | wc -l | tr -d ' ')
: > "$OUT"

while read -r kind; do
  RAW=$(kubectl --context="$CTX" get "$kind" -A -o json 2>&1) || true
  if ! printf '%s' "$RAW" | jq -e . >/dev/null 2>&1; then
    ERRORS=$((ERRORS + 1))
    echo "ERROR reading $kind" >&2
    continue
  fi
  SEEN=$((SEEN + $(printf '%s' "$RAW" | jq -r '.items | length')))
  printf '%s' "$RAW" | jq -r '.items[]? |
    [ .metadata.namespace, .metadata.name, .kind, .metadata.uid,
      (.metadata.generation | tostring), .metadata.creationTimestamp,
      (.metadata.labels["krateo.io/composition-version"] // "-"),
      (.metadata.annotations["krateo.io/composition-origin-version"] // "UNSTAMPED"),
      ((.metadata.ownerReferences // []) | length | tostring),
      (.metadata.annotations["meta.helm.sh/release-name"] // "none")
    ] | @tsv' >> "$OUT"
done <<< "$CRDS"

sort -o "$OUT" "$OUT"

# The umbrella's OWN managed-children count, recorded as a trailing comment so a before/after
# diff can see it move.
#
# WHY THIS IS NOT REDUNDANT WITH THE ROWS ABOVE. core-provider#230: `status.managed` is replaced
# WHOLESALE from each render and is the only input to the health rollup, so a render that yields
# fewer resources shrinks the denominator, the rollup finds the smaller set healthy, and the
# composition reports Available with nothing in the condition saying the set moved. Measured on
# krateo-057 during the 0.3.465 roll: 122 -> 108 -> 122, with Ready=True throughout the 108.
#
# So the composition's own Ready condition cannot distinguish "complete and healthy" from
# "incomplete and healthy about what is left", and a gate that waits on Ready=True can be
# satisfied precisely BECAUSE components are missing. The row count above does not catch it
# either: the pruned components' CRs are gone, so they are absent from both the capture and the
# umbrella, and 83 == 83 holds while 14 children are missing from the render.
# -A rather than a hardcoded namespace: the rest of this script is namespace-agnostic and the
# umbrella is not guaranteed to live in krateo-system on every install.
# READ status.managed DIRECTLY, NOT THE CONDITION MESSAGE.
#
# This used to grep "N managed children" out of the Ready condition's MESSAGE. That is prose: it is
# core-provider's to reword, and a gate reading prose breaks silently when it is reworded -- the
# count would quietly become "unknown" and the comparison would downgrade to "cannot compare"
# without anything failing. `status.managed` is the array the rollup actually consumes (#230), so
# its length is the primary source and the message is a signature of it.
#
# ALSO RECORD EACH CHILD'S apiVersion, not just the count. Window 4 proved the count alone is not
# enough: the umbrella read 122 before the roll and 122 after, both correct, while the composition
# was NOT converged -- the managed SET was right and the managed children's VERSIONS were stale. A
# capture that records only the length can never speak to WHICH children moved, so it cannot
# distinguish "did not happen" from "has not happened yet". These lines are comments, so the row
# count (grep -vc '^#') is unaffected and old captures stay comparable.
MANAGED_JSON=$(kubectl --context="$CTX" get installers.composition.krateo.io -A -o json 2>/dev/null || echo '{}')
MANAGED=$(printf '%s' "$MANAGED_JSON" | jq -r '[.items[]?.status.managed // [] | length] | add // "unknown"')
echo "#MANAGED-CHILDREN ${MANAGED:-unknown}" >> "$OUT"
# FIELD NAMES READ OFF A LIVE ENTRY, NOT ASSUMED. A status.managed entry has exactly:
#   apiVersion, name, namespace, path, resource
# There is NO `kind`. The first version of this line used `.kind // "-"` and would have emitted
# "-" on every row forever -- a field that does not exist, defaulting silently, with uniqueness
# holding only by luck because apiVersion happened to differ. `resource` is the plural
# ("secrets", "krateosseproxies") and `path` is the fully-qualified self link.
printf '%s' "$MANAGED_JSON" | jq -r '
  .items[]? | .status.managed // [] | .[] |
  "#MANAGED-CHILD \(.apiVersion // "-") \(.resource // "-")/\(.namespace // "-")/\(.name // "-")"
' 2>/dev/null | sort >> "$OUT" || true

ROWS=$(grep -vc '^#' "$OUT" | tr -d ' ')
echo "captured $ROWS compositions from $CRD_COUNT CRDs; CRs seen $SEEN; read errors $ERRORS; umbrella managed children ${MANAGED:-unknown}"

if [ "$ERRORS" -gt 0 ]; then
  # Mark the file itself, not just this output: a stderr warning is lost the moment
  # someone diffs the file in a later shell.
  echo "#INCOMPLETE-CAPTURE $ERRORS CRD read(s) failed — DO NOT DIFF" >> "$OUT"
  echo "WARNING: $ERRORS CRD read(s) failed — this capture is INCOMPLETE and must not be diffed" >&2
  exit 2
fi
