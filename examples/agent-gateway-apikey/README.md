---
type: Example
title: agent gateway — distribution-agnostic LLM with a Gemini API key (no Vertex, no GCP)
description: Route the whole kagent fleet's LLM traffic through agentgateway and reach Gemini with a plain API key from a Secret — portable auth that runs on kind / EKS / AKS / on-prem, and turns token spend into observable cost telemetry.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [agentgateway, gemini, llm, apikey, kagent, portability, cost, telemetry]
timestamp: 2026-09-01T00:00:00Z
---

# Agent gateway — distribution-agnostic LLM with a Gemini API key

The default agent-fleet profile (`vertexAI.enabled: true`) has each agent pod call Vertex AI
directly through GCP Application Default Credentials. That needs a GCP project and cloud identity,
and — because `google-genai` goes straight to Vertex — it **bypasses the gateway entirely**, so the
gateway can never meter token spend and `gen_ai` cost telemetry never flows.

This example flips the switch. `vertexAI.enabled: false` selects the **API-key path**:

- every agent gets `GOOGLE_GENAI_USE_VERTEXAI=0` and a ModelConfig pointed at the gateway's
  `/llm/v1` route (so no agent calls a provider directly);
- the gateway is the single key-holder — it reaches Gemini with a plain API key from a Secret, and
  its backend's `provider: auto` resolves to Gemini because no `projectID` is present;
- because every LLM call is now on the gateway route, budgets, rate-limits, and `gen_ai` token/cost
  telemetry all work.

No Vertex AI, no GKE Workload Identity, no GCP project. It runs unchanged on kind, EKS, AKS, or
on-prem — the auth is portable.

## Preconditions

- A Krateo install this umbrella owns, with `authn` up (it is the issuer — `features.portal`), whose
  JWKS the gateway validates the caller's token against.
- Credentials for the private `oci://ghcr.io/krateo-agentiko/charts` registry (the agent charts),
  as a Secret in the krateo namespace — see
  [configuration](../../docs/configuration.md#registryauth).
- A **Google AI Studio** API key for `generativelanguage.googleapis.com` — **not** a Vertex/GCP
  service-account key — placed in a Secret (no cloud IAM required):

  ```console
  $ kubectl -n krateo-system create secret generic gemini-api-key \
      --from-literal=apiKey='<your-google-ai-studio-key>'
  ```

`features.agentGateway` installs its own Gateway API CRDs and agentgateway controller, so
`features.ingress` can stay off.

## One command

```console
$ helm upgrade -i installer oci://ghcr.io/krateo-platformops/charts/installer \
    -n krateo-system -f values.yaml
```

(or apply the same keys as a merge patch on the live `Installer` CR). Then let Pass B converge and
restart the controller so it picks up the new model path:

```console
$ kubectl -n krateo-system rollout restart deploy/kagent-controller
```

## What the installer wired

The switch lives in the `Installer` spec; the effect shows up in the injected env and the rendered
ModelConfigs.

```console
# vertexAI OFF → the API-key path is selected
$ kubectl -n krateo-system get installer krateo -o jsonpath='{.spec.vertexAI.enabled}'
false

# every agent is told NOT to call Vertex directly
$ kubectl -n krateo-system get deploy autopilot \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GOOGLE_GENAI_USE_VERTEXAI")].value}'
0

# the fleet's model calls now point at the gateway's LLM route (not a provider URL)
$ kubectl -n krateo-system get modelconfig.kagent.dev gemini-pro \
    -o jsonpath='{.spec.provider}{"  "}{.spec.openAI.baseUrl}{"  "}{.spec.apiKeyPassthrough}'
OpenAI  http://krateo-agent-gateway.krateo-system.svc.cluster.local:8080/llm/v1  true

# the gateway backend resolves to Gemini and holds the only provider key
$ kubectl -n krateo-system get agentgatewaybackend agentgateway-policies-llm \
    -o jsonpath='{.spec.ai.provider}'
{"gemini":{...}}
```

## Verify the LLM route + cost telemetry

The point of routing LLM through the gateway is governance — get a token, port-forward the gateway,
and confirm a model call goes through it and is metered:

```console
$ kubectl -n krateo-system port-forward svc/authn 8082:8082
$ JWT=$(curl -s http://localhost:8082/basic/login \
    -H "Authorization: Basic $(printf '%s' "admin:$(kubectl get secret admin-password -n krateo-system -o jsonpath='{.data.password}' | base64 -d)" | base64)" | jq -r .accessToken)

$ kubectl -n krateo-system port-forward svc/krateo-agent-gateway 8080:8080
$ curl -s localhost:8080/llm/v1/chat/completions -H "Authorization: Bearer $JWT" \
    -H 'content-type: application/json' \
    -d '{"model":"gemini-3.7-flash","messages":[{"role":"user","content":"hello"}]}' \
    | jq -r .choices[0].message.content

# no token → the LLM route is not an open proxy
$ curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/llm/v1/chat/completions \
    -H 'content-type: application/json' -d '{"model":"gemini-3.7-flash","messages":[]}'
401
```

With `tracing.enabled: true` (on in this profile) the gateway exports its spans — carrying the
`gen_ai.*` token attributes on the `/llm/v1` spans — to the ClickStack **daemonset** collector, so
LLM cost becomes queryable in ClickHouse. That is the whole reason to put the fleet's LLM on the
gateway: with the default Vertex-direct path those spans never exist.

## Auth model

`llm.callerAuth.mode: Jwt` means the caller's own authn token authorizes the LLM route (kagent
`apiKeyPassthrough`) — no shared LLM key rides inside any agent. `jwt.mode: Strict` keeps tokenless
requests at `401` on the user-facing routes. `llm.callerAuth.subjects` decides who may spend tokens.

## Rolling back to Vertex

```console
$ helm upgrade installer … --set vertexAI.enabled=true
```

Pass B repoints the agents back to Vertex-direct egress and drops the gateway's LLM route on the
same reconcile. Note this returns to the GCP-only, un-metered path.
