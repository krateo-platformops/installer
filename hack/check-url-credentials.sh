#!/usr/bin/env bash
# check-url-credentials.sh — does any gateway access-log line carry a credential in the URL?
#
# The acceptance criterion of installer#395, made re-runnable. It is also the durable form of a
# security check: "no component puts a bearer token in a URL" is not a one-time migration, it is
# an invariant that any new call site can break.
#
# IT REFUSES TO REPORT A ZERO WITHOUT A NON-ZERO CONTROL. A zero from a detector that has never
# found anything is worthless, and this issue's own history is full of exactly that: a 401 detector
# matching the substring ' 401 ' returned 0 in BOTH arms, which reads as "no 401s ever" and
# actually means "this predicate matches nothing". So both windows are mandatory, and a zero in the
# control arm withholds the verdict instead of passing.
#
# THE DETECTOR IS STRUCTURAL, NOT A SUBSTRING. It looks for a query string on the logged path,
# never for the parameter's NAME. Substring-matching the name counts the DOCUMENTATION of the
# parameter: the one estate-wide hit of an earlier name-based search was sse-proxy's own startup
# banner, which names the fallback in a log line rather than using it.
#
# AND IT IS SCOPED TO /events AND /notifications, WHICH IS LOAD-BEARING. A query string is only
# evidence of a credential on those two paths, because they are logged bare once the token moved
# to the header. Everywhere else a query string is ordinary API usage — `?limit=`, `?name=` — so an
# unscoped version of this predicate matches almost everything: measured on krateo-057, it returned
# 75350 of 80563 rows "carrying a credential" over 7 days, against the 76 of 105 the scoped
# predicate finds. That is the mirror image of the void-detector failure this file warns about: not
# a predicate that matches nothing, but one that matches everything, where a non-zero result is
# equally uninformative. The magnitudes are what exposed it — 767x more route rows than the
# measurement it was meant to reproduce.
#
# IT NEVER SELECTS A LOG BODY. Pre-roll rows contain real bearer tokens — 76 of them in the
# control window used for #395 — so dumping bodies would re-inject live credentials into
# otel_logs, which the filelog receiver scrapes from this job's own stdout. Only counts and
# query-stripped paths leave the query.
#
# USAGE
#   check-url-credentials.sh <kube-context> <test-from> <test-to> <control-from> <control-to>
#   timestamps are 'YYYY-MM-DD HH:MM:SS' in UTC
#
# EXIT CODES
#   0  the test window is clean AND the control arm proves the detector works
#   1  usage error
#   2  verdict WITHHELD — the control arm found nothing, so the test result means nothing
#   3  the test window CONTAINS url credentials
set -euo pipefail

CTX="${1:?usage: check-url-credentials.sh <ctx> <test-from> <test-to> <control-from> <control-to>}"
TFROM="${2:?test-from}"; TTO="${3:?test-to}"
CFROM="${4:?control-from}"; CTO="${5:?control-to}"

NS=krateo-system
SVC=krateo-clickstack-clickhouse-clickhouse-headless
SECRET=mcp-clickhouse-credentials
JOB="url-cred-check-$(date +%s)"

# The gateway's access log is tab-separated `key=value` fields. A credential in the URL shows up
# as a query string on the logged path; the path is otherwise logged bare.
read -r -d '' SQL <<SQLEOF || true
SELECT
  'test'    AS arm,
  countIf(match(extract(Body, 'http\\\\.path=([^ \\\\t]*)'), '^/(events|notifications)'))   AS route_rows,
  countIf(match(extract(Body, 'http\\\\.path=([^ \\\\t]*)'), '^/(events|notifications)[^ ]*\\\\?')) AS with_url_credential
FROM otel_logs
WHERE ServiceName = 'krateo-agent-gateway-platform'
  AND Timestamp >= toDateTime('${TFROM}', 'UTC')
  AND Timestamp <  toDateTime('${TTO}', 'UTC')
