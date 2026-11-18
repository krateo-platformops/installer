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
CHART_PREFIX = "oci://ghcr.io/krateo-platformops/charts"
IMAGE_ORG = "krateo-platformops"
IMPOSSIBLE = "9.9.9"


def chart_present(name, version, timeout=90):
    """True if `helm show chart` can read it. helm exits non-zero for an absent version."""
    try:
        r = subprocess.run(
            ["helm", "show", "chart", "%s/%s" % (CHART_PREFIX, name), "--version", version],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None  # unknown, not absent -- the caller must not read this as a verdict


def _token(repo, timeout=30):
    url = ("https://%s/token?scope=repository:%s/%s:pull&service=%s"
           % (REGISTRY, IMAGE_ORG, repo, REGISTRY))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            return (json.load(fh) or {}).get("token")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def image_present(repo, tag, token, timeout=30):
    """True/False from a TOKENED manifest HEAD. None if the probe itself could not run.

    Without the token this returns 401 for present and absent alike -- see the module
    docstring. The Accept header must name the index/list types or a multi-arch tag can
    answer 404 for a manifest it does have in another form.
    """
    if not token:
        return None
    url = "https://%s/v2/%s/%s/manifests/%s" % (REGISTRY, IMAGE_ORG, repo, tag)
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
    a = ap.parse_args(argv)

    if not a.chart and not a.image:
        print("release-complete: nothing to probe -- pass at least one --chart or --image")
        return 2

    rows, broken = [], []

    for name in a.chart:
        under = chart_present(name, a.version)
        ctl_ok = chart_present(name, a.control)
        ctl_no = chart_present(name, IMPOSSIBLE)
        if ctl_ok is not True or ctl_no is not False:
            broken.append("chart %s: control %s=%r and %s=%r -- the probe does not discriminate"
                          % (name, a.control, ctl_ok, IMPOSSIBLE, ctl_no))
        rows.append(("chart", name, under))

    for repo in a.image:
        tok = _token(repo)
        under = image_present(repo, a.version, tok)
        ctl_ok = image_present(repo, a.control, tok)
        ctl_no = image_present(repo, IMPOSSIBLE, tok)
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
