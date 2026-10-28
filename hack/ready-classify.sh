#!/usr/bin/env bash
# ready-classify.sh — the ONE place this repo decides what a composition's Ready condition means.
#
# WHY IT IS ITS OWN FILE. verify-roll.sh needs a live cluster, so a decision embedded in it cannot
# be exercised by CI. That is exactly how the chart's own gate shipped a version that only accepted
# "True" and went unexamined for as long as it did -- the fix there was to split the decision out
# so it could be tested (inst.readyFromConditions + check-ready-gate.py). This is the same move for
# the operator-side gate, and it is tested by .github/scripts/check-ready-classify.py, which shells
# into THIS function rather than reimplementing it. A test that reimplements the logic proves the
# reimplementation, not the code.
#
# Sourcing this file defines the function and does nothing else -- no side effects, no `set`.
#
# classify_ready <status> <reason>  ->  prints exactly one of:
#   healthy       Ready=True
#   converging    Ready=False/Creating -- in flight, NOT a fault and NOT a pass
#   zeroevidence  Ready=Unknown/ZeroEvidence -- health could not be assessed at all (#165)
#   fail          everything else
classify_ready() {
  local st="${1-}" rsn="${2-}"
  local lr; lr="$(printf '%s' "$rsn" | tr '[:upper:]' '[:lower:]')"
  case "$st" in
    True) printf 'healthy' ;;
    False)
      # ONLY the literal `Creating` means converging. core-provider#234 recognises that string and
      # DELIBERATELY rejects synonyms (Progressing/Converging/Pending), which fall back to
      # Unavailable. Accepting a synonym here would make this gate disagree with the controller
      # that stamped the condition. The unrecognised->fail direction is also the safe one: a typo
      # read as converging would silently downgrade a real failure and wait forever, whereas a typo
      # read as a failure is loud.
      if [ "$lr" = "creating" ]; then printf 'converging'; else printf 'fail'; fi ;;
    Unknown)
      # Unknown is ONLY acceptable for the one reason that means "zero children were evaluated".
      # Bare Unknown must NOT be accepted: a genuinely converging child also reports Unknown, and
      # that one has to be waited for rather than waved through.
      if [ "$rsn" = "ZeroEvidence" ]; then printf 'zeroevidence'; else printf 'fail'; fi ;;
    *) printf 'fail' ;;
  esac
}
