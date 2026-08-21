---
type: Example
title: agent gateway — JWT auth and two-tier RBAC for the agent fleet
description: Turn on features.agentGateway and give admins and devs different powers over the same fleet, with what the installer installs and wires and the checks that prove each layer.
resource: oci://ghcr.io/krateo-platformops/charts/installer
tags: [agentgateway, rbac, jwt, authn, kagent, security]
timestamp: 2026-08-20T00:00:00Z
---

# Agent gateway — JWT auth and two-tier RBAC

By default a kagent agent sees one static identity for every caller, and anyone who can reach an
agent can make it run any tool it owns. This example puts the gateway in front of the fleet and
splits it over the two identities a stock Krateo install already seeds — `admin` (group `admins`)
and `cyberjoker` (group `devs`):

| | `admins` | `devs` |
| --- | --- | --- |
| Reach `autopilot`, `clickstack-agent`, `frontend-agent`, `snowplow-agent` | yes | yes |
| Reach `installer-agent`, `core-provider-agent`, `authn-agent` | yes | **403** |
| Cluster/Helm mutating tools (`k8s_apply_manifest`, `helm_upgrade`, …) | yes | **tool is absent** |
| Delegation to the platform-changing agents | yes | **403 on the hop** |

The three denials differ in kind on purpose: Layer 1 is a `403` on the user's own request, Layer 2
is a silent omission from `tools/list` (the agent answers that it has no write tool), Layer 3 is a
`403` the *calling agent* reports inside its answer.

## Preconditions

- A Krateo install this umbrella owns, with `authn` up (it is the issuer — `features.portal`).
- Credentials for the private `oci://ghcr.io/krateo-agentiko/charts` registry (the agent charts),
  as a Secret in the krateo namespace — see
  [configuration](../../docs/configuration.md#registryauth).
- A Vertex AI project for the fleet's models (`vertexAI.projectID`), or `localModel` instead.

Nothing else: `features.agentGateway` installs its own Gateway API CRDs and agentgateway
controller, so `features.ingress` can stay off.

> Enabling this is a **breaking change for existing callers** — a valid token becomes mandatory on
> every agent endpoint. Roll it out knowing that.

## One command

```console
$ helm upgrade -i installer oci://ghcr.io/krateo-platformops/charts/installer \
    -n krateo-system -f values.yaml
```

Then let Pass B converge and restart the controller so it picks up the new auth mode:

```console
$ kubectl -n krateo-system rollout restart deploy/kagent-controller
$ kubectl -n krateo-system get agentgatewaypolicy
$ kubectl -n krateo-system get gateway krateo-agent-gateway
```

## What the installer installed and wired

```console
$ kubectl -n krateo-system get agentgatewaycontroller agentgateway-controller
$ kubectl -n krateo-system get agentgatewaypolicies agentgateway-policies

$ kubectl -n krateo-system get kagent kagent \
    -o jsonpath='{.spec.kagentapp.controller.auth.mode}{"  "}{.spec.kagentapp.proxy.url}'
trusted-proxy  http://krateo-agent-gateway.krateo-system.svc.cluster.local:8080

$ kubectl -n krateo-system get krateoautopilot krateo-autopilot -o jsonpath='{.spec.agentgateway}'
{"enabled":true}

$ kubectl -n krateo-system get agentgatewaypolicies agentgateway-policies -o yaml | yq '.spec.mcpServers'
```

## Verify the three layers

Get a token per tier (authn's `/basic/login`), port-forward the gateway, and go through it:

```console
$ kubectl -n krateo-system port-forward svc/authn 8082:8082
$ JWT_ADMIN=$(curl -s http://localhost:8082/basic/login \
    -H "Authorization: Basic $(printf '%s' "admin:$(kubectl get secret admin-password -n krateo-system -o jsonpath='{.data.password}' | base64 -d)" | base64)" | jq -r .accessToken)
$ JWT_DEV=$(curl -s http://localhost:8082/basic/login \
    -H "Authorization: Basic $(printf '%s' "cyberjoker:$(kubectl get secret cyberjoker-password -n krateo-system -o jsonpath='{.data.password}' | base64 -d)" | base64)" | jq -r .accessToken)
```

```console
$ kubectl -n krateo-system port-forward svc/krateo-agent-gateway 8080:8080
$ for t in "$JWT_ADMIN" "$JWT_DEV"; do
    for a in autopilot installer-agent; do
      printf '%s ' "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $t" \
        localhost:8080/api/a2a/krateo-system/$a/.well-known/agent-card.json)"
    done; echo
  done
200 200      # admin
200 403      # cyberjoker: autopilot yes, installer-agent denied
```

With `jwt.mode: Strict` a request with no token at all is a `401` from the JWT layer. The tool and
delegation layers, and the exact per-user behaviour, are exercised in the policies chart's own example:
`krateo-agentiko/agentgateway-policies`, `examples/two-tier-rbac`.

## Rolling back

```console
$ helm upgrade installer … --set features.agentGateway=false
```

Pass B drops both components and removes the controller injections in the same reconcile, so the
controller rolls back to direct egress. Tool calls fail for the moment between the routes going away
and the new controller pod being ready — it unwinds cleanly, but not atomically.