UNION ALL
SELECT
  'control' AS arm,
  countIf(match(extract(Body, 'http\\\\.path=([^ \\\\t]*)'), '^/(events|notifications)'))   AS route_rows,
  countIf(match(extract(Body, 'http\\\\.path=([^ \\\\t]*)'), '^/(events|notifications)[^ ]*\\\\?')) AS with_url_credential
FROM otel_logs
WHERE ServiceName = 'krateo-agent-gateway-platform'
  AND Timestamp >= toDateTime('${CFROM}', 'UTC')
  AND Timestamp <  toDateTime('${CTO}', 'UTC')
FORMAT TSV
SQLEOF

cleanup() { kubectl --context "$CTX" -n "$NS" delete job "$JOB" --ignore-not-found >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Credentials reach the pod via secretKeyRef and are sent as HTTP headers — never in a URL, never
# echoed. The query itself contains no credential pattern, so printing it is safe.
kubectl --context "$CTX" -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
spec:
  ttlSecondsAfterFinished: 120
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: q
          image: curlimages/curl:8.11.0
          env:
            - name: CH_USER
              valueFrom:
                secretKeyRef:
                  name: ${SECRET}
                  key: username
            - name: CH_PASS
              valueFrom:
                secretKeyRef:
                  name: ${SECRET}
                  key: password
            - name: SQL
              value: |
$(printf '%s\n' "$SQL" | sed 's/^/                /')
          command:
            - sh
            - -c
            - >-
              curl -sS --fail-with-body
              -H "X-ClickHouse-User: \$CH_USER"
              -H "X-ClickHouse-Key: \$CH_PASS"
              --data-binary "\$SQL"
              "http://${SVC}.${NS}.svc:8123/"
YAML

kubectl --context "$CTX" -n "$NS" wait --for=condition=complete --timeout=120s "job/$JOB" >/dev/null 2>&1 \
  || { echo "check-url-credentials: the query job did not complete — verdict WITHHELD" >&2
       kubectl --context "$CTX" -n "$NS" logs "job/$JOB" 2>&1 | tail -5 >&2; exit 2; }

OUT="$(kubectl --context "$CTX" -n "$NS" logs "job/$JOB" 2>/dev/null)"
t_rows=$(printf '%s\n' "$OUT" | awk -F'\t' '$1=="test"    {print $2}')
t_cred=$(printf '%s\n' "$OUT" | awk -F'\t' '$1=="test"    {print $3}')
c_rows=$(printf '%s\n' "$OUT" | awk -F'\t' '$1=="control" {print $2}')
c_cred=$(printf '%s\n' "$OUT" | awk -F'\t' '$1=="control" {print $3}')

for v in "$t_rows" "$t_cred" "$c_rows" "$c_cred"; do
  [ -n "$v" ] || { echo "check-url-credentials: could not parse the result — verdict WITHHELD" >&2
                   printf '%s\n' "$OUT" | tail -5 >&2; exit 2; }
done

pct() { [ "$2" -gt 0 ] && echo "scale=1; 100*$1/$2" | bc || echo "n/a"; }

echo "  CONTROL  ${CFROM} -> ${CTO}"
echo "           ${c_rows} route rows, ${c_cred} with a URL credential  ($(pct "$c_cred" "$c_rows")%)"
echo "  TEST     ${TFROM} -> ${TTO}"
echo "           ${t_rows} route rows, ${t_cred} with a URL credential  ($(pct "$t_cred" "$t_rows")%)"
echo

if [ "$c_cred" -eq 0 ]; then
  echo "VERDICT WITHHELD: the control arm found 0 URL credentials, so this predicate has never"
  echo "matched anything and the test result carries no information. Fix the detector, not the code."
  exit 2
fi
if [ "$t_rows" -eq 0 ]; then
  echo "VERDICT WITHHELD: the test window contains 0 route rows — nothing was exercised in it."
  exit 2
fi
if [ "$t_cred" -gt 0 ]; then
  echo "FAIL: ${t_cred} access-log lines in the test window carry a credential in the URL."
  exit 3
fi
echo "PASS: 0 URL credentials across ${t_rows} route rows, from a detector that found ${c_cred} in the control window."
