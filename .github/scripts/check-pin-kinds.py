#!/usr/bin/env python3
"""CI guard (installer#31): every component pin's `kind:` must equal the CRD Kind core-provider's
crdgen derives from the chart name:

    flect.Pascalize(strutil.ToGolangName(chartName))    # core-provider chart.go:179

i.e. split the (kebab) chart name on non-alphanumerics, PascalCase each part, then apply
gobuffalo/flect v1.0.3 baseAcronyms (api->API, dns->DNS; crds/mcp/sse/otel etc. are NOT acronyms).
Pass B's inst.crdExists matches spec.names.kind by EXACT string, so a naive-PascalCase mispin
(e.g. GatewayApiCrds vs GatewayAPICrds) silently blocks Pass B on a live cluster — the component's
CD goes Ready but no Composition CR is ever emitted (installer#31, shipped 0.3.21).

Scope: validates the name->Kind derivation from the pinned chart name. It does not pull each chart
to confirm the pin's `chart:` equals the upstream Chart.yaml `name:` (a rarer, separate bug).
If crdgen bumps flect, re-sync FLECT below from gobuffalo/flect acronyms.go baseAcronyms.
"""
import sys, re, yaml
FLECT = ("OK UTF8 HTML JSON JWT ID UUID SQL ACK ACL ADSL AES ANSI API ARP ATM BGP BSS CCITT CHAP "
 "CIDR CIR CLI CPE CPU CRC CRT CSMA CMOS DCE DEC DES DHCP DNS DRAM DSL DSLAM DTE DMI EHA EIA EIGRP "
 "EOF ESS FCC FCS FDDI FTP GBIC gbps GEPOF HDLC HTTP HTTPS IANA ICMP IDF IDS IEEE IETF IMAP IP IPS "
 "ISDN ISP kbps LACP LAN LAPB LAPF LLC MAC Mbps MC MDF MIB MoCA MPLS MTU NAC NAT NBMA NIC NRZ NRZI "
 "NVRAM OSI OSPF OUI PAP PAT PC PIM PCM PDU POP3 POTS PPP PPTP PTT PVST RAM RARP RFC RIP RLL ROM RSTP "
 "RTP RCP SDLC SFD SFP SLARP SLIP SMTP SNA SNAP SNMP SOF SRAM SSH SSID STP SYN TDM TFTP TIA TOFU UDP "
 "URL URI USB UTP VC VLAN VLSM VPN W3C WAN WEP WiFi WPA WWW").split()
CANON = {a.upper(): a for a in FLECT}

def crdgen_kind(chart):
    parts = re.split(r'[^a-zA-Z0-9]+', chart)
    return "".join(CANON.get(p.upper(), p[:1].upper() + p[1:].lower()) for p in parts if p)

pins = yaml.safe_load(open("chart/files/component-pins.yaml"))["components"]
bad = [(c["name"], c.get("chart", c["name"]), c["kind"], crdgen_kind(c.get("chart", c["name"])))
       for c in pins if c["kind"] != crdgen_kind(c.get("chart", c["name"]))]
if bad:
    print("::error::pin kind(s) do not match crdgen PascalCase(chart) — Pass B will silently never match (installer#31):")
    for n, ch, pinned, exp in bad:
        print(f"  {n}: chart={ch}  pinned={pinned}  expected={exp}")
    sys.exit(1)
print(f"pin-kinds: OK — all {len(pins)} match crdgen derivation")
