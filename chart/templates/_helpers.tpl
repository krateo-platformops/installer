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

{{/* Optional CompositionDefinition spec.chart extras (registry-level): insecureSkipVerifyTLS
     and credentials, rendered ONLY when set so the chart spec stays minimal on public
     registries. arg: (list $). passwordRef.namespace defaults to the krateo namespace.
     Used by Pass A (definitions.yaml) and the self-bootstrap installer CD. */}}
{{- define "inst.chartExtras" -}}
{{- $top := index . 0 -}}
{{- $lines := list -}}
{{- if $top.Values.registryAuth.insecureSkipVerifyTLS -}}
{{- $lines = append $lines "insecureSkipVerifyTLS: true" -}}
{{- end -}}
{{- if $top.Values.registryAuth.enabled -}}
{{- $ns := $top.Values.registryAuth.passwordRef.namespace | default $top.Values.namespaces.krateo -}}
{{- $lines = append $lines (printf "credentials:\n  username: %s\n  passwordRef:\n    name: %s\n    namespace: %s\n    key: %s" ($top.Values.registryAuth.username | quote) ($top.Values.registryAuth.passwordRef.name | quote) ($ns | quote) ($top.Values.registryAuth.passwordRef.key | quote)) -}}
{{- end -}}
{{- join "\n" $lines -}}
{{- end -}}

{{/* Is a feature flag enabled? args: (list $ "featureName"); empty featureName => true.
     Final feature set: coreProvider (engine marker, gates nothing), coreAgents (base agent layer),
     portal (the non-agent platform: authn/snowplow/frontend/portal + portal-starter + the
     observability stack), oasgenProvider, specialistAgents. Every component gates on its flag. */}}
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
  {{- $kind := "" -}}{{- $ver := "" -}}
  {{- range $c := $comps -}}{{- if eq $c.name $d -}}{{- $kind = $c.kind -}}{{- $ver = $c.version -}}{{- end -}}{{- end -}}
  {{- if ne (include "inst.ready" (list $top $kind $d $ver $allCRDs)) "true" -}}{{- $all = "" -}}{{- end -}}
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
{{- range $c := $comps -}}
  {{- if has $name ($c.deps | default list) -}}
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
{{- define "inst.nodeip" -}}
{{- $top := index . 0 -}}{{- $ext := "" -}}{{- $int := "" -}}
{{- range (lookup "v1" "Node" "" "").items -}}
{{- range (.status | default dict).addresses | default list -}}
{{- if eq .type "ExternalIP" -}}{{- $ext = .address -}}{{- end -}}
{{- if eq .type "InternalIP" -}}{{- $int = .address -}}{{- end -}}
{{- end -}}
{{- end -}}
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
       - name IS a pinned component  -> version override (bump a live component's chart version)
       - name is NOT pinned          -> APPEND it as a new component. This is how a CMP (tier-b)
         adds catalog blueprints (registerOnly + tier: catalog, e.g. openstack) via values while
         the base installer stays use-case-agnostic — it ships no such blueprint by default. */}}
{{- $ov := dict -}}
{{- $extra := list -}}
{{- range $o := (.Values.components | default list) -}}
{{- if and (kindIs "map" $o) (hasKey $o "name") -}}
{{- if hasKey $known (toString $o.name) -}}
{{- if hasKey $o "version" -}}{{- $_ := set $ov (toString $o.name) $o.version -}}{{- end -}}
{{- else -}}
{{/* Unknown name = a CMP-appended catalog blueprint. Guard the footgun: a typo'd known-component
     name (e.g. `snowplowe`) would otherwise silently register a dead CompositionDefinition whose
     chart URL resolves to nothing. Only allow the append when the entry explicitly opts in as a
     registerOnly catalog blueprint — anything else is a mistake, so fail loudly at render time. */}}
{{- if not $o.registerOnly -}}{{- fail (printf "components[%s]: unknown name not in files/component-pins.yaml — only registerOnly catalog blueprints may be appended via .Values.components (typo?)" (toString $o.name)) -}}{{- end -}}
{{- $extra = append $extra $o -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $c := $components -}}
{{- if hasKey $ov (toString $c.name) -}}
{{- $_ := set $c "version" (index $ov (toString $c.name)) -}}
{{- end -}}
{{- end -}}
{{ dict "components" (concat $components $extra) | toYaml }}
{{- end -}}
