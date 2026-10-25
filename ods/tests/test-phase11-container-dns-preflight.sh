#!/usr/bin/env bash
# Phase 11 must resolve build registries inside a container before building
# local images, and stop with the cause and fix when it cannot (issue #7447).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PHASE11="$ROOT_DIR/installers/phases/11-services.sh"
LIB="$ROOT_DIR/installers/lib/container-dns.sh"
DASHBOARD_IMAGE="$(awk 'toupper($1) == "FROM" { print $2; exit }' \
    "$ROOT_DIR/extensions/services/dashboard/Dockerfile")"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
    echo "[FAIL] $*" >&2
    exit 1
}

pass() {
    echo "[PASS] $*"
}

extract_phase11_function() {
    sed -n "/^${1}() {/,/^}$/p" "$PHASE11"
}

[[ -f "$LIB" ]] || fail "missing $LIB"
# shellcheck source=../installers/lib/container-dns.sh
source "$LIB"
for fn in _phase11_check_build_dns _phase11_build_local_images; do
    eval "$(extract_phase11_function "$fn")"
    declare -F "$fn" >/dev/null || fail "could not extract $fn"
done

# ── Stubs on PATH ───────────────────────────────────────────
BIN="$TMP_DIR/bin"
mkdir -p "$BIN" "$TMP_DIR/install/extensions/services/dashboard"
cp "$ROOT_DIR/extensions/services/dashboard/Dockerfile" \
    "$TMP_DIR/install/extensions/services/dashboard/Dockerfile"
CALL_LOG="$TMP_DIR/docker.calls"

cat > "$BIN/docker" <<'MOCK'
#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$MOCK_DOCKER_CALL_LOG"
case " $* " in
    *" run "*)
        case "${MOCK_PROBE:-ok}" in
            ok)    printf 'OK registry.npmjs.org\nOK pypi.org\n'; exit 0 ;;
            dns)   printf 'FAIL registry.npmjs.org EAI_AGAIN\nOK pypi.org\n'; exit 1 ;;
            broken) echo 'docker: Error response from daemon: oci runtime error' >&2; exit 125 ;;
        esac
        ;;
    *" image inspect "*) [[ "${MOCK_IMAGE_PRESENT:-true}" == true ]]; exit ;;
    *" pull "*) exit 0 ;;
    *" config --format json "*)
        echo '{"name":"ods","services":{"dashboard":{"build":{"context":"x"}}}}'
        exit 0
        ;;
esac
exit 0
MOCK
cat > "$BIN/resolvectl" <<'MOCK'
#!/usr/bin/env bash
printf 'Global:\nLink 2 (enp3s0): 192.168.1.1 fe80::1%%2\nLink 3 (wlp2s0): 1.1.1.1#cloudflare-dns.com\nLink 4 (docker0):\n'
MOCK
chmod +x "$BIN/docker" "$BIN/resolvectl"
export PATH="$BIN:$PATH"
export MOCK_DOCKER_CALL_LOG="$CALL_LOG"

printf 'nameserver 127.0.0.53\noptions edns0 trust-ad\nsearch lan\n' > "$TMP_DIR/resolv.stub"
printf 'nameserver 192.168.1.1\n' > "$TMP_DIR/resolv.lan"

export INSTALL_DIR="$TMP_DIR/install"
export LOG_FILE="$TMP_DIR/install.log"
export DOCKER_CMD="docker"
export DOCKER_COMPOSE_CMD="docker compose"
export ODS_DOCKER_BUILD_MAX_ATTEMPTS=3 ODS_DOCKER_BUILD_RETRY_DELAY_SECONDS=0
# shellcheck disable=SC2034  # Read by the extracted phase 11 functions.
COMPOSE_FLAGS_ARR=(-f docker-compose.base.yml)
OUT="$TMP_DIR/out.log"

log() { printf 'LOG: %s\n' "$*" >> "$OUT"; }
ai() { printf '%s\n' "$*" >> "$OUT"; }
ai_ok() { printf 'OK: %s\n' "$*" >> "$OUT"; }
ai_bad() { printf 'ERROR: %s\n' "$*" >> "$OUT"; }
ai_warn() { printf 'WARN: %s\n' "$*" >> "$OUT"; }
ui_status_line() { printf '[%s] %s\n' "$1" "$2" >> "$OUT"; }
spin_task() { wait "$1"; }

# Mirrors the phase 11 call site: the DNS check gates the build loop.
run_build_section() {
    : > "$CALL_LOG"
    : > "$OUT"
    : > "$LOG_FILE"
    _phase11_check_build_dns "$@" || return 1
    _phase11_build_local_images "$@"
}

# ── Pure helpers ────────────────────────────────────────────
ods_resolv_conf_is_loopback_only < "$TMP_DIR/resolv.stub" \
    || fail "127.0.0.53 stub resolv.conf not classified as loopback-only"
! ods_resolv_conf_is_loopback_only < "$TMP_DIR/resolv.lan" \
    || fail "LAN resolv.conf classified as loopback-only"
! printf 'nameserver 127.0.0.53\nnameserver 10.0.0.1\n' | ods_resolv_conf_is_loopback_only \
    || fail "mixed resolv.conf classified as loopback-only"
! printf '# empty\n' | ods_resolv_conf_is_loopback_only \
    || fail "resolv.conf without nameservers classified as loopback-only"
[[ "$(resolvectl dns | ods_resolvectl_dns_servers)" == "192.168.1.1 1.1.1.1" ]] \
    || fail "resolvectl dns parsing: $(resolvectl dns | ods_resolvectl_dns_servers)"
[[ "$(printf 'FROM --platform=linux/amd64 a/b:1@sha256:00 AS x\nFROM c\n' | ods_dockerfile_first_from_image)" == "a/b:1@sha256:00" ]] \
    || fail "first FROM image not parsed"
