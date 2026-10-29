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
  # No case folding: every comparison below is against a Go constant, exact by contract.
  case "$st" in
    True) printf 'healthy' ;;
    False)
      # ONLY THE EXACT STRING `Creating`, case-SENSITIVE. This was case-insensitive and that was
      # wrong -- I had carried a normalisation from the wrong boundary.
      #
      # core-provider applies EqualFold at childhealth.go:239, but to `p.reason`, which
      # readProjectedHealth reads from `.status.health.reason` (support.go:286) -- the
      # AUTHOR-DECLARED PROJECTION INPUT. The OUTPUT is different: setCreatingStatus calls
      # condition.Creating(), which sets `Reason: ReasonCreating` where
      # `ReasonCreating string = "Creating"` (unstructured-runtime condition.go:21,52). So a
      # blueprint author may write `creating` and have it accepted, but the CONDITION this script
      # reads can only ever carry the constant.
      #
      # Therefore a case variant HERE is off-contract, not a tolerable spelling. Accepting it would
      # mean reporting "healthy progress" for a value the controller cannot emit -- and if
      # core-provider ever changed that constant, a tolerant match would keep passing silently
      # while a strict one goes loudly red. Loud is the failure mode worth having.
      #
      # The frontend session argued this and was right; verified against the source above rather
      # than conceded.
      if [ "$rsn" = "Creating" ]; then printf 'converging'; else printf 'fail'; fi ;;
    Unknown)
      # Unknown is ONLY acceptable for the one reason that means "zero children were evaluated".
      # Bare Unknown must NOT be accepted: a genuinely converging child also reports Unknown, and
      # that one has to be waited for rather than waved through.
      if [ "$rsn" = "ZeroEvidence" ]; then printf 'zeroevidence'; else printf 'fail'; fi ;;
    *) printf 'fail' ;;
  esac
}
