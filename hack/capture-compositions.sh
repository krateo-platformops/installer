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

CRDS=$(kubectl --context="$CTX" get crds -o json 2>/dev/null \
  | jq -r '.items[] | select(.spec.group=="composition.krateo.io") | "\(.spec.names.plural).composition.krateo.io"')
if [ -z "$CRDS" ]; then
  echo "FATAL: no composition.krateo.io CRDs found on context $CTX" >&2
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
ROWS=$(wc -l < "$OUT" | tr -d ' ')
echo "captured $ROWS compositions from $CRD_COUNT CRDs; CRs seen $SEEN; read errors $ERRORS"

if [ "$ERRORS" -gt 0 ]; then
  # Mark the file itself, not just this output: a stderr warning is lost the moment
  # someone diffs the file in a later shell.
  echo "#INCOMPLETE-CAPTURE $ERRORS CRD read(s) failed — DO NOT DIFF" >> "$OUT"
  echo "WARNING: $ERRORS CRD read(s) failed — this capture is INCOMPLETE and must not be diffed" >&2
  exit 2
fi
