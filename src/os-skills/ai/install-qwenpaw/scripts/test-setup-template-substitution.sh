#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Regression test for install-qwenpaw setup.sh — credentials were spliced
# into the config templates with `sed "s|{PLACEHOLDER}|$SECRET|g"`, so
# secret bytes that are sed replacement metacharacters corrupted the
# output: `&` expanded to the placeholder itself (silent wrong
# credential that still parses as JSON), and `|` broke the s|||
# expression, aborting the installer under set -euo pipefail.
#
# Runs the real setup.sh end-to-end against a stubbed PATH (uv/curl/
# sleep/pgrep/nohup stubbed; HOME redirected), exactly like the
# config-permission regression test. No dependencies; run directly:
#     bash test-setup-template-substitution.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/setup.sh"

pass=0
fail=0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

ok_()     { echo "ok - $1"; pass=$((pass + 1)); }
not_ok_() { echo "not ok - $1"; fail=$((fail + 1)); }

check_eq() {  # <name> <actual> <expected>
    if [ "$2" = "$3" ]; then
        ok_ "$1"
    else
        not_ok_ "$1 (got '$2', want '$3')"
    fi
}

mkdir -p "$tmp/bin" "$tmp/home"
printf '#!/bin/sh\ncase "$1" in --version) echo "uv 0.11.32 (stub)";; esac\nexit 0\n' > "$tmp/bin/uv"
cat > "$tmp/bin/curl" <<'STUB'
#!/bin/sh
case "$*" in
  *localhost:8088*) echo "404" ;;
  *) echo "curl-stub $*" ;;
esac
STUB
printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/sleep"
printf '#!/bin/sh\nexit 1\n' > "$tmp/bin/pgrep"
printf '#!/bin/sh\nexit 0\n' > "$tmp/bin/nohup"
chmod +x "$tmp/bin/uv" "$tmp/bin/curl" "$tmp/bin/sleep" "$tmp/bin/pgrep" "$tmp/bin/nohup"

run_setup() {  # <api_key> <client_id> <client_secret>
    (
        cd "$tmp" || exit 1
        HOME="$tmp/home" PATH="$tmp/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
        bash "$SCRIPT" "$1" "$2" "$3" \
            > "$tmp/out" 2> "$tmp/err"
    )
    echo $?
}

json_field() {  # <file> <python-expr over d>
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sys.argv[2] and eval(sys.argv[2]))' \
        "$1" "$2" 2>/dev/null || echo "JSON-ERROR"
}

# ── Scenario 1: `&` in the secret silently corrupts the written config ──
code="$(run_setup 'sk-a&b-key123' 'ding-client-id' 'sec&ret-value')"
echo "# scenario1 exit=$code"

check_eq "setup survives & in credentials" "$code" "0"
got="$(json_field "$tmp/home/.qwenpaw/config.json" \
    'd["channels"]["dingtalk"]["client_secret"]')"
check_eq "client_secret round-trips (&)" "$got" "sec&ret-value"
got="$(json_field "$tmp/home/.qwenpaw.secret/providers/builtin/dashscope.json" \
    'd["api_key"]')"
check_eq "dashscope api_key round-trips (&)" "$got" "sk-a&b-key123"

# ── Scenario 2: `|` in the secret used to abort the whole install ──
rm -rf "$tmp/home"
mkdir -p "$tmp/home"
code="$(run_setup 'sk-plain-key-99' 'ding-client-id' 'sec|ret-value')"
echo "# scenario2 exit=$code"

check_eq "setup survives | in secret" "$code" "0"
got="$(json_field "$tmp/home/.qwenpaw/config.json" \
    'd["channels"]["dingtalk"]["client_secret"]')"
check_eq "client_secret round-trips (|)" "$got" "sec|ret-value"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
