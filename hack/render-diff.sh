#!/usr/bin/env bash
# render-diff.sh — compare two PUBLISHED installer versions by OBJECT IDENTITY.
#
# Answers the only question a pre-roll gate needs: does any object that exists today
# disappear, appear, or change helm ownership when we move to the new version?
#
# WHY OBJECT IDENTITY AND NOT A DIFF. A pins diff cannot answer it (the sse-proxy
# ClusterRole and the portal are why), and a byte diff answers a different question —
# every release changes version stamps, so a byte diff is never empty and tells you
# nothing. We read FIELDS, so generated material is excluded by construction.
#
# WHY LIVE VALUES ARE THE PRIMARY MODE. Rendering chart DEFAULTS predicts nothing about
# a configured cluster. On 0.3.455->0.3.458 the defaults render reported a spurious
# `+1 Secret/hyperdx-admin-creds` — correct for a default profile (installer#526 made the
# gate follow alert-provider's own feature) and false for krateo-057, which runs
# specialistAgents=true and saw no change at all. A gate that is wrong about the cluster
# you are rolling is worse than no gate, so --from-cluster is the mode to use and
# --defaults says loudly that it is not a prediction.
#
# USAGE
#   render-diff.sh <fromVersion> <toVersion> --from-cluster <kube-context>[:<release>[:<ns>]]
#   render-diff.sh <fromVersion> <toVersion> --values <file>
#   render-diff.sh <fromVersion> <toVersion> --defaults          # NOT a cluster prediction
#
#   release defaults to `installer`, namespace to `krateo-system`.
#
# EXIT CODES
#   0  compared successfully, no object added or removed
#   1  usage / fetch / render error
#   2  VOID — a render produced too few objects, so the comparison is meaningless
#   3  compared successfully, objects WERE added or removed (read them and decide)
set -euo pipefail

OCI_REPO="${OCI_REPO:-oci://ghcr.io/krateo-platformops/charts/installer}"
# The chart declares kubeVersion >= 1.36.0-0; helm's default assumption is lower and
# fails, which looks like a broken chart and is not.
KUBE_VERSION="${KUBE_VERSION:-1.36.0}"
# Below this an empty-ish render would match another empty-ish render trivially. Two
# FAILED renders once compared as "byte-identical" here, which is the reason this exists.
MIN_OBJECTS="${MIN_OBJECTS:-10}"

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

[ $# -ge 3 ] || usage
FROM="$1"; TO="$2"; shift 2
MODE=""
VALUES_FILE=""
CLUSTER_SPEC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from-cluster) MODE="cluster"; CLUSTER_SPEC="${2:?--from-cluster needs <context>[:<release>[:<ns>]]}"; shift 2 ;;
    --values)       MODE="values";  VALUES_FILE="${2:?--values needs a file}"; shift 2 ;;
    --defaults)     MODE="defaults"; shift ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$MODE" ] || { echo "pick one of --from-cluster / --values / --defaults" >&2; usage; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

case "$MODE" in
  cluster)
    CTX="${CLUSTER_SPEC%%:*}"
    REST="${CLUSTER_SPEC#*:}"
    if [ "$REST" = "$CLUSTER_SPEC" ]; then REL="installer"; NS="krateo-system"
    else
      REL="${REST%%:*}"
      NS="${REST#*:}"
      [ "$NS" = "$REST" ] && NS="krateo-system"
    fi
    VALUES_FILE="$WORK/live-values.yaml"
    echo "  values: LIVE from context=$CTX release=$REL namespace=$NS"
    # Assert the named release IS an installer release before trusting its values. Pointing
    # this at a sibling release (installer-agent, say) would render the INSTALLER chart with
    # that release's values and produce a confident, wrong answer. The VOID gate catches the
    # worst version of that by accident; this catches it on purpose.
    REL_CHART=$(helm --kube-context "$CTX" -n "$NS" list -a -f "^${REL}\$" -o json 2>/dev/null \
      | python3 -I -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["chart"] if r else "")')
    # `installer-*` is NOT the right glob: `installer-agent-0.3.3` matches it. A helm chart
    # string is `<name>-<version>`, so require a DIGIT straight after `installer-`.
    if [ -z "$REL_CHART" ]; then
      echo "FATAL: no release named '$REL' in $NS on $CTX" >&2
      exit 1
    fi
    if ! printf '%s' "$REL_CHART" | grep -Eq '^installer-[0-9]'; then
      echo "FATAL: release '$REL' is chart '$REL_CHART', not the installer chart." >&2
      echo "       Rendering the installer chart with another release's values gives a confident wrong answer." >&2
      exit 1
    fi
    echo "  release chart: $REL_CHART"
    helm --kube-context "$CTX" -n "$NS" get values "$REL" -o yaml > "$VALUES_FILE" \
      || { echo "FATAL: could not read live values for $REL in $NS" >&2; exit 1; }
    # A release with no user-supplied values yields `null`, which renders as defaults and
    # would silently become the misleading mode this flag exists to avoid.
    if [ "$(tr -d '[:space:]' < "$VALUES_FILE")" = "null" ] || [ ! -s "$VALUES_FILE" ]; then
      echo "FATAL: $REL has NO user-supplied values — this would be a defaults render, not a cluster prediction." >&2
      exit 1
    fi
    echo "  value keys: $(python3 -I -c '
