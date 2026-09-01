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
     IMAGES (autopilot's repo-mcp-server, core-provider-agent's chart-gate, agentgateway-policies' native-RBAC
     decider — see inst.privateImageComponents). registryAuth.imagePullSecretName
     overrides the default so a caller can point at a pre-existing dockerconfigjson Secret instead. arg: $top */}}
{{- define "inst.imagePullSecretName" -}}
{{- .Values.registryAuth.imagePullSecretName | default "krateo-registry-image-pull" -}}
{{- end -}}

{{/* SINGLE source of truth for the components that ship PRIVATE krateo-agentiko IMAGES and expose an
     imagePullSecrets knob their chart documents as "wired from registryAuth". `path` is the FULL (dot-free)
     location of that knob inside the component spec, INCLUDING the final key: the injection walks all but the
     last segment as nested maps and sets the last segment to the pull-secret list, so a knob at ANY depth works
     (mcpServers.repoSearch.imagePullSecrets, or the decider's nativeRbac.image.pullSecrets). Shared by
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
  - nativeRbac
  - image
  - pullSecrets
- name: agentgateway-policies
  path:
  - certReplayHop
  - image
  - pullSecrets
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
{{- define "inst.ready" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $allCRDs := index . 4 -}}
{{- $r := "" -}}
{{- if eq (include "inst.crdExists" (list $kind $ver $allCRDs)) "true" -}}
{{- $apiv := include "inst.apiVersion" (list $ver) -}}
{{- $o := lookup $apiv $kind $top.Values.namespaces.krateo $name -}}
{{- if $o -}}
{{/* nil-guard: a freshly-created dep CR can exist with .status still nil (controller hasn't
     written it yet). $o.status.conditions would then panic ("nil pointer evaluating
     interface {}.conditions") and chart-inspector returns 500, wedging the umbrella mid-chain.
     Coalesce .status to an empty dict so a status-less dep cleanly reads as not-ready. */}}
{{- $st := $o.status | default dict -}}
{{- range ($st.conditions | default list) -}}
{{- if and (eq .type "Ready") (eq (.status | toString) "True") -}}{{- $r = "true" -}}{{- end -}}
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
    {{- if eq (include "inst.crdExists" (list $c.kind $c.version $allCRDs)) "true" -}}
      {{- if (lookup (include "inst.apiVersion" (list $c.version)) $c.kind $top.Values.namespaces.krateo $c.name) -}}{{- $gone = "" -}}{{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $gone -}}
{{- end -}}

{{/* Does a component's OWN Composition CR currently exist? args: (list $ "Kind" "name" "version")
     Guarded by inst.crdExists (an unserved Kind is a hard error in chart-inspector), like inst.ready.
     Used by Pass A (definitions.yaml) to keep a CompositionDefinition registered while its component
     is still present/draining — so the generated CRD OUTLIVES the Composition it serves and is never
     GC'd out from under a still-draining component (the Pass-A counterpart to inst.dependentsGone). */}}
{{- define "inst.compositionExists" -}}
{{- $top := index . 0 -}}{{- $kind := index . 1 -}}{{- $name := index . 2 -}}{{- $ver := index . 3 -}}{{- $allCRDs := index . 4 -}}
{{- if eq (include "inst.crdExists" (list $kind $ver $allCRDs)) "true" -}}
{{- if (lookup (include "inst.apiVersion" (list $ver)) $kind $top.Values.namespaces.krateo $name) -}}true{{- end -}}
{{- end -}}
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
{{- $fields := dict -}}
{{- range $k := (list "version" "registerOnly" "tier" "repo" "chart") -}}
{{- if hasKey $o $k -}}{{- $_ := set $fields $k (index $o $k) -}}{{- end -}}
{{- end -}}
{{- $_ := set $ov (toString $o.name) $fields -}}
{{- else -}}
{{/* Unknown name = a CMP-appended catalog blueprint. Guard the footgun: a typo'd known-component
     name (e.g. `snowplowe`) would otherwise silently register a dead CompositionDefinition whose
     chart URL resolves to nothing. Only allow the append when the entry explicitly opts in as a
     registerOnly catalog blueprint — anything else is a mistake, so fail loudly at render time. */}}
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
