{{/*
  installer umbrella — helpers (adapted from krateo-openstack-blueprint osh.* helpers).
  Unlike openstack, each component pins its OWN chart version, so the served CRD
  apiVersion is derived per-component from that component's version.
*/}}

{{/* Served apiVersion for a component's generated CRD, from its version string.
     "0.22.2" -> "composition.krateo.io/v0-22-2" */}}
{{- define "inst.apiVersion" -}}
{{- $ver := index . 0 -}}
{{- printf "composition.krateo.io/v%s" ($ver | toString | replace "." "-") -}}
{{- end -}}

{{/* Image for the installer's own lifecycle hook Jobs (self-bootstrap waves + the pre/post-delete
     teardown/cleanup reapers). These hooks only ever run `kubectl` inside `/bin/sh` poll loops
     (no helm/jq/yq/kustomize), so they use the minimal official-family alpine/kubectl (~21MB
     compressed, busybox shell) instead of alpine/k8s (~250MB compressed / 822MB unpacked). The
     smaller pull keeps every hook Job inside its activeDeadlineSeconds on fresh multi-node clusters
     where the fat image previously blew the 300s self-register/self-instance budget (#66). Pinned to
     kubectl 1.36.x to match the installer's k8s>=1.36 floor (was 1.31, already skewed). Single source
     of truth: a pure template constant, NOT a values key, so it never has to be threaded through
     values.schema.json / the crdgen'd Installer CRD / the composition-mode CR (cf. #68). */}}
{{- define "inst.hookImage" -}}alpine/kubectl:1.36.3{{- end -}}