pass "resolver classification and image parsing are pure and correct"

# ── Probe failure stops before any build ────────────────────
export MOCK_PROBE=dns ODS_HOST_RESOLV_CONF="$TMP_DIR/resolv.stub"
if run_build_section dashboard dashboard-api; then
    fail "container DNS failure did not stop the install"
fi
grep -q '^run ' "$CALL_LOG" || fail "no container DNS probe ran"
! grep -q ' build' "$CALL_LOG" || fail "an image build ran after the DNS probe failed"
grep -q "run --rm --pull never --entrypoint node $DASHBOARD_IMAGE -e" "$CALL_LOG" \
    || fail "probe did not use the digest-pinned dashboard base image without pulling: $(cat "$CALL_LOG")"
[[ "$DASHBOARD_IMAGE" == *@sha256:* ]] || fail "dashboard base image is not digest-pinned"
grep -q 'registry.npmjs.org pypi.org' "$CALL_LOG" || fail "probe did not resolve the build registries"
grep -q 'ERROR: Containers on this Docker daemon cannot resolve package registries (registry.npmjs.org: EAI_AGAIN)' "$OUT" \
    || fail "failure did not name the unresolvable host: $(cat "$OUT")"
grep -q '127.0.0.53' "$OUT" || fail "loopback stub resolv.conf was not named"
grep -qF 'add "dns": ["192.168.1.1", "1.1.1.1"] to /etc/docker/daemon.json' "$OUT" \
    || fail "fix did not list the host uplink resolvers"
grep -q 'sudo systemctl restart docker' "$OUT" || fail "fix did not say to restart Docker"
grep -q 'INSTALL-TROUBLESHOOTING.md#container-build-dns' "$OUT" || fail "troubleshooting link missing"
pass "probe failure aborts before building and names the stub, resolvers and fix"

# Non-stub host: the stub sentence must not appear; no resolvectl: placeholder.
export ODS_HOST_RESOLV_CONF="$TMP_DIR/resolv.lan"
rm "$BIN/resolvectl"
if run_build_section dashboard; then
    fail "container DNS failure did not stop the install"
fi
! grep -q '127.0.0.53' "$OUT" || fail "stub named although resolv.conf lists a LAN resolver"
grep -qF '"dns": ["<resolver-ip>"]' "$OUT" || fail "placeholder resolver missing without resolvectl"
pass "stub is named only when resolv.conf lists only a loopback resolver"

# ── Success proceeds to the builds ──────────────────────────
export MOCK_PROBE=ok
run_build_section dashboard dashboard-api || fail "successful DNS probe blocked the install: $(cat "$OUT")"
grep -q 'compose.* build dashboard$' "$CALL_LOG" || fail "dashboard was not built after a passing probe"
grep -q 'compose.* build dashboard-api$' "$CALL_LOG" || fail "dashboard-api was not built after a passing probe"
[[ "$(grep -n '^run ' "$CALL_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -n ' build ' "$CALL_LOG" | head -1 | cut -d: -f1)" ]] \
    || fail "probe did not run before the first build"
pass "a passing probe proceeds to the builds"

# Image not cached yet: pulled once (the build needs it anyway), then probed.
: > "$CALL_LOG"
MOCK_IMAGE_PRESENT=false _phase11_check_build_dns dashboard || fail "probe with an uncached image failed"
grep -q "^pull $DASHBOARD_IMAGE$" "$CALL_LOG" || fail "uncached probe image was not pulled by digest"
pass "an uncached probe image is pulled by its pinned digest"

# Docker could not run the probe: not DNS evidence, so builds still run.
MOCK_PROBE=broken run_build_section dashboard || fail "a probe that could not run blocked the install"
grep -q 'WARN: Container DNS check could not run (exit 125)' "$OUT" || fail "unrunnable probe not reported"
grep -q 'compose.* build dashboard$' "$CALL_LOG" || fail "build skipped after an unrunnable probe"
pass "a probe Docker cannot run warns and lets the builds report errors"

# ── Skips ───────────────────────────────────────────────────
export MOCK_PROBE=dns
OFFLINE_MODE=true run_build_section dashboard || fail "offline install ran the DNS probe"
! grep -qF -e 'run ' -e 'pull ' -e "image inspect $DASHBOARD_IMAGE" "$CALL_LOG" || fail "offline mode touched Docker for the probe"
grep -q 'Skipping container DNS check: offline mode' "$OUT" || fail "offline skip not logged"
pass "offline mode skips the probe"

run_build_section || fail "an install with no local builds ran the probe"
[[ ! -s "$CALL_LOG" ]] || fail "probe ran with nothing to build: $(cat "$CALL_LOG")"
pass "no local builds means no probe"

# ── Phase wiring ────────────────────────────────────────────
# shellcheck disable=SC2016  # Literal phase source text.
check_line="$(grep -nF '_phase11_check_build_dns "${_build_services[@]}"' "$PHASE11" | head -1 | cut -d: -f1)"
# shellcheck disable=SC2016  # Literal phase source text.
build_line="$(grep -nF '_phase11_build_local_images "${_build_services[@]}"' "$PHASE11" | head -1 | cut -d: -f1)"
[[ -n "$check_line" && -n "$build_line" && "$check_line" -lt "$build_line" ]] \
    || fail "phase 11 must run the DNS check before the local image builds"
sed -n "${check_line},$((check_line + 2))p" "$PHASE11" | grep -q 'exit 1' \
    || fail "a failed DNS check must stop phase 11"
grep -q 'installers/lib/container-dns.sh' "$ROOT_DIR/install-core.sh" \
    || fail "install-core.sh does not source container-dns.sh"
pass "phase 11 stops on a failed DNS check before building"