import sys, yaml
v = yaml.safe_load(open(sys.argv[1])) or {}
print(len(v), sorted(v))' "$VALUES_FILE")"
    ;;
  values)
    [ -s "$VALUES_FILE" ] || { echo "FATAL: $VALUES_FILE is missing or empty" >&2; exit 1; }
    echo "  values: $VALUES_FILE"
    ;;
  defaults)
    echo "  values: CHART DEFAULTS"
    echo "  *** THIS IS NOT A PREDICTION ABOUT ANY CONFIGURED CLUSTER. ***"
    echo "  *** Feature-gated objects differ under a real value set; use --from-cluster for a gate. ***"
    ;;
esac

render() {
  local version="$1"
  local out="$2"
  local dir="$WORK/chart-$version"
  mkdir -p "$dir"
  helm pull "$OCI_REPO" --version "$version" --untar --untardir "$dir" >/dev/null 2>&1 \
    || { echo "FATAL: cannot pull installer $version" >&2; return 1; }
  # Chart.yaml ships version/appVersion as the CHART_VERSION placeholder in-repo, but a
  # PUBLISHED chart has them substituted, so no fixup is needed here. Dependencies do
  # have to be fetched before templating.
  ( cd "$dir/installer" && helm dependency build >/dev/null 2>&1 ) || true
  local -a args
  args=(template release-under-test "$dir/installer" --kube-version "$KUBE_VERSION")
  [ "$MODE" != "defaults" ] && args+=(-f "$VALUES_FILE")
  helm "${args[@]}" > "$out" 2> "$out.err" || true
  python3 -I - "$out" > "$out.ids" <<'PY'
import sys, yaml
# Identity = (kind, namespace, name, helm release-name, managed-by). Reading FIELDS rather
# than bytes is what excludes generated material; ownership is included because a change of
# owning release is exactly as dangerous as a disappearance.
for doc in yaml.safe_load_all(open(sys.argv[1])):
    if not isinstance(doc, dict) or "kind" not in doc:
        continue
    meta = doc.get("metadata") or {}
    ann = meta.get("annotations") or {}
    lab = meta.get("labels") or {}
    print("\t".join([
        doc.get("kind", "?"),
        meta.get("namespace") or "-",
        str(meta.get("name", "?")),
        ann.get("meta.helm.sh/release-name", "-"),
        lab.get("app.kubernetes.io/managed-by", "-"),
    ]))
PY
  sort -o "$out.ids" "$out.ids"
}

render "$FROM" "$WORK/a" || exit 1
render "$TO"   "$WORK/b" || exit 1

NA=$(wc -l < "$WORK/a.ids" | tr -d ' ')
NB=$(wc -l < "$WORK/b.ids" | tr -d ' ')
echo "  $FROM: $NA objects      $TO: $NB objects"

if [ "$NA" -lt "$MIN_OBJECTS" ] || [ "$NB" -lt "$MIN_OBJECTS" ]; then
  echo "  VOID: a render produced fewer than $MIN_OBJECTS objects — this comparison means nothing." >&2
  echo "  --- stderr from both renders ---" >&2
  head -5 "$WORK/a.err" "$WORK/b.err" >&2
  exit 2
fi

CHANGED=0
echo "  --- objects REMOVED in $TO ---"
if comm -23 "$WORK/a.ids" "$WORK/b.ids" | grep -q .; then
  comm -23 "$WORK/a.ids" "$WORK/b.ids" | awk -F'\t' '{printf "    - %s %s/%s  (release=%s managed-by=%s)\n",$1,$2,$3,$4,$5}'
  CHANGED=1
else
  echo "    none"
fi
echo "  --- objects ADDED in $TO ---"
if comm -13 "$WORK/a.ids" "$WORK/b.ids" | grep -q .; then
  comm -13 "$WORK/a.ids" "$WORK/b.ids" | awk -F'\t' '{printf "    + %s %s/%s  (release=%s managed-by=%s)\n",$1,$2,$3,$4,$5}'
  CHANGED=1
else
  echo "    none"
fi

[ "$CHANGED" -eq 0 ] || exit 3