{{/* Optional CompositionDefinition spec.chart extras (registry-level): insecureSkipVerifyTLS
     and credentials, rendered ONLY when set so the chart spec stays minimal on public
     registries. arg: (list $). passwordRef.namespace defaults to the krateo namespace.
     Used by Pass A (definitions.yaml) and the self-bootstrap installer CD. */}}
{{/* Emit spec.chart extras (credentials + insecureSkipVerifyTLS) for a chart pull.
     args: (list $top $effectiveRepo) — $effectiveRepo is the OCI registry base the pull uses
     (a component's `default ociRepo $c.repo`, or ociRepo for the umbrella's own chart).

     Two modes, keyed on registryAuth.registries (see values.yaml / #23):
     - registries[] NON-EMPTY: registry-keyed. Emit ONLY the entry whose `repo` matches
       $effectiveRepo; a pull whose registry has no entry gets NO credentials, so a token is
       never presented to a registry it does not belong to. The global username/passwordRef are
       ignored in this mode.
     - registries[] EMPTY (default): legacy — the global registryAuth credentials apply to every
       pull when enabled (backward-compatible, unchanged).
     insecureSkipVerifyTLS: the global toggle applies to every pull; a matched registries[] entry
     may raise it for its own registry. */}}
{{- define "inst.chartExtras" -}}
{{- $top := index . 0 -}}
{{- $repo := index . 1 -}}
{{- $ra := $top.Values.registryAuth -}}
{{- $registries := $ra.registries | default list -}}
{{- $lines := list -}}
{{- $cred := dict -}}
{{- $hasCred := false -}}
{{- $insecure := $ra.insecureSkipVerifyTLS -}}
{{- if $registries -}}
{{- range $r := $registries -}}
{{- if eq ($r.repo | toString) ($repo | toString) -}}
{{- if $r.passwordRef -}}{{- $cred = $r -}}{{- $hasCred = true -}}{{- end -}}
{{- if $r.insecureSkipVerifyTLS -}}{{- $insecure = true -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- else if $ra.enabled -}}
{{- $cred = $ra -}}{{- $hasCred = true -}}
{{- end -}}
{{- if $insecure -}}
{{- $lines = append $lines "insecureSkipVerifyTLS: true" -}}
{{- end -}}
{{- if $hasCred -}}
{{- $ns := $cred.passwordRef.namespace | default $top.Values.namespaces.krateo -}}
{{- $lines = append $lines (printf "credentials:\n  username: %s\n  passwordRef:\n    name: %s\n    namespace: %s\n    key: %s" ($cred.username | quote) ($cred.passwordRef.name | quote) ($ns | quote) ($cred.passwordRef.key | quote)) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}

{{/* The name of the image-pull dockerconfigjson Secret the installer derives from registryAuth (see
     templates/imagepull-secret.yaml) and injects into the components that ship PRIVATE krateo-agentiko
     IMAGES (autopilot's repo-mcp-server, core-provider-agent's chart-gate, agentgateway-policies' cert-replay
     hop — see inst.privateImageComponents). registryAuth.imagePullSecretName
     overrides the default so a caller can point at a pre-existing dockerconfigjson Secret instead. arg: $top */}}
{{- define "inst.imagePullSecretName" -}}
{{- .Values.registryAuth.imagePullSecretName | default "krateo-registry-image-pull" -}}
{{- end -}}

{{/* SINGLE source of truth for the components that ship PRIVATE krateo-agentiko IMAGES and expose an
     imagePullSecrets knob their chart documents as "wired from registryAuth". `path` is the FULL (dot-free)
     location of that knob inside the component spec, INCLUDING the final key: the injection walks all but the
     last segment as nested maps and sets the last segment to the pull-secret list, so a knob at ANY depth works
     (mcpServers.repoSearch.imagePullSecrets, or the cert-replay hop's certReplayHop.image.pullSecrets). Shared by
     compositions.yaml (the injection target) and inst.imagePullAuths (which registries to derive image-pull
     creds for) so the two lists can never drift. Add a component here to wire its image pull. arg: $top */}}
{{- define "inst.privateImageComponents" -}}
components:
- name: repo-mcp-server
  path:
  - imagePullSecrets
- name: structure-graph-mcp-server
  path:
  - imagePullSecrets
- name: config-refs-mcp-server
  path:
  - imagePullSecrets
- name: core-provider-agent
  path:
  - mcpServers
  - chartGate
  - imagePullSecrets
- name: agentgateway-policies
  path:
  - certReplayHop
  - image
  - pullSecrets
- name: agentgateway-policies
  path:
  - uiTokenHop
  - image
  - pullSecrets
- name: alert-provider
  path:
  - imagePullSecrets
{{- end -}}

{{/* inst.imagePullAuths — the image-pull credential REFERENCES for the private-image components, as a JSON
     array of {host, username, secretName, secretNamespace, secretKey}, deduped to ONE entry per distinct
     image-registry host (a dockerconfigjson auths map is host-keyed). The current pins put both private-image
     components on the same agentiko repo, so they collapse to a single host entry; if two private-image
     components were ever on different credentials at the SAME host, the last one iterated would win — add a
     per-component secret then. Selects each credential the SAME way inst.chartExtras selects the CHART-pull cred,
     so the token that pulls a private IMAGE is exactly the one that authorizes that component's registry:
       - registries[] NON-EMPTY: the entry whose `repo` matches the component's effective repo
         (default ociRepo $c.repo). A component whose registry has no entry contributes nothing, so a token
         is never presented to a registry it does not belong to — and a same-host collision is impossible
         because we key off each private-image component's OWN repo, not the raw registries[] list.
       - registries[] EMPTY: the global registryAuth credential (only when enabled), host taken from the
         component's own repo (correct even if the images ever move off ociRepo's host).
     Lookup-FREE (returns refs, not tokens) so it is cheap enough to double as the inst.imagePullOn predicate;
     imagepull-secret.yaml resolves the token Secret and assembles the dockerconfigjson. Empty output means
     nothing is derivable. arg: (list $top) */}}
{{- define "inst.imagePullAuths" -}}
{{- $top := index . 0 -}}
{{- $ra := $top.Values.registryAuth -}}
{{- $registries := $ra.registries | default list -}}
{{- $names := list -}}
{{- range (include "inst.privateImageComponents" $top | fromYaml).components -}}{{- $names = append $names .name -}}{{- end -}}
{{- $byHost := dict -}}
{{- range $c := (include "inst.componentsYaml" $top | fromYaml).components -}}
{{- if has $c.name $names -}}
{{- $repo := default $top.Values.ociRepo $c.repo -}}
{{- $host := regexReplaceAll "^oci://" ($repo | toString) "" | splitList "/" | first -}}
{{- $cred := dict -}}{{- $has := false -}}
{{- if $registries -}}
{{- range $r := $registries -}}
{{- if and (eq ($r.repo | toString) ($repo | toString)) $r.passwordRef -}}{{- $cred = $r -}}{{- $has = true -}}{{- end -}}
{{- end -}}
{{- else if and $ra.enabled $ra.passwordRef.name -}}
{{- $cred = $ra -}}{{- $has = true -}}
{{- end -}}
{{- if $has -}}
{{- $ns := $cred.passwordRef.namespace | default $top.Values.namespaces.krateo -}}
{{- $_ := set $byHost $host (dict "host" $host "username" $cred.username "secretName" $cred.passwordRef.name "secretNamespace" $ns "secretKey" $cred.passwordRef.key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $byHost -}}{{- values $byHost | toJson -}}{{- end -}}
{{- end -}}

{{/* Is registryAuth image-pull wiring active? The SINGLE predicate shared by the derived-Secret render
     (imagepull-secret.yaml) and the componentValues injection (compositions.yaml) so the two can never
     drift into a dangling reference. True when registryAuth is active (enabled, or registries[] set — which
     is self-contained like inst.chartExtras) AND either the caller supplied a pre-existing imagePullSecretName
     (BYO) OR inst.imagePullAuths resolves at least one derivable credential (global-mode passwordRef, or a
     registries[] entry matching a private-image component's registry). arg: $top */}}
{{- define "inst.imagePullOn" -}}
{{- $ra := .Values.registryAuth -}}
{{- $active := or $ra.enabled (gt (len ($ra.registries | default list)) 0) -}}
{{- if and $active $ra.imagePullSecretName -}}true
{{- else if ne (include "inst.imagePullAuths" (list .)) "" -}}true
{{- end -}}
{{- end -}}

{{/* Is a feature flag enabled? args: (list $ "featureName"); empty featureName => true.
     Final feature set: coreProvider (engine marker, gates nothing), coreAgents (base agent layer),
     portal (the non-agent platform: authn/snowplow/frontend/portal + portal-starter + the
     observability stack), oasgenProvider, specialistAgents, agentGateway. Every component gates
     on its flag. */}}
{{/* No feature is force-enabled: every component gates on its feature flag, so a minimal
     "agent-only" install (just the autopilot + installer-agent) can disable the platform and let
     the agent provision it later by editing the Installer CR. Defaults in values.yaml are all true,
     so a plain `helm install` still brings up the full platform. */}}
{{- define "inst.coreFeatures" -}}{{- end -}}
{{- define "inst.featureEnabled" -}}
{{- $top := index . 0 -}}{{- $feat := index . 1 -}}
{{- if not $feat -}}true
{{- else if has $feat (splitList " " (include "inst.coreFeatures" .)) -}}true
{{- else if index $top.Values.features $feat -}}true{{- end -}}
{{- end -}}

{{/* inst.fillPath — set a nested key on a spec dict only if absent, creating intermediate maps.
     args: (dict "target" $spec "path" (list "a" "b") "value" X). Mutates `target`, renders
     nothing, so a componentValues override always wins over the injection. */}}
{{- define "inst.fillPath" -}}
{{- $m := .target -}}
{{- $path := .path -}}
{{- $value := .value -}}
{{- $leaf := last $path -}}
{{- range $seg := (initial $path) -}}
{{- $next := index $m $seg -}}
{{- if not (kindIs "map" $next) -}}
{{- $next = dict -}}
{{- $_ := set $m $seg $next -}}
{{- end -}}
{{- $m = $next -}}
{{- end -}}
{{- if not (hasKey $m $leaf) -}}{{- $_ := set $m $leaf $value -}}{{- end -}}
{{- end -}}

{{/* inst.forcePath — like inst.fillPath but ALWAYS overwrites the leaf, even if already present.
     args: same as inst.fillPath. Use only where a componentValues override cannot be trusted to
     mean what it says: a boolean whose own schema declares a default reads identically whether a
     user deliberately set it or a sibling override (e.g. cors.allowOrigins) merely dragged the
     rest of that object's schema defaults — including this leaf — into existence. Fill-if-absent
     silently loses that race, so the flag never leaves its schema default. */}}
{{- define "inst.forcePath" -}}
{{- $m := .target -}}
{{- $path := .path -}}
{{- $value := .value -}}
{{- $leaf := last $path -}}
{{- range $seg := (initial $path) -}}
{{- $next := index $m $seg -}}
{{- if not (kindIs "map" $next) -}}
{{- $next = dict -}}
{{- $_ := set $m $seg $next -}}
{{- end -}}
{{- $m = $next -}}
{{- end -}}
{{- $_ := set $m $leaf $value -}}
{{- end -}}

{{/* Does the component's generated CRD exist AND serve this component's version yet?
     args: (list "Kind" "version")
     Version-aware: core-provider derives the served apiVersion from the chart version
     ("0.1.1" -> "v0-1-1"). On a component version bump the Kind already exists but the
     new served version lags until core-provider regenerates the CRD; a typed lookup of
     the not-yet-served apiVersion is a HARD ERROR. Checking spec.versions[].served for
     the exact version makes both Pass B emission and readiness checks tolerate that
     transient (treat as "not ready yet") instead of failing the whole render. */}}
{{- define "inst.crdExists" -}}
{{- $kind := index . 0 -}}{{- $ver := index . 1 -}}{{- $allCRDs := index . 2 -}}
{{- $want := printf "v%s" ($ver | toString | replace "." "-") -}}
{{- $found := "" -}}
{{/* $allCRDs is the cluster's CRD list, looked up ONCE by the caller and threaded through here — see
     the head of definitions.yaml / compositions.yaml. Ranging it in-memory (instead of a fresh
     apiextensions LIST per call) turns the gating from O(components x all-CRDs LISTs) into a single
     LIST; on a full cluster (121 CRDs, 42 large widget schemas) that cut the umbrella render from ~55s
     (which blew the cdc<->chart-inspector timeout) to a few seconds (D9, 2026-07-08). */}}
{{- range $allCRDs -}}
{{- if and (eq .spec.group "composition.krateo.io") (eq .spec.names.kind $kind) -}}
{{- range .spec.versions -}}{{- if and (eq .name $want) .served -}}{{- $found = "true" -}}{{- end -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}

{{/* Is a peer Composition Ready=True? args: (list $ "Kind" "name" "version")
     Guarded: a typed lookup of a Kind whose CRD is not yet registered is a HARD ERROR
     in chart-inspector (server-side), unlike client-side `helm template` which returns
     empty. So short-circuit via inst.crdExists (an apiextensions lookup, always valid)
     before doing the typed Composition lookup. */}}
{{/* inst.readyFromConditions — the ONE place this chart decides whether a dep counts as ready.
     args: (list <conditions list>). Returns "true" or "".

     Split out of inst.ready so it can be TESTED: inst.ready has to `lookup` the live object,
     and `helm template` disables lookup, so the decision was previously unverifiable except
     against a cluster. Shipping a gate nobody can exercise is how the version that only
     accepted "True" went unexamined for as long as it did.

     READY, or HONESTLY UNKNOWN. A dep satisfies this gate when Ready is True, OR when it is
     Unknown for the specific reason that the cdc evaluated NONE of its managed children
     (core-provider#165). Those are different claims and only the first means "healthy":
     ZeroEvidence means "we do not know", and a dependant must not be blocked forever on
     ignorance that is a steady state rather than a transient.

     ZeroEvidence DOES NOT MEAN "the composition has no children", and this comment used to
     imply that it did. Corrected 2026-11-04 against core-provider's source (childhealth.go:155):
     a composition that genuinely manages nothing takes the PRESENT-AND-EMPTY branch and reports
     Ready=True / reason=Available. It never reaches ZeroEvidence. So the justification for this
     arm is NOT "no children, nothing to converge to".

     ZeroEvidence is emitted for TWO different situations, and only one of them justifies this arm:

       1. status.managed was NEVER RECORDED (`!found`). A DEFECT -- core-provider#326: absent
          inventory means no drift is reported, so Observe says up-to-date, so Update never runs,
          and Update is the only writer of status.managed. Self-perpetuating.
       2. The children ARE recorded and NONE of them publish readiness this controller can read
          (unevaluated+unreadable == total, total > 0). Legitimate, permanent, #165's case: a
          blueprint whose children are all custom resources with vocabularies only their own
          authors know never becomes interpretable.

     THIS ARM IS RIGHT FOR (2) AND WRONG FOR (1), and (1) is live. Measured on 2026-11-04:
     krateo-057 has 7 of 85 compositions with no status.managed key at all, every one of them
     reporting Unknown/ZeroEvidence with a deployed helm release and digest != managedDigest --
     agentgateway-policies, observability-page, portal-agents, alerts-page, blueprints-page,
     builders-page, config-refs-mcp-server. krateo-demo has 0 of 43, with observability-page at
     the SAME chart version publishing 75 managed children there. So it is not the chart: those
     seven are passing this gate on the strength of a bug's output, and their dependants are being
     emitted on that basis.

     DELIBERATELY NOT CHANGED. Refusing ZeroEvidence today would gate real dependants -- the 8
     below -- on a condition no operator can clear, which is the same trap pointed the other way.
     The fix is a SECOND reason word so a consumer can tell (1) from (2), by exactly the argument
     that separated ChildrenForbidden from NotObserved. core-provider is taking that to Diego;
     when the word exists, meaning (1) must NOT pass and this arm keeps meaning (2) only.

     KEYED ON `reason`, NEVER ON MESSAGE TEXT. The caveat message is prose and will be
     reworded; a gate reading prose breaks silently when it is.

     AND NEVER ON `Unknown` ALONE. A genuinely converging child also reports Unknown, and
     that one MUST block — it is the transient this gate exists to wait for. Accepting bare
     Unknown would turn every mid-creation dep into "ready" and let dependants render against
     children that do not exist yet.

     WHY THE CONSUMER GOES FIRST. 8 components dep on model-configs (installer-agent,
     krateo-autopilot, incident-agent, authn-agent, snowplow-agent, frontend-agent,
     clickstack-agent, core-provider-agent), and model-configs is one of exactly two
     compositions on krateo-057 at 100% unevaluated children. So the moment the cdc starts
     emitting Unknown, those 8 are gated unless this already accepts it. Landing it first is
     a no-op until then: a reason nothing emits is never matched. */}}
{{- define "inst.readyFromConditions" -}}
{{- $conds := index . 0 -}}
{{- $r := "" -}}
{{/* SYNCED=TRUE IS REQUIRED TOO, and the reason is that the Ready REASON cannot carry it.
     A PAUSED composition reports Ready=True with reason ReconcilePaused and Synced=False: it was
     healthy when it was paused and nothing is maintaining that state now, so it must not satisfy a
     dependency gate -- a dependant would be emitted against a component that is not converging and
     will not.
     A reason-based rule CANNOT express this. unstructured-runtime declares two constants,
     ReasonReconcilePausedReady and ReasonReconcilePaused, whose string VALUE is the same
     "ReconcilePaused", written on Ready and on Synced respectively. So the reason is ambiguous by
     construction and distinguishing the two cases requires reading the condition TYPE anyway.
     Synced is the typed condition that says it once.
     BLAST RADIUS, measured across 166 live composition instances on three clusters before this
     changed: exactly ONE object is newly withheld -- acme-feedback-controller on 057, which is the
     paused one this exists to catch. The 9 Unknown/ZeroEvidence instances are unaffected because
     their Synced is already True, and the one False/Creating instance was already withheld. */}}
{{- $synced := "" -}}
{{- range $conds -}}
{{- if eq (.type | toString) "Synced" -}}
{{- if eq (.status | toString) "True" -}}{{- $synced = "true" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- range $conds -}}
{{- if eq (.type | toString) "Ready" -}}
{{- $s := .status | toString -}}
{{- if eq $s "True" -}}{{- $r = "true" -}}
{{- else if and (eq $s "Unknown") (eq (.reason | toString) "ZeroEvidence") -}}{{- $r = "true" -}}
{{/* ChildrenForbidden (core-provider#318): the cdc's SA got a 403 reading a managed child, so it
     was PREVENTED from observing rather than having observed unhealth. Accepted for the same
     reason as ZeroEvidence and NOT for the reason NotObserved is refused: a 403 from the
     authorization layer is a STEADY STATE -- nothing becomes forbidden later the way something
     becomes observed later. It persists until an operator changes a Role, so blocking a
     dependant on it would block forever. NotObserved stays refused precisely because it is the
     transient kind: an attempt failed and the next may succeed.
     WITHOUT this arm the cost is NOT a fleet-wide outage -- compositions.yaml:124 renders on
     `depsReady OR compositionExists`, so every existing install is unaffected. The cost falls
     entirely on a FRESH install, where a dependant would never be emitted on any pass because
     the condition never clears on its own. That is why this is allowlisted rather than tolerated. */}}
{{- else if and (eq $s "Unknown") (eq (.reason | toString) "ChildrenForbidden") -}}{{- $r = "true" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne $synced "true" -}}{{- $r = "" -}}{{- end -}}
{{- $r -}}
{{- end -}}

{{- define "inst.ready" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $allCRDs := index . 4 -}}
{{- $r := "" -}}
{{/* Ask at ANY SERVED tag, not at $ver (#477, the last pinned-version site). A dep may
     legitimately live at an older served tag across a bump -- inst.emitDecision keeps it there on
     purpose -- so a lookup keyed on the PINNED version misses an object that exists and reports a
     healthy dep as not-Ready. That is absorbed by the $exists escape for a dependant that already
     exists, so the cost is a FRESH dependant held back for the length of the bump rather than
     anything deleted. Still wrong, and the last place in the chart that derived a version instead
     of reading what the cluster serves. */}}
{{- $found := "" -}}
{{- range $t := splitList " " (include "inst.servedTags" (list $kind $allCRDs)) -}}
{{- if and (not $found) $t -}}
{{- $o := lookup (printf "composition.krateo.io/%s" $t) $kind $top.Values.namespaces.krateo $name -}}
{{- if $o -}}
{{- $found = "true" -}}
{{/* nil-guard: a freshly-created dep CR can exist with .status still nil (controller hasn't
     written it yet). $o.status.conditions would then panic ("nil pointer evaluating
     interface {}.conditions") and chart-inspector returns 500, wedging the umbrella mid-chain.
     Coalesce .status to an empty dict so a status-less dep cleanly reads as not-ready. */}}
{{- $st := $o.status | default dict -}}
{{- $r = include "inst.readyFromConditions" (list ($st.conditions | default list)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $r -}}
{{- end -}}

{{/* Are all of a component's deps Ready? args: (list $ $deps $components) */}}
{{- define "inst.depsReady" -}}
{{- $top := index . 0 -}}{{- $deps := index . 1 -}}{{- $comps := index . 2 -}}{{- $allCRDs := index . 3 -}}
{{- $all := "true" -}}
{{- range $d := $deps -}}
  {{- $kind := "" -}}{{- $ver := "" -}}{{- $feat := "" -}}
  {{- range $c := $comps -}}{{- if eq $c.name $d -}}{{- $kind = $c.kind -}}{{- $ver = $c.version -}}{{- $feat = $c.feature -}}{{- end -}}{{- end -}}
  {{- if eq (include "inst.featureEnabled" (list $top $feat)) "true" -}}
  {{- if ne (include "inst.ready" (list $top $kind $d $ver $allCRDs)) "true" -}}{{- $all = "" -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- $all -}}
{{- end -}}

{{/* HARD-REQUIREMENT gate — args: (list (list "group.example.io" ...) $allCRDs)
     Returns "true" if EVERY listed API GROUP has at least one served CRD in the cluster, "" if any
     is missing. An empty or absent `requiresApiGroups:` is vacuously "true".

     WHY IT EXISTS (installer 0.3.474 broke krateo-demo). builder-gate was pinned `feature: portal`
     with `deps: [portal, kagent]`. On krateo-demo — portal ON, coreAgents OFF — it was emitted
     anyway and chart-inspector 500'd on every reconcile:
       no matches for kind "RemoteMCPServer" in version "kagent.dev/v1alpha2"
       ensure CRDs are installed first
     `deps` cannot prevent that: inst.depsReady SKIPS a dep whose own feature is disabled (the
     readiness check sits INSIDE `if inst.featureEnabled $dep.feature`). That is correct for an
     OPTIONAL dep but makes `deps` an ORDERING constraint and nothing more.

     WHY THE UNIT IS AN API GROUP AND NOT A COMPONENT NAME — measured, not assumed. The first draft
     of this gate required the NAMED COMPONENT's composition to be Ready. krateo-demo refutes that
     design: it runs `ingress: false`, so the installer's cert-manager component is absent, yet SIX
     cert-manager.io CRDs are served there from a hand-install, and clickhouse-operator and
     mongodb-operator (which render cert-manager.io objects) are both Ready=Available. A
     component-keyed requirement would have SUPPRESSED two working compositions. What a chart needs
     is the KIND to be renderable, and any provider of that group satisfies it — installer-managed
     or not. So this asks the cluster what is served, which is the question that actually decides
     whether chart-inspector can render.

     PASS B ONLY, deliberately. Pass A registers the CompositionDefinition, which makes core-provider
     pull the chart and run crdgen; it never renders the chart with values, so it cannot produce the
     500. Gating Pass A would also make CD emission lookup-dependent for all 67 components, changing
     bring-up ordering fleet-wide and narrowing check-feature-render.py, which covers the CD layer
     precisely because `helm template` disables lookup.

     NO DRAIN BRANCH, unlike feature-OFF. A feature going off is an orderly teardown, so
     inst.dependentsGone keeps the component alive until its dependents leave. A missing API group is
     not orderly: continuing to emit buys an endless chart-inspector 500 rather than a graceful
     drain, which is the failure this gate exists to stop. So it suppresses at once. */}}
{{- define "inst.requiresApiGroups" -}}
{{- $groups := index . 0 -}}{{- $allCRDs := index . 1 -}}
{{- $all := "true" -}}
{{- range $g := $groups -}}
  {{- $found := "" -}}
  {{- range $crd := $allCRDs -}}
    {{- if eq $crd.spec.group $g -}}
      {{- range $crd.spec.versions -}}{{- if .served -}}{{- $found = "true" -}}{{- end -}}{{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if not $found -}}{{- $all = "" -}}{{- end -}}
{{- end -}}
{{- $all -}}
{{- end -}}

{{/* THE EMISSION DECISION, in ONE place. args: (list $ $component $components $allCRDs)
     Returns the apiVersion TAG to emit the component at, or "" to withhold it. Callers render iff
     the result is non-empty and MUST use the returned tag as the emitted apiVersion.

     It is one helper because it was three expressions. compositions.yaml held the live gate,
     withheld-components.yaml held a SECOND copy of it for reporting, and inst.withheldReason
     re-derived a third answer to explain the outcome. While every copy was keyed on the pinned
     version they agreed by accident. #477 changed the live gate to resolve the emit tag to one the
     cluster actually SERVES -- and the copies did not change, so at the bump instant the reporter
     now calls "withheld" the very component the gate emits. Two gates cannot be kept in step by
     care; the duplication is the defect. Hence: one decision, three readers.

     registerOnly (catalog) blueprints are excluded by the CALLER (they are registered but never
     auto-emitted), so this helper is never asked about them. */}}
{{- define "inst.emitDecision" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}{{- $comps := index . 2 -}}{{- $allCRDs := index . 3 -}}
{{- $emit := include "inst.emitTag" (list $top $c.kind $c.name $c.version $allCRDs) -}}
{{- if $emit -}}
{{- if eq (include "inst.featureEnabled" (list $top $c.feature)) "true" -}}
{{/* HARD API-group requirements are ANDed OUTSIDE the $exists escape on purpose: that escape rides
     out a momentary depsReady flap without pruning a live workload, but a missing API GROUP is not a
     flap -- chart-inspector cannot render the chart at all, so emitting anyway buys an endless 500. */}}
{{- if eq (include "inst.requiresApiGroups" (list $c.requiresApiGroups $allCRDs)) "true" -}}
{{- $deps := include "inst.depsReady" (list $top $c.deps $comps $allCRDs) -}}
{{- $exists := include "inst.compositionExistsAny" (list $top $c.kind $c.name $allCRDs) -}}
{{- if or (eq $deps "true") (eq $exists "true") -}}{{- $emit -}}{{- end -}}
{{- end -}}
{{- else -}}
{{/* feature OFF -> reverse-dependency DRAIN: keep emitting until every component that deps on this
     one is gone, so teardown is leaves-first and never deletes this component (or the CRDs its
     release ships) out from under a still-present dependent. */}}
{{- if ne (include "inst.dependentsGone" (list $top $c.name $comps $allCRDs)) "true" -}}{{- $emit -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Reverse-dependency TEARDOWN gate — the mirror of inst.depsReady. args: (list $ "componentName" $components)
     Returns "true" if NO still-present Composition depends on <componentName> (so it is safe to stop
     rendering it and let the cdc delete it). Returns "" while ANY dependent's Composition still exists,
     so a feature-disabled component stays RENDERED (alive) until its dependents drain. This makes teardown
     reverse-topological (leaves first): a component — and the CRDs its helm release ships — is never deleted
     out from under a still-present dependent, so there are no orphaned CRs, no helm release stuck
     "uninstalling", and no CompositionDefinition delete deadlock. Guarded by inst.crdExists before the typed
     lookup (an unserved Kind is a hard error in chart-inspector), exactly like inst.ready. */}}
{{- define "inst.dependentsGone" -}}
{{- $top := index . 0 -}}{{- $name := index . 1 -}}{{- $comps := index . 2 -}}{{- $allCRDs := index . 3 -}}
{{- $gone := "true" -}}
{{- $isAgent := "" -}}
{{- range $c := $comps -}}{{- if and (eq $c.name $name) $c.agent -}}{{- $isAgent = "true" -}}{{- end -}}{{- end -}}
{{- range $c := $comps -}}
  {{- if and (has $name ($c.deps | default list)) (not (and $c.orchestrator (eq $isAgent "true"))) -}}
    {{/* Ask about the dependent at the tag it is actually SERVED at, not at its pinned version
         (#477 follow-on). The pinned form was correct while a composition could only exist at its
         pinned GVK. inst.emitTag now deliberately keeps a live composition at an OLDER served tag
         across a bump, so a lookup at the PINNED version MISSES an object that exists, $gone stays
         "true", and the draining component is deleted WHILE A DEPENDENT IS STILL THERE -- the
         exact outcome this helper exists to prevent, reintroduced by the fix for the other one. */}}
    {{- if eq (include "inst.compositionExistsAny" (list $top $c.kind $c.name $allCRDs)) "true" -}}{{- $gone = "" -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- $gone -}}
{{- end -}}

{{/* Does a component's OWN Composition CR currently exist? args: (list $ "Kind" "name" "version")
     The typed lookup is safe because inst.emitTag only ever returns a SERVED tag (an unserved Kind
     is a hard error in chart-inspector), so no unserved GVK is ever looked up.
     Used by Pass A (definitions.yaml) to keep a CompositionDefinition registered while its component
     is still present/draining — so the generated CRD OUTLIVES the Composition it serves and is never
     GC'd out from under a still-draining component (the Pass-A counterpart to inst.dependentsGone). */}}
{{- define "inst.compositionExists" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $allCRDs := index . 4 -}}
{{/* Keyed on the tag the instance is actually SERVED at, not on $ver (#477 follow-on). This helper
     answers "is this component still present?" for definitions.yaml, which uses it to keep a
     CompositionDefinition REGISTERED while the component drains. A pinned-version lookup misses a
     live CR that inst.emitTag is holding at an older served tag, so the definition would be
     deregistered and the generated CRD GC'd out from under the live composition -- the orphaned-CR
     outcome this helper was added to prevent. */}}
{{- include "inst.compositionExistsAny" (list $top $kind $name $allCRDs) -}}
{{- end -}}

{{/* External IP of a browser-facing component's LoadBalancer Service. args: (list $ "svcNameSubstring")
     The component's underlying Service is named after its Helm release (e.g. authn-<hash>), so we
     match on a stable substring and return the assigned ingress IP — "" until the cloud LB is ready.
     This is the reconcile-time resolution the values.yaml exposure model documents: each reconcile
     re-runs the lookup, so the frontend config fills in as soon as the peer IPs are assigned. */}}
{{- define "inst.lbip" -}}
{{- $top := index . 0 -}}{{- $sub := index . 1 -}}{{- $ip := "" -}}
{{- range (lookup "v1" "Service" $top.Values.namespaces.krateo "").items -}}
{{- if and (eq (.spec.type | toString) "LoadBalancer") (contains $sub .metadata.name) -}}
{{- $lb := (.status | default dict).loadBalancer | default dict -}}
{{- range ($lb.ingress | default list) -}}{{- if .ip -}}{{- $ip = .ip -}}{{- end -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $ip -}}
{{- end -}}

{{/*
inst.nodeip — a browser-reachable Node IP for NodePort exposure. Prefers an ExternalIP
(public), else falls back to the first InternalIP. "" if the cluster advertises neither (the
caller then omits the key and the next reconcile retries). */}}
{{- /* inst.nodeip — the browser-reachable node IP for NodePort exposure, read from the
       `krateo-nodeip` ConfigMap that the bootstrap resolves once (see self-bootstrap.yaml). We read
       a NAMESPACE ConfigMap here rather than a cluster `lookup "v1" "Node"` on purpose: the umbrella
       is rendered by the per-version `installers-v<ver>` render SA, and a cluster Node list needs a
       cluster grant that every version migration's FRESH SA lacks (wedging the render with
       `nodes is forbidden`). A namespace ConfigMap is always readable by that SA, so the node IP
       survives version migrations with no per-version cluster RBAC. Returns "" if the ConfigMap is
       absent (older bootstrap / not yet resolved) — the caller then omits the key and the next
       reconcile fills it, same graceful-degradation as the pending-LB path. */}}
{{- define "inst.nodeip" -}}
{{- $top := index . 0 -}}
{{- $cm := lookup "v1" "ConfigMap" $top.Values.namespaces.krateo "krateo-nodeip" -}}
{{- $data := (($cm | default dict).data | default dict) -}}
{{- $ext := $data.externalIP | default "" -}}
{{- $int := $data.internalIP | default "" -}}
{{- if $ext -}}{{ $ext }}{{- else -}}{{ $int }}{{- end -}}
{{- end -}}

{{/*
inst.peerurl — a peer component's BROWSER-reachable base URL, resolved from its Service per the
exposure model (values.yaml `exposure.type`), so a consumer's spec.config.<KEY> points at an
address the browser can actually reach — for LoadBalancer AND NodePort alike:
  - LoadBalancer -> http://<lb-ingress-ip|hostname>:<eport>  (eport = shared exposure.port, passed in)
  - NodePort     -> http://<node-ip>:<nodePort>              (nodePort read off the Service)
The Service is matched on a stable name substring ($svcMatch), narrowed to the exposed type.
Returns "" until the external address exists (LB IP still pending / no node IP) — the caller omits
the key so the frontend keeps its default and the next reconcile fills it. A STATIC operator
override (a real external hostname) is handled by the caller and wins over this; this helper only
covers the two auto-exposed modes. */}}
{{- define "inst.peerurl" -}}
{{- $top := index . 0 -}}{{- $sub := index . 1 -}}{{- $eport := index . 2 -}}{{- $url := "" -}}
{{- range (lookup "v1" "Service" $top.Values.namespaces.krateo "").items -}}
{{- if contains $sub .metadata.name -}}
{{- $t := .spec.type | toString -}}
{{- if eq $t "LoadBalancer" -}}
{{- $lb := (.status | default dict).loadBalancer | default dict -}}
{{- range ($lb.ingress | default list) -}}
{{- $addr := .ip | default .hostname -}}
{{- if $addr -}}{{- $url = printf "http://%s:%v" $addr $eport -}}{{- end -}}
{{- end -}}
{{- else if eq $t "NodePort" -}}
{{- $np := "" -}}
{{- range .spec.ports -}}{{- if .nodePort -}}{{- $np = .nodePort -}}{{- end -}}{{- end -}}
{{- $nip := include "inst.nodeip" (list $top) -}}
{{- if and $nip $np -}}{{- $url = printf "http://%s:%v" $nip $np -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{/* inst.hostname — the external hostname for an exposed component under exposure.type=Gateway.
     args: (list $top $component). Returns exposure.hosts[<component-name>] if set, else the
     derived <svcMatch>.<baseDomain> when baseDomain is set, else "" (route/URL omitted). */}}
{{- define "inst.hostname" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}
{{- $override := index ($top.Values.exposure.hosts | default dict) $c.name -}}
{{- if $override -}}{{- $override -}}
{{- else if $top.Values.exposure.baseDomain -}}{{- printf "%s.%s" ($c.svcMatch | default $c.name) $top.Values.exposure.baseDomain -}}
{{- end -}}
{{- end -}}

{{/* inst.svcname — the EXACT name of the live Service backing a peer, for an HTTPRoute backendRef.
     args: (list $top $svcMatch). Prefers an exact name match, else the first contains-match
     (mirrors inst.peerurl). "" until the Service exists (route emitted next reconcile). Needs the
     services read the chart already grants the cdc SA (installers-lbip-services, self-bootstrap.yaml). */}}
{{- define "inst.svcname" -}}
{{- $top := index . 0 -}}{{- $sub := index . 1 -}}{{- $exact := "" -}}{{- $contains := "" -}}
{{- range (lookup "v1" "Service" $top.Values.namespaces.krateo "").items -}}
{{- if eq .metadata.name $sub -}}{{- $exact = .metadata.name -}}
{{- else if and (not $contains) (contains $sub .metadata.name) -}}{{- $contains = .metadata.name -}}{{- end -}}
{{- end -}}
{{- $exact | default $contains -}}
{{- end -}}

{{/*
inst.componentsYaml — the chart-managed component pin list, sourced from
files/component-pins.yaml (NOT .Values), so it is CHART CONTENT immune to
`helm upgrade --reuse-values`. A chart bump therefore always propagates its component
version/repo/dep pins on upgrade. Callers pass the ROOT context ($) so .Files is available and
parse the result:
    {{- $components := (include "inst.componentsYaml" $ | fromYaml).components -}}

PERMIT UPDATE — per-component `version` override from the Installer CR:
Each component's `version` may be overridden by the Installer CR's spec.components[].version
(= .Values.components here), matched by name. This lets an operator bump a live component version
by patching the Installer CR — the override flows through BOTH Pass A (definitions.yaml ->
CompositionDefinition spec.chart.version) AND Pass B (compositions.yaml -> the instance apiVersion
via inst.apiVersion), so the CD and its composition instance move in LOCKSTEP (no served-version
skew). When the CR does not set a component's version, the baked file version is used — so a chart
bump still propagates its pins and the set stays immune to `--reuse-values`. Only `version` is
overridable; all other pin fields (kind/chart/deps/tier/feature/exposure) remain chart content.
*/}}
{{- define "inst.componentsYaml" -}}
{{- $file := .Files.Get "files/component-pins.yaml" | fromYaml -}}
{{- $components := ($file.components | default list) -}}
{{/* Index the chart-pinned component names. */}}
{{- $known := dict -}}
{{- range $c := $components -}}{{- $_ := set $known (toString $c.name) true -}}{{- end -}}
{{/* RETIRED-NAMES allowlist (files/retired-components.yaml, installer#144 Landmine 1):
     a live Installer CR ships its FULL spec.components list wholesale on every upgrade
     (self-bootstrap.yaml), so a name whose pin was DELETED when its feature was retired
     still arrives here via .Values.components for at least one more reconcile. Such a
     name is neither pinned nor a legitimate append — index it so the append branch below
     can SKIP it (not fatal, not registered) while the typo-guard stays intact for every
     truly-unknown name. Future retirements just append the name to that file. */}}
{{- $retiredFile := .Files.Get "files/retired-components.yaml" | fromYaml -}}
{{- $retired := dict -}}
{{- range $n := ($retiredFile.retired | default list) -}}{{- $_ := set $retired (toString $n) true -}}{{- end -}}
{{/* .Values.components entries do one of two things, matched by name:
       - name IS a pinned component  -> override the whitelisted fields version / registerOnly / tier
         on that live component. `version` bumps its chart; `registerOnly: true` demotes it to
         registered-but-not-installed (Pass A registers the CD, Pass B suppresses the Composition) —
         the way a nested/multi-tenant child opts OUT of a pinned component it should not run (e.g.
         otel-collector-daemonset, whose node-level telemetry + hostPorts don't belong in a child on
         shared nodes; installer#38). `tier` re-groups it (e.g. -> catalog). `repo`/`chart` re-source it
         (see below). kind/deps stay NOT overridable (they'd break resolution). Footgun: do NOT registerOnly a component that
         others `deps` on — its dependents then never see it Ready and stall.
       `repo` (and `chart`, the artifact name in the URL) re-SOURCE the component (installer#84: let a
       user pull a component from a different registry — e.g. Docker Hub vs ghcr — so the source need not
       be equal for all). Safe to override because they change only WHERE/WHAT-PATH the chart is pulled
       from, not the component's identity (kind/deps stay; the CompositionDefinition/Composition name stays
       the component name), and `repo` threads through everywhere the effective repo is read: the CD chart
       URL (definitions.yaml), the chart-pull credentials (inst.chartExtras matches registries[] by repo),
       and the derived image-pull secret (inst.imagePullAuths). A PRIVATE override source needs a matching
       registryAuth.registries[] entry (repo == the override) for its pull credential; a public one needs
       nothing. IMPORTANT: use registries[] mode for a re-source to a DIFFERENT registry — in GLOBAL mode
       (registries[] empty, enabled) the single credential is presented to EVERY pull, so it would hand
       your ociRepo token to the override registry (fine when both are the same host with a cross-org
       token, e.g. ghcr; wrong for a genuinely different registry). `kind`/`deps` stay NOT overridable
       (those WOULD break resolution/identity — the CRD kind is crdgen'd from the chart at build time, so
       the override target must host the SAME chart+version the pin names, or Pass B never sees it Ready).
       - name is NOT pinned          -> APPEND it as a new component. This is how a CMP (tier-b)
         adds catalog blueprints (registerOnly + tier: catalog, e.g. openstack) via values while
         the base installer stays use-case-agnostic — it ships no such blueprint by default. */}}
{{- $ov := dict -}}
{{- $extra := list -}}
{{- range $o := (.Values.components | default list) -}}
{{- if and (kindIs "map" $o) (hasKey $o "name") -}}
{{- if hasKey $known (toString $o.name) -}}
{{/* namespace/readerGroups place a blueprint's CompositionDefinition in its own namespace; they are
     for APPENDED registerOnly catalog blueprints only (definitions.yaml). A pinned component's
     definition stays in namespaces.krateo, so refuse rather than silently ignore them. */}}
{{- if or (hasKey $o "namespace") (hasKey $o "readerGroups") -}}{{- fail (printf "components[%s]: `namespace`/`readerGroups` apply only to appended registerOnly catalog blueprints, not to a pinned component" (toString $o.name)) -}}{{- end -}}
{{- $fields := dict -}}
{{- range $k := (list "version" "registerOnly" "tier" "repo" "chart") -}}
{{- if hasKey $o $k -}}{{- $_ := set $fields $k (index $o $k) -}}{{- end -}}
{{- end -}}
{{- $_ := set $ov (toString $o.name) $fields -}}
{{- else if hasKey $retired (toString $o.name) -}}
{{/* RETIRED name (files/retired-components.yaml): its pin was deleted when its feature was
     retired, but the live CR's wholesale spec.components still carries it. SKIP it — do not
     append it (no dead CompositionDefinition) and do not fail (that would abort the whole
     umbrella render on every existing install, installer#144 Landmine 1). Deliberately NOT
     an `else` fallthrough: a retired name must be an explicit allowlist entry, never silence
     for any unpinned name. */}}
{{- else -}}
{{/* Unknown name = a CMP-appended catalog blueprint. Guard the footgun: a typo'd known-component
     name (e.g. `snowplowe`) would otherwise silently register a dead CompositionDefinition whose
     chart URL resolves to nothing. Only allow the append when the entry explicitly opts in as a
     registerOnly catalog blueprint — anything else is a mistake, so fail loudly at render time.
     (Retired names are handled by the branch above; a name that is neither pinned nor retired
     still fails here, preserving the typo-guard.) */}}
{{- if not $o.registerOnly -}}{{- fail (printf "components[%s]: unknown name not in files/component-pins.yaml — only registerOnly catalog blueprints may be appended via .Values.components (typo?)" (toString $o.name)) -}}{{- end -}}
{{/* An APPEND registers a brand-new CD, so it needs kind + version (the schema now requires only
     `name`, to let overrides of PINNED components omit them — installer#38). Guard here so an
     append missing them fails loudly instead of registering a broken CompositionDefinition. */}}
{{- if or (not (hasKey $o "kind")) (not (hasKey $o "version")) -}}{{- fail (printf "components[%s]: an appended catalog blueprint needs `kind` and `version` (only overrides of PINNED components may omit them)" (toString $o.name)) -}}{{- end -}}
{{- $extra = append $extra $o -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $c := $components -}}
{{- if hasKey $ov (toString $c.name) -}}
{{- range $k, $v := (index $ov (toString $c.name)) -}}
{{- $_ := set $c $k $v -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{ dict "components" (concat $components $extra) | toYaml }}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.jwtSigningKeyPEM — resolve the authn RS256 JWT signing key.

     TAKES THE LOOKED-UP SECRET AS A PARAMETER AND DOES NOT CALL `lookup` ITSELF. That is the
     whole point of it existing: `helm template` DISABLES lookup, so while the reuse/generate
     decision sat inline in secret.yaml next to its own `lookup` call, only ONE of its two arms
     could ever be rendered — the generate arm, because lookup always came back empty. The arm
     that matters could not be exercised without a cluster, and the arm that runs on a cluster
     was the untested one.

     WHY THE UNTESTED ARM IS THE DANGEROUS ONE. This key signs every JWT authn issues. If the
     reuse arm ever fails to fire — a lookup that returns empty on a cluster where the Secret
     exists, a renamed data key, a nil-map guard that stops matching — the else-arm generates a
     NEW key, helm applies it, authn restarts with it, and EVERY OUTSTANDING TOKEN BECOMES
     INVALID. There is no error anywhere: the render succeeds, the Secret applies cleanly, and
     the only symptom is that every signed-in user is logged out at once and no log says why.
     Rotation is indistinguishable from correct operation in every signal the chart produces.

     So the decision is split out here where .github/scripts/check-signing-key.py can hand it
     both a found Secret and nothing at all, assert the two differ, and assert the found case
     returns THAT key rather than a fresh one. secret.yaml keeps the `lookup` and passes its
     result in; the gate asserts that wiring textually, since it cannot execute it.
     --------------------------------------------------------------------------------------- */}}
{{- define "inst.jwtSigningKeyPEM" -}}
{{- $found := index . 0 -}}
{{- if and $found $found.data (hasKey $found.data "private.pem") -}}
{{- index $found.data "private.pem" | b64dec -}}
{{- else -}}
{{- genPrivateKey "rsa" -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.authnSigningKeySecretName / inst.authnSigningKeyKid — the NAME and the KID of authn's
     JWT signing key, both derived from ONE integer: authn.jwt.keyGeneration.

     WHY A GENERATION COUNTER AND NOT A NAME. authn has no per-token revocation, so the only
     lever against a leaked token is to stop serving the key that signed it. That means a NEW
     key, which means a new Secret, which means a new kid — because a validator caches JWKS by
     kid, and reusing the kid for different key material is the one combination that fails
     silently in both directions (a stale cache accepts tokens signed by the dead key; a fresh
     cache rejects tokens the dead key signed, with no way to tell which happened).

     Deriving BOTH from one counter is the point. Three facts have to agree — the Secret the
     umbrella creates, the Secret name authn mounts, and the kid authn advertises. Exposed as
     three values they can disagree, and every disagreement is quiet: mount the wrong Secret and
     authn signs with a key whose public half is not in its own JWKS, so every token it issues
     is rejected by every verifier, while authn itself logs a clean boot.

     GENERATION 1 IS THE IDENTITY CASE, deliberately. It returns authn's own chart defaults
     (`authn-jwt-signing-key`, `krateo-authn-key-1`, read from authn 0.40.0 values.yaml), so
     every existing cluster renders byte-identical objects and no upgrade rotates anything. A
     rotation is only ever an explicit bump of the counter.

     NEITHER CALLS `lookup`, for the same reason inst.jwtSigningKeyPEM does not: `helm template`
     disables lookup, so anything downstream of it is unreachable in CI. These are pure
     functions of the counter and .github/scripts/check-signing-key.py renders them directly.
     --------------------------------------------------------------------------------------- */}}
{{- define "inst.authnSigningKeySecretName" -}}
{{- $gen := index . 0 | default 1 | int -}}
{{- if le $gen 1 -}}authn-jwt-signing-key{{- else -}}authn-jwt-signing-key-{{ $gen }}{{- end -}}
{{- end -}}

{{- define "inst.authnSigningKeyKid" -}}
{{- $gen := index . 0 | default 1 | int -}}
{{- if le $gen 1 -}}krateo-authn-key-1{{- else -}}krateo-authn-key-{{ $gen }}{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.unmetApiGroups — the API groups a component DECLARES and the cluster does not serve.
     Returns a comma-joined list, or "" when every declared group is present.

     inst.crdExists answers "is this component's own CRD served"; inst.requiresApiGroups answers
     "true"/"" for the gate. Neither can say WHICH group is missing, and the gate's whole failure
     mode is that it emits nothing and explains nothing. This names them.
     --------------------------------------------------------------------------------------- */}}
{{- define "inst.unmetApiGroups" -}}
{{- $want := index . 0 -}}{{- $allCRDs := index . 1 -}}
{{- $missing := list -}}
{{- range $g := ($want | default list) -}}
{{- $found := "" -}}
{{- range $allCRDs -}}{{- if eq .spec.group $g -}}{{- $found = "true" -}}{{- end -}}{{- end -}}
{{- if not $found -}}{{- $missing = append $missing $g -}}{{- end -}}
{{- end -}}
{{- $missing | sortAlpha | join "," -}}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.withheldReason — why a DECLARED component is not being emitted, in words, on the
     component the operator actually named.

     THE DEFECT THIS EXISTS FOR (#614). krateo-observability declares no unsatisfied API group of
     its own. It is withheld because clickhouse-operator is, and clickhouse-operator's declaration
     is the only place the real condition is recorded — a component the operator had no reason to
     look at. Every CompositionDefinition in the chain reports True/True, the composition CRs are
     simply never created, and there is no error anywhere. An operator who enables the
     observability tier without features.ingress gets an install that looks entirely healthy and
     emits nothing.

     Measured as a REGRESSION, not a standing gap: installer 0.3.477 has no requiresApiGroups at
     all and installed the whole tier on a fresh cluster with no cert-manager; 0.3.478 introduced
     them (#594) and a fresh install stops at 33 of 40 declared CRs. The snowplow bench bracketed
     it from outside on 0.3.468 (40/40) and 0.3.506 (33/40).

     Reasons are ordered by what the gate checks first, so the reported reason is the one that
     actually blocks. The transitive arm is bounded rather than recursive because helm templates
     cannot recurse; 6 passes covers this chart's deepest chain (operator -> observability ->
     collector-deployment -> collector-daemonset) with room to spare, and a chain deeper than that
     reports the direct cause rather than silently reporting none.
     --------------------------------------------------------------------------------------- */}}
{{- define "inst.withheldReason" -}}
{{- $top := index . 0 -}}{{- $c := index . 1 -}}{{- $components := index . 2 -}}{{- $allCRDs := index . 3 -}}
{{- $own := include "inst.unmetApiGroups" (list $c.requiresApiGroups $allCRDs) -}}
{{- if $own -}}
{{- printf "withheld: this component requires API group(s) %s, which the cluster does not serve" $own -}}
{{- else -}}
{{/* build the set of components whose OWN declared groups are unmet */}}
{{- $unmet := dict -}}
{{- range $x := $components -}}
{{- if $x.requiresApiGroups -}}
{{- $m := include "inst.unmetApiGroups" (list $x.requiresApiGroups $allCRDs) -}}
{{- if $m -}}{{- $_ := set $unmet $x.name $m -}}{{- end -}}
{{- end -}}
{{- end -}}
{{/* bounded transitive walk over deps */}}
{{- $seen := dict -}}
{{- $frontier := ($c.deps | default list) -}}
{{- range $pass := until 6 -}}
{{- $next := list -}}
{{- range $d := $frontier -}}
{{- if not (hasKey $seen $d) -}}
{{- $_ := set $seen $d true -}}
{{- range $x := $components -}}{{- if eq $x.name $d -}}{{- $next = concat $next ($x.deps | default list) -}}{{- end -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $frontier = $next -}}
{{- end -}}
{{- $blockers := list -}}
{{- range $d, $_v := $seen -}}
{{- if hasKey $unmet $d -}}{{- $blockers = append $blockers (printf "%s needs %s" $d (index $unmet $d)) -}}{{- end -}}
{{- end -}}
{{- if $blockers -}}
{{- printf "withheld: a dependency requires an API group the cluster does not serve -- %s" ($blockers | sortAlpha | join "; ") -}}
{{- else -}}
{{/* Read the SAME resolver inst.emitDecision read, not a separately derived answer to a similar
     question (#477 follow-on). A crdExists-at-the-pin check here names a reason the decision did
     not act on, because the decision now accepts an older served tag. */}}
{{- $etag := include "inst.emitTag" (list $top $c.kind $c.name $c.version $allCRDs) -}}
{{/* Ask existence the way inst.emitDecision asks it -- version-agnostically. A reason derived from
     a different question than the decision acted on is how this reporter came to contradict the
     gate once already. */}}
{{- $any := include "inst.compositionExistsAny" (list $top $c.kind $c.name $allCRDs) -}}
{{- if not $etag -}}
{{- printf "withheld: no CRD serves kind %s at version %s and no live instance exists at any served version to keep alive (normal before first creation; persistent means crdgen rejected the CRD)" $c.kind (toString $c.version) -}}
{{- else if and ($c.deps | default list) (ne $any "true") -}}
{{- printf "withheld: dependencies not Ready yet and no live instance to keep alive -- %s" (($c.deps | default list) | sortAlpha | join ",") -}}
{{- else if ($c.deps | default list) -}}
{{/* A live instance EXISTS and the CRD serves a version, so inst.emitDecision would have emitted
     it on the $exists escape. If this component is nonetheless withheld, the gate and this
     reporter disagree and that must be said, not papered over with the deps message. */}}
{{- printf "withheld: NO REASON FOUND -- a live instance of %s/%s exists at a served version and the $exists escape should have emitted it, so the emission gate and this reporter disagree. That is a bug in one of them; please report it." $c.kind $c.name -}}
{{- else -}}
{{/* No deps, CRD served, no unmet groups -- the gate's own terms are all satisfied, so this
     component should have been emitted. Reaching here means the gate and this reporter disagree,
     which is a defect in one of them and must not print as a reason. An empty dep list rendered
     into "dependencies not Ready yet -- " is the shape that would hide it. */}}
{{- printf "withheld: NO REASON FOUND -- this component has no dependencies, its CRD is served and its declared API groups are present, so the emission gate and this reporter disagree. That is a bug in one of them; please report it." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.servedTags — the apiVersion tags a Kind's generated CRD actually SERVES, space-joined.

     inst.crdExists answers "is THIS version served" and cannot say what else is. This can, which
     is what makes it possible to keep a live composition alive at a version the cluster still
     answers on while a bump is in flight.
     --------------------------------------------------------------------------------------- */}}
{{- define "inst.servedTags" -}}
{{- $kind := index . 0 -}}{{- $allCRDs := index . 1 -}}
{{- $tags := list -}}
{{- range $allCRDs -}}
{{- if and (eq .spec.group "composition.krateo.io") (eq .spec.names.kind $kind) -}}
{{- range .spec.versions -}}{{- if .served -}}{{- $tags = append $tags .name -}}{{- end -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $tags | join " " -}}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------
     inst.emitTag — THE single resolution of "which apiVersion is this component emitted at".

     Returns an apiVersion tag (e.g. `v1-8-90`), or "" when nothing should be emitted. The gate,
     the existence check and the emitted `apiVersion:` all call this, because deriving that fact at
     more than one call site is exactly how they come to disagree -- the defect this fixes.

     THE DEFECT (installer#477). The gate used to AND `inst.crdExists $c.version` OUTSIDE the
     `$exists` escape. At the instant a component's pin bumps, the generated CRD does not yet serve
     the new version -- the bumped CompositionDefinition is what causes crdgen to create it -- so
     the conjunct failed, `$exists` was never consulted, the composition vanished from the manifest
     and helm PRUNED the live CR. The escape added on 2026-07-08 to stop exactly that could not
     help, because it sat inside the conjunct that failed.

     Measured, not theorised: the identical construction in krateo-blueprints' openstack umbrella
     pruned 34 live composition CRs on 2026-11-04, leaving 27 helm releases with nothing driving
     them. Their escape was MORE permissive than ours -- version-agnostic -- and it made no
     difference, because the outer conjunct fails first. And core-provider measured their half:
     ApplyOrUpdateCRD waited on `crd.IsReady`, which checks only `Established` -- already true from
     the PREVIOUS version and never false across an update -- so nothing had ever established that
     the new version was servable. Their #343 bounds that window; it cannot order their return
     against this template's next render.

     THE RESOLUTION, in order:
       1. the target version is served            -> the target. Steady state, and the normal case
                                                     once crdgen has caught up.
       2. the target is NOT served, but the live instance exists at a version that IS
                                                  -> THAT version. The bump instant: keep rendering
                                                     the object that already exists, at a version the
                                                     cluster still serves. Two separate reasons, and
                                                     they are worth keeping apart because they fail
                                                     differently:
                                                       PRESENT rather than absent -- this is what
                                                         stops the prune. Helm prunes what leaves the
                                                         manifest; the defect above is an ABSENCE,
                                                         not a version mismatch.
                                                       at a SERVED version -- this is what stops the
                                                         apply being REJECTED. Emitting an unserved
                                                         apiVersion is a different failure from being
                                                         pruned, and reads nothing like it.
                                                     Deliberately NOT claimed here: that helm would
                                                     treat the same object at a new apiVersion as a
                                                     delete-and-create. openstack-as-a-service
                                                     measured the opposite on k3s (helm does not match
                                                     on version) and core-provider confirmed it in
                                                     plumbing's Reconcile; this session has not
                                                     reproduced either, so the reasoning above is
                                                     written to hold whichever way that falls.
                                                     It migrates on a later pass, once (1) becomes
                                                     true.
       3. neither                                 -> "". First creation before the CRD is servable,
                                                     or a CRD crdgen rejected. Withholding is
                                                     correct: there is nothing to preserve and no
                                                     version to emit at.

     Case 2 is the whole fix. It is deliberately conditioned on the instance EXISTING: without that
     it would emit a brand-new object at a stale version, which is a different defect.
     --------------------------------------------------------------------------------------- */}}
{{/* inst.emitTagFrom — the DECISION, as a pure function of facts already gathered.

     Split out from inst.emitTag for the same reason inst.jwtSigningKeyPEM was split from
     secret.yaml: the resolution's interesting arm depends on a `lookup`, and `helm template`
     DISABLES lookup -- so while the decision lived beside its own lookup, the only arm a test
     could reach was the one that does not matter. Taking the lookup's RESULT as an argument makes
     every arm reachable with no cluster and no fake-cluster plugin.

     Arguments: the wanted tag, the space-joined served tags, and the tag at which the live
     instance was found ("" if it was not found, or if lookup was unavailable). */}}
{{- define "inst.emitTagFrom" -}}
{{- $want := index . 0 -}}{{- $servedStr := index . 1 -}}{{- $existingTag := index . 2 -}}
{{- $served := splitList " " ($servedStr | toString) -}}
{{- if has $want $served -}}{{- $want -}}
{{- else if and $existingTag (has $existingTag $served) -}}{{- $existingTag -}}
{{- end -}}
{{- end -}}

{{- define "inst.emitTag" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $allCRDs := index . 4 -}}
{{- $want := printf "v%s" ($ver | toString | replace "." "-") -}}
{{- $servedStr := include "inst.servedTags" (list $kind $allCRDs) -}}
{{/* Find the tag the live instance is actually served at, if any. Only consulted when the wanted
     tag is not served -- that is the bump instant -- so the common path costs no extra lookup. */}}
{{- $existing := "" -}}
{{- if not (has $want (splitList " " $servedStr)) -}}
{{- range $t := splitList " " $servedStr -}}
{{- if and (not $existing) $t -}}
{{- if lookup (printf "composition.krateo.io/%s" $t) $kind $top.Values.namespaces.krateo $name -}}
{{- $existing = $t -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- include "inst.emitTagFrom" (list $want $servedStr $existing) -}}
{{- end -}}

{{/* inst.compositionExistsAny — does the live instance exist at ANY SERVED tag?
     args: (list $ "Kind" "name" $allCRDs)

     This is the right shape for every EXISTENCE question, and the wrong one for the emitted
     apiVersion. The two must not share a helper, which is the mistake this replaces.

     inst.emitTag PREFERS THE TARGET, correctly: once crdgen serves the new version that is what a
     composition should be emitted at, so the cdc performs the origin-version migration. But asking
     "does this still exist?" at the tag we would EMIT it at is a DIFFERENT question, and it is
     wrong in a cell the first #477 fix did not cover: when the CRD serves the old AND the new
     version and the instance has not migrated yet, inst.emitTag returns the new tag, a lookup
     there MISSES the instance living at the old one, and the caller concludes it is gone. For
     inst.dependentsGone that deletes a draining component WITH A DEPENDENT STILL PRESENT; for
     inst.compositionExists it deregisters a CompositionDefinition under a live CR; for the gate's
     own escape it prunes the instance it exists to protect.

     krateo-blueprints' openstack umbrella uses a version-agnostic escape and said so; this is why.
     An existence check must be permissive, because its job is to STOP a deletion. */}}
{{- define "inst.compositionExistsAny" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $allCRDs := index . 3 -}}
{{- $found := "" -}}
{{- range $t := splitList " " (include "inst.servedTags" (list $kind $allCRDs)) -}}
{{- if and (not $found) $t -}}
{{- if lookup (printf "composition.krateo.io/%s" $t) $kind $top.Values.namespaces.krateo $name -}}{{- $found = "true" -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $found -}}
{{- end -}}


{{/* ---------------------------------------------------------------------------------------
     inst.componentValuesPatch — componentValues for the CR merge patch, with explicit nulls
     for every path named in .Values.componentValuesUnset.

     WHY THIS EXISTS. components-patch.json is applied as an RFC-7386 merge patch, where an
     OMITTED key is PRESERVED and only an explicit null deletes. That is deliberate — it is what
     stops a chart upgrade clobbering operator-set values (see self-bootstrap.yaml) — but it also
     means a componentValues key could be SET and CHANGED and never REMOVED. Deleting it from the
     values file was silently inert: helm reported the new values while the CR kept the old ones.

     Measured on krateo-057, 2026-11-07: after removing componentValues.authn.env entirely and
     rolling 0.3.519, the live CR still held {"env":{"AUTHN_DEBUG":"false","AUTHN_DUMP_ENV":"false"}}
     and the rendered ConfigMap still carried AUTHN_DUMP_ENV. A sweep then found NINE components
     whose live componentValues were explained by neither the values file nor a schema default —
     so the CR was no longer reconstructible from the repo.

     An explicit null in the VALUES cannot express this: the schema types these fields (string,
     object), so helm refuses a null before the patch is ever built. Hence a separate, additive
     key whose only job is removal — it loosens no existing type, and it makes a removal a
     DECLARED act that a gate can check rather than an impossible one.

     Paths are dotted and relative to componentValues:
         componentValuesUnset:
           - authn.env.AUTHN_DUMP_ENV      removes that one key
           - authn.env                     removes the whole object
     A path whose parent is absent is a no-op, not an error: re-running an unset after it has
     taken effect must stay safe, or the values file becomes un-rerunnable.
*/}}
{{- define "inst.componentValuesPatch" -}}
{{- /* Builds components-patch.json's componentValues, turning every componentValuesUnset path into
       an EXPLICIT NULL so the RFC-7386 merge patch DELETES it. An omitted key is PRESERVED by that
       patch, and the schema types these fields so a literal null cannot be written in values — so
       without this, a componentValues key can be set and changed and never removed.

       IT CREATES THE INTERMEDIATE MAPS RATHER THAN REQUIRING THEM. The first version walked the
       path and emitted a null only if every parent was ALREADY PRESENT in .Values.componentValues.
       That made the feature useless in the ONLY sequence that occurs: an operator removes the key
       from the values file, discovers on the next roll that the live CR still carries it (because
       omission preserves), and THEN adds the unset — by which point the parent is gone from values,
       the walk fails, and no null is emitted. MEASURED on krateo-057 at 0.3.524:
       componentValuesUnset: [authn.env] with componentValues.authn absent from the values produced
       NO null, and the CR kept {"env":{"AUTHN_DEBUG":"false","AUTHN_DUMP_ENV":"false"}}.

       Deleting an already-absent key is a no-op under RFC-7386, so emitting the null
       unconditionally stays idempotent — re-running a values file after the unset has taken effect
       remains safe, which was the property the existence guard was protecting.

       THE FIRST SEGMENT IS TYPO-GUARDED, and it has to be. Creating intermediates means a typo
       would mint a componentValues entry for a component that does not exist — and
       componentValues is `additionalProperties: false` and typed per component in the Installer
       CRD, so the API server would REJECT THE WHOLE PATCH. One mistyped unset would therefore
       break every componentValues update rather than just its own. So an unknown component name
       fails the render loudly, exactly as an unknown name does in inst.componentsYaml. */}}
{{- $top := . -}}
{{- $cv := deepCopy (default dict $top.Values.componentValues) -}}
{{- $known := dict -}}
{{- range $c := (include "inst.componentsYaml" $top | fromYaml).components -}}
{{- $_ := set $known (toString $c.name) true -}}
{{- end -}}
{{- range $path := (default list $top.Values.componentValuesUnset) -}}
{{- $segs := splitList "." $path -}}
{{- if lt (len $segs) 2 -}}
{{- fail (printf "componentValuesUnset[%s]: a path needs at least <component>.<key>; unsetting a whole component is not supported, because componentValues is required per component" $path) -}}
{{- end -}}
{{- $comp := first $segs -}}
{{- if not (hasKey $known $comp) -}}
{{- fail (printf "componentValuesUnset[%s]: '%s' is not a known component. Creating its entry would mint a componentValues key the Installer CRD rejects (additionalProperties: false), breaking EVERY componentValues update, not just this one. Check the spelling against files/component-pins.yaml." $path $comp) -}}
{{- end -}}
{{- $leaf := last $segs -}}
{{- $cur := $cv -}}
{{- range $seg := (initial $segs) -}}
{{- if not (and (kindIs "map" $cur) (hasKey $cur $seg) (kindIs "map" (index $cur $seg))) -}}
{{- $_ := set $cur $seg dict -}}
{{- end -}}
{{- $cur = index $cur $seg -}}
{{- end -}}
{{- $_ := set $cur $leaf nil -}}
{{- end -}}
{{- $cv | toJson -}}
{{- end -}}

{{/*
inst.subAgents — DERIVE the agentgateway's A2A delegation-target list from the pinned components.

Args: (list $top $components)
Returns: a YAML list of {name, port, subjects}, or "" when nothing qualifies.

EXTRACTED FROM compositions.yaml SO IT CAN BE TESTED AT ALL. The derivation needs no `lookup` —
only $components, $top.Values.componentValues and inst.featureEnabled — but it used to sit inline in
compositions.yaml, and THAT FILE IS lookup-GATED (line 37 reads the live CRD list). `helm template`
disables lookup, so the file renders ZERO composition objects and the derivation could not be
exercised locally in any way. As a named helper it is reachable from a probe chart by plain render.

The orchestrator's own route name comes from the pin's `agentName`, defaulting to "autopilot" so
every pin that does not set it renders byte-identically. With TWO orchestrators pinned side by side
(krateo-autopilot frozen at 0.7.3 and agentiko 0.8.0 during the rename) a literal emitted the SAME
name twice: one duplicate entry and NO route for the second agent.

A non-orchestrator agent is routed by its chart name, falling back to the component name.
*/}}
{{- define "inst.subAgents" -}}
{{- $top := index . 0 -}}
{{- $components := index . 1 -}}
{{- $subAgents := list -}}
{{- range $comp := $components }}
{{- if and $comp.agent (eq (include "inst.featureEnabled" (list $top $comp.feature)) "true") }}
{{- if $comp.orchestrator }}
{{- $ap := dig $comp.name dict (default dict $top.Values.componentValues) }}
{{- $subAgents = append $subAgents (dict "name" (default "autopilot" $comp.agentName) "port" 8080 "subjects" (dict "anyAuthenticated" true)) }}
{{- /* A sub-agent's route is contributed ONLY by the orchestrator that OWNS its Agent object.
       `owned: false` means another release owns it, so contributing the route here would emit the
       SAME name twice while two orchestrators are pinned side by side — k8s-agent and helm-agent
       each appearing once per orchestrator. `owned` is absent on every pin today and the charts
       default it to true, so `(ne ... false)` keeps the single-orchestrator render identical. */ -}}
{{- if and (dig "agents" "k8s" "enabled" false $ap) (ne (dig "agents" "k8s" "owned" true $ap) false) }}{{- $subAgents = append $subAgents (dict "name" "k8s-agent" "port" 8080 "subjects" (dict "anyAuthenticated" true)) }}{{- end }}
{{- if and (dig "agents" "helm" "enabled" false $ap) (ne (dig "agents" "helm" "owned" true $ap) false) }}{{- $subAgents = append $subAgents (dict "name" "helm-agent" "port" 8080 "subjects" (dict "anyAuthenticated" true)) }}{{- end }}
{{- else }}
{{- $subAgents = append $subAgents (dict "name" (default $comp.name $comp.chart) "port" 8080 "subjects" (dict "anyAuthenticated" true)) }}
{{- end }}
{{- end }}
{{- end }}
{{- if gt (len $subAgents) 0 }}{{- toYaml $subAgents -}}{{- end }}
{{- end -}}
