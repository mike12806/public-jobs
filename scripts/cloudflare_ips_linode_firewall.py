#!/usr/bin/env python3
"""Keep a Linode Cloud Firewall rule in step with Cloudflare's published IPs.

The Linodes behind the Cloudflare load balancer accept HTTPS only from
Cloudflare, through one inbound rule (RULE_LABEL). This replaces that rule's
IPv4 addresses with https://api.cloudflare.com/client/v4/ips and leaves every
other rule, the policies and the rule's IPv6 list exactly as they are.

Fails closed: if Cloudflare's list can't be fetched or doesn't validate,
nothing is written, since an empty or bogus list would cut the origin off
from Cloudflare (or open it wider than intended).

Environment:
  LINODE_TOKEN          Linode API token with firewall read/write
  RULE_LABEL            inbound rule to manage (default accept-inbound-https-cloudflare)
  DRY_RUN               "true" reports the change without writing it
  GITHUB_STEP_SUMMARY   optional; a Markdown summary is appended here
"""

import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request

CF_IPS_URL = "https://api.cloudflare.com/client/v4/ips"
LINODE_API = "https://api.linode.com/v4"
# Cloudflare publishes ~15 IPv4 ranges. Anything far outside this is a broken
# response, not a real change. Linode allows 255 addresses per rule.
MIN_RANGES, MAX_RANGES = 5, 255

TOKEN = os.environ["LINODE_TOKEN"]
RULE_LABEL = os.environ.get("RULE_LABEL") or "accept-inbound-https-cloudflare"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def request(url, method="GET", body=None, headers=None, attempts=4):
    data = json.dumps(body).encode() if body is not None else None
    delay = 5
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # 4xx other than rate limiting won't fix itself; surface Linode's
            # error text (e.g. a token without firewall:read_write).
            if e.code != 429 and e.code < 500:
                raise SystemExit(f"::error::{method} {url} -> {e.code}: {e.read().decode(errors='replace')}")
            err = f"{e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            err = str(e)
        if attempt == attempts:
            raise SystemExit(f"::error::{method} {url} failed after {attempts} attempts: {err}")
        print(f"{method} {url} attempt {attempt} failed ({err}); retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
        delay *= 2


def linode(path, method="GET", body=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    return request(f"{LINODE_API}/{path}", method, body, headers)


def cloudflare_ipv4():
    resp = request(CF_IPS_URL)
    if not resp.get("success"):
        raise SystemExit(f"::error::Cloudflare IP list reported failure: {resp.get('errors')}")
    cidrs = resp.get("result", {}).get("ipv4_cidrs") or []
    if not MIN_RANGES <= len(cidrs) <= MAX_RANGES:
        raise SystemExit(f"::error::Cloudflare returned {len(cidrs)} IPv4 ranges; expected {MIN_RANGES}-{MAX_RANGES}. Not touching the firewall.")
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=True)
        except ValueError:
            raise SystemExit(f"::error::Malformed range {c!r} in Cloudflare's IPv4 list. Not touching the firewall.")
        if net.version != 4 or not net.is_global:
            raise SystemExit(f"::error::Unexpected range {c} in Cloudflare's IPv4 list. Not touching the firewall.")
    return cidrs


def canon(rules):
    """Rules in a comparable form: address order isn't significant."""
    out = []
    for r in rules:
        addrs = {k: sorted(v or []) for k, v in (r.get("addresses") or {}).items()}
        out.append(json.dumps({**r, "addresses": addrs}, sort_keys=True))
    return out


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")


def main():
    cf = cloudflare_ipv4()
    print(f"Cloudflare publishes {len(cf)} IPv4 ranges.")

    firewalls = linode("networking/firewalls?page_size=500")["data"]
    matched = 0
    out = [f"### `{RULE_LABEL}` vs Cloudflare ({len(cf)} IPv4 ranges){' (dry run)' if DRY_RUN else ''}", ""]

    for fw in firewalls:
        rules = linode(f"networking/firewalls/{fw['id']}/rules")
        idx = [i for i, r in enumerate(rules["inbound"]) if r.get("label") == RULE_LABEL]
        if not idx:
            continue
        if len(idx) > 1:
            raise SystemExit(f"::error::Firewall {fw['label']} has {len(idx)} inbound rules labelled {RULE_LABEL}; expected one.")
        matched += 1
        rule = rules["inbound"][idx[0]]
        current = (rule.get("addresses") or {}).get("ipv4") or []
        added, removed = sorted(set(cf) - set(current)), sorted(set(current) - set(cf))
        name = f"{fw['label']} ({fw['id']})"

        if not added and not removed:
            print(f"{name}: in sync ({len(current)} ranges).")
            out.append(f"- **{name}**: in sync, {len(current)} ranges ✅")
            continue

        for c in added:
            print(f"{name}: + {c}")
        for c in removed:
            print(f"{name}: - {c}")
        out.append(f"- **{name}**: +{len(added)} / -{len(removed)} {'(would update)' if DRY_RUN else 'updated'}")
        out += [f"  - added `{c}`" for c in added] + [f"  - removed `{c}`" for c in removed]
        if DRY_RUN:
            continue

        # PUT replaces the whole rule set, so send back exactly what was read
        # with only this rule's IPv4 list changed.
        new_inbound = [dict(r) for r in rules["inbound"]]
        new_inbound[idx[0]] = {**rule, "addresses": {**(rule.get("addresses") or {}), "ipv4": cf}}
        payload = {
            "inbound": new_inbound,
            "inbound_policy": rules["inbound_policy"],
            "outbound": rules["outbound"],
            "outbound_policy": rules["outbound_policy"],
        }
        linode(f"networking/firewalls/{fw['id']}/rules", "PUT", payload)

        after = linode(f"networking/firewalls/{fw['id']}/rules")
        got = [r for r in after["inbound"] if r.get("label") == RULE_LABEL]
        others_before = [r for i, r in enumerate(rules["inbound"]) if i != idx[0]]
        others_after = [r for r in after["inbound"] if r.get("label") != RULE_LABEL]
        if (len(got) != 1 or set(got[0]["addresses"].get("ipv4") or []) != set(cf)
                or canon(others_after) != canon(others_before)
                or canon(after["outbound"]) != canon(rules["outbound"])
                or after["inbound_policy"] != rules["inbound_policy"]):
            raise SystemExit(f"::error::{name}: rules read back after the update don't match what was sent. Check the firewall now.")
        print(f"{name}: updated and verified.")

    if not matched:
        raise SystemExit(f"::error::No Linode firewall has an inbound rule labelled {RULE_LABEL}.")
    summary(out)


if __name__ == "__main__":
    main()
