#!/usr/bin/env python3
"""Is a component's release COMPLETE -- every chart AND every image actually published?

WHY THIS EXISTS. A release workflow can go green and publish nothing. Measured on
krateo-platformops/oasgen-provider 0.83.0 on 2026-11-18: a job-level `if` left
`guard-prerelease-tag` skipped on the tag path, a skipped `needs` dependency skips the
dependent job with no error, and the publishing job never ran. The run showed a tick, the tag
existed, and 0 of 5 artifacts were published. A pin to that version would have failed at
INSTALL time -- on a chart pull, looking like a registry outage rather than a missing version.

So "the workflow is green" and "the artifact is pullable" are different events, and only the
second one licenses a pin. This script asks the second question.

AND THE IMAGE PROBE NEEDS A TOKEN, which is the trap that made this a script rather than a
one-liner. Measured against ghcr.io:

    unauthenticated GET /v2/<repo>/manifests/2.13.110   -> 401
    unauthenticated GET /v2/<repo>/manifests/9.9.9      -> 401     <- SAME

An unauthenticated probe cannot tell an existing tag from an absent one: it answers 401 for
both, and reading 401 as "exists, needs auth" reports every version as published. With an
anonymous pull token the same two requests give 200 and 404. So this script fetches a token
per repository and REFUSES to report anything if its own controls do not discriminate.

AND "PULLABLE BY ME" IS NOT "PULLABLE BY THE CLUSTER". Measured 2026-11-18 on krateo-demo: this
script reported `chart kyverno PRESENT` for oci://ghcr.io/krateo-blueprints/charts/kyverno 3.9.4,
the pin went out in 0.3.682, and the cdc could not install it:

    GET https://ghcr.io/token?scope=repository:krateo-blueprints/charts/kyverno:pull
    response status code 401: unauthorized: authentication required

The package was PRIVATE. `helm show chart` had succeeded here because this machine is logged in to
ghcr, so the probe answered a question nobody asked: "can Diego pull it", not "can a cluster pull
it". Every other krateo-blueprints chart that cluster uses is public, so the pin looked ordinary and
the failure appeared only after the roll, as one CD stuck in Creating.

So presence is probed TWICE: once however this machine is configured, and once ANONYMOUSLY, which
is what a cdc with no registry credentials does. A chart that is present but not anonymously
pullable is reported as NOT PINNABLE, because that is the fact that licenses a pin.

CONTROLS ARE MANDATORY, NOT OPTIONAL. Every run probes:
  - the version under test
  - a KNOWN-PUBLISHED version (--control), which must come back present
  - an impossible version (9.9.9), which must come back absent
If either control misbehaves the exit is 2 and no verdict is printed, because a probe that
cannot detect absence cannot certify presence. That is the same discipline as
hack/roll-snapshot.py refusing to call an incomplete child read clean.
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

REGISTRY = "ghcr.io"
# THREE REGISTRIES ARE IN USE and the component's `repo` key is the authority, not this default.
# Measured 2026-11-18: krateo-keycloak-realm lives in krateo-blueprints, and probing it against the
# default answered ABSENT for 0.6.1, 0.6.2 and 9.9.9 alike -- so the controls caught it and the
# script refused to report, which is the design working. But refusing is not probing: pass --repo
# with whatever the pin's `repo` key says.
DEFAULT_CHART_PREFIX = "oci://ghcr.io/krateo-platformops/charts"
IMAGE_ORG = "krateo-platformops"
IMPOSSIBLE = "9.9.9"


def chart_present(name, version, prefix=DEFAULT_CHART_PREFIX, timeout=90):
    """True if `helm show chart` can read it. helm exits non-zero for an absent version."""
    try:
        r = subprocess.run(
            ["helm", "show", "chart", "%s/%s" % (prefix, name), "--version", version],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None  # unknown, not absent -- the caller must not read this as a verdict


def anon_pullable(path, timeout=30):
    """Can a client with NO credentials get a pull token for this OCI path?

    `path` is the registry path without the host, e.g. "krateo-blueprints/charts/kyverno".
    True  -> the registry issued an anonymous pull token: a credential-less cdc can fetch it.
    False -> it refused (401): the package is private, whatever `helm show chart` says here.
    None  -> the probe itself could not run, which is NOT a verdict.

    This asks the token endpoint rather than the manifest, because the token refusal is the
    discriminator: GHCR answers 401 on a manifest GET for a public-but-absent tag too, so a
    manifest probe cannot separate "private" from "that version does not exist".
    """
    url = ("https://%s/token?scope=repository:%s:pull&service=%s" % (REGISTRY, path, REGISTRY))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return bool((json.load(fh) or {}).get("token"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _token(repo, org=IMAGE_ORG, timeout=30):
    url = ("https://%s/token?scope=repository:%s/%s:pull&service=%s"
           % (REGISTRY, org, repo, REGISTRY))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return (json.load(fh) or {}).get("token")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def image_present(repo, tag, token, org=IMAGE_ORG, timeout=30):
    """True/False from a TOKENED manifest HEAD. None if the probe itself could not run.

    Without the token this returns 401 for present and absent alike -- see the module
    docstring. The Accept header must name the index/list types or a multi-arch tag can
    answer 404 for a manifest it does have in another form.
    """
    if not token:
        return None
    url = "https://%s/v2/%s/%s/manifests/%s" % (REGISTRY, org, repo, tag)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return fh.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None  # 401/403/5xx -- the probe failed, which is NOT absence
    except (urllib.error.URLError, OSError):
        return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="the version under test")
    ap.add_argument("--control", required=True,
                    help="a KNOWN-PUBLISHED version, to prove the probe can see presence")
    ap.add_argument("--chart", action="append", default=[], help="chart name; repeatable")
    ap.add_argument("--image", action="append", default=[], help="image repo name; repeatable")
    ap.add_argument("--repo", default=DEFAULT_CHART_PREFIX,
                    help="OCI chart prefix; use the pin's own `repo` key (default: %s)"
                         % DEFAULT_CHART_PREFIX)
    ap.add_argument("--image-org", default=IMAGE_ORG,
                    help="ghcr org for images (default: %s)" % IMAGE_ORG)
    a = ap.parse_args(argv)

    if not a.chart and not a.image:
        print("release-complete: nothing to probe -- pass at least one --chart or --image")
        return 2

    rows, broken, private, unknown_vis = [], [], [], []

    for name in a.chart:
        # "pullable by me" and "pullable by a cluster" are different facts -- see the docstring.
        path = "%s/%s" % (a.repo.split("://", 1)[-1].split("/", 1)[-1], name)
        anon = anon_pullable(path)
        if anon is False:
            private.append((name, path))
        elif anon is None:
            print("  note: could not run the anonymous visibility probe for %s -- treating its "
                  "visibility as UNKNOWN, not public" % name)
            unknown_vis.append(name)
        under = chart_present(name, a.version, a.repo)
        ctl_ok = chart_present(name, a.control, a.repo)
        ctl_no = chart_present(name, IMPOSSIBLE, a.repo)
        if ctl_ok is not True or ctl_no is not False:
            broken.append("chart %s at %s: control %s=%r and %s=%r -- the probe does not "
                          "discriminate. The component's `repo` key is the authority; three "
                          "registries are in use (krateo-platformops, krateo-blueprints, "
                          "krateo-agentiko), so check --repo before reading this as absence."
                          % (name, a.repo, a.control, ctl_ok, IMPOSSIBLE, ctl_no))
        rows.append(("chart", name, under))

    for repo in a.image:
        tok = _token(repo, a.image_org)
        under = image_present(repo, a.version, tok, a.image_org)
        ctl_ok = image_present(repo, a.control, tok, a.image_org)
        ctl_no = image_present(repo, IMPOSSIBLE, tok, a.image_org)
        if ctl_ok is not True or ctl_no is not False:
            broken.append("image %s: control %s=%r and %s=%r -- the probe does not discriminate "
                          "(an UNTOKENED probe answers 401 for both, which is the trap this guards)"
                          % (repo, a.control, ctl_ok, IMPOSSIBLE, ctl_no))
        rows.append(("image", repo, under))

    if broken:
        print("::error::release-complete: REFUSING to report. The probe failed its own controls, "
              "so it can neither certify presence nor absence:")
        for b in broken:
            print("  %s" % b)
        return 2

    if private:
        print("::error::release-complete: %s is PUBLISHED BUT NOT PINNABLE. The registry refuses an "
              "anonymous pull token, so a cdc with no registry credentials CANNOT fetch it -- even "
              "though a probe from this machine succeeds, because this machine is logged in:"
              % a.version)
        for name, path in private:
            print("  chart %s: %s is PRIVATE" % (name, path))
        print("Do NOT pin it. Measured consequence: the pin installs, the CompositionDefinition "
              "sticks in Ready=False/Creating with a 401 in its Synced message, and nothing else "
              "reports a problem. Make the package public first, then re-run this.")
        return 1

    for kind, name, present in rows:
        print("  %-6s %-34s %s" % (kind, name, "PRESENT" if present else
                                   ("ABSENT" if present is False else "UNKNOWN")))
    missing = [n for _k, n, p in rows if p is not True]
    print()
    if missing:
        print("::error::release-complete: %s is INCOMPLETE -- %d of %d artifact(s) not published: %s"
              % (a.version, len(missing), len(rows), ", ".join(missing)))
        print("Do NOT pin it. A green release workflow is not a published artifact; see the "
              "module docstring for the oasgen-provider 0.83.0 case.")
        return 1
    print("release-complete: OK -- %s has all %d artifact(s), controls passed (%s present, %s absent)"
          % (a.version, len(rows), a.control, IMPOSSIBLE))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
