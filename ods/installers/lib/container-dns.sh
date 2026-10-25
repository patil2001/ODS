#!/bin/bash
# ============================================================================
# ODS Installer — Container build DNS helpers
# ============================================================================
# Part of: installers/lib/
# Purpose: Pure helpers for the phase 11 pre-build container DNS check:
#          pick the probe image from a Dockerfile, classify host resolver
#          files, and build the failure message.
#
# Expects: Nothing (pure functions; inputs on stdin or as arguments)
# Provides: ods_container_dns_probe_hosts(), ods_dockerfile_first_from_image(),
#           ods_resolv_conf_is_loopback_only(), ods_resolvectl_dns_servers(),
#           ods_container_dns_failure_message(), ODS_CONTAINER_DNS_PROBE_JS
#
# Modder notes:
#   The probe hosts are the registries the always-built dashboard (npm ci) and
#   dashboard-api (pip) images need. Keep them in step with those Dockerfiles.
# ============================================================================

# Resolves each host argument through getaddrinfo, the path npm and pip use,
# and prints "OK <host>" or "FAIL <host> <code>". Exits 1 if any host fails.
# The timer bounds a resolver that never answers.
# shellcheck disable=SC2034  # Used by installers/phases/11-services.sh.
ODS_CONTAINER_DNS_PROBE_JS='
const dns = require("dns");
const hosts = process.argv.slice(1);
const pending = new Set(hosts);
let failed = false;
const timer = setTimeout(() => {
  for (const host of pending) console.log("FAIL " + host + " ETIMEOUT");
  process.exit(1);
}, 15000);
for (const host of hosts) {
  dns.lookup(host, (err) => {
    pending.delete(host);
    if (err) { failed = true; console.log("FAIL " + host + " " + (err.code || "ERROR")); }
    else console.log("OK " + host);
    if (pending.size === 0) { clearTimeout(timer); process.exit(failed ? 1 : 0); }
  });
}
'

ods_container_dns_probe_hosts() {
    printf '%s\n' registry.npmjs.org pypi.org
}

# stdin: Dockerfile content. Prints the image reference of the first FROM,
# skipping flags such as --platform.
ods_dockerfile_first_from_image() {
    awk '
        toupper($1) == "FROM" {
            for (i = 2; i <= NF; i++) {
                if ($i !~ /^--/) { print $i; exit }
            }
        }
    '
}

# stdin: resolv.conf content. Succeeds when at least one nameserver is listed
# and every nameserver is loopback, such as the systemd-resolved 127.0.0.53
# stub. Containers cannot reach a host loopback resolver.
ods_resolv_conf_is_loopback_only() {
    awk '
        $1 == "nameserver" {
            seen = 1
            if ($2 !~ /^127\./ && $2 != "::1") other = 1
        }
        END { exit (seen && !other) ? 0 : 1 }
    '
}

# stdin: `resolvectl dns` output, e.g. "Link 2 (eth0): 192.168.1.1 fe80::1%2".
# Prints the distinct non-loopback resolver addresses on one line. Drops
# DNS-over-TLS names (#name) and zone-scoped link-local addresses, which
# daemon.json cannot use.
ods_resolvectl_dns_servers() {
    awk '
        {
            sub(/^[^:]*:/, "")
            for (i = 1; i <= NF; i++) {
                addr = $i
                sub(/#.*/, "", addr)
                if (addr == "" || addr ~ /%/ || addr ~ /^127\./ || addr == "::1") continue
                if (!(addr in seen)) { seen[addr] = 1; out = out (out ? " " : "") addr }
            }
        }
        END { if (out) print out }
    '
}

# $1: failed probe results, e.g. "registry.npmjs.org: EAI_AGAIN, pypi.org: ENOTFOUND"
# $2: "true" when the host resolv.conf lists only a loopback stub
# $3: host uplink resolvers (space separated, may be empty)
# Prints one message line per output line; the first line is the error.
ods_container_dns_failure_message() {
    local failures="$1" loopback_stub="$2" resolvers="$3" dns_list="" addr

    printf 'Containers on this Docker daemon cannot resolve package registries (%s), so local image builds (npm ci, pip, apt-get) would fail.\n' "$failures"
    if [[ "$loopback_stub" == "true" ]]; then
        printf '%s\n' "Host /etc/resolv.conf lists only a loopback stub resolver (such as systemd-resolved's 127.0.0.53), which containers cannot reach. Docker then uses the uplink resolvers or falls back to public DNS such as 8.8.8.8, which this network may block."
    fi
    if [[ -n "$resolvers" ]]; then
        printf 'Host uplink resolvers from resolvectl dns: %s\n' "$resolvers"
        for addr in $resolvers; do
            dns_list="${dns_list:+$dns_list, }\"$addr\""
        done
    else
        dns_list='"<resolver-ip>"'
    fi
    printf 'Fix: add "dns": [%s] to /etc/docker/daemon.json (use resolvers reachable from this network; preserve existing keys), restart Docker (sudo systemctl restart docker), then rerun the installer.\n' "$dns_list"
    printf '%s\n' "See docs/INSTALL-TROUBLESHOOTING.md#container-build-dns. ODS has not changed DNS settings or restarted Docker."
}
