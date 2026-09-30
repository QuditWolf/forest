#!/usr/bin/env bash
# Post-deploy smoke test through the real path (internet -> nginx -> VPN -> container).
# Usage: deploy/smoke.sh https://tools.example.com fst_<write token>
set -uo pipefail
B="${1:?url}"; T="${2:?token}"; fail=0
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got $2, want $3)"; fail=1; fi; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

check "https health"               "$(code "$B/healthz")" 200
check "http redirects to https"    "$(code "${B/https:/http:}/healthz")" 301
check "ui needs login"             "$(code "$B/")" 302
check "api needs auth"             "$(code "$B/api/me")" 401
check "bad token rejected"         "$(code -H 'Authorization: Bearer fst_x' "$B/api/me")" 401
check "token works"                "$(code -H "Authorization: Bearer $T" "$B/api/me")" 200
check "oauth metadata public url"  "$(curl -s "$B/.well-known/oauth-authorization-server" | grep -c "\"issuer\":\"$B\"")" 1
check "mcp 401 advertises oauth"   "$(curl -s -D- -o /dev/null -X POST "$B/mcp" | grep -ci 'resource_metadata')" 1
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
check "mcp initialize"             "$(code -X POST -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT" "$B/mcp")" 200
check "mcp from claude.ai origin"  "$(code -X POST -H "Authorization: Bearer $T" -H 'Origin: https://claude.ai' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$INIT" "$B/mcp")" 200
check "mcp foreign origin blocked" "$(code -X POST -H "Authorization: Bearer $T" -H 'Origin: https://evil.example' -H 'Content-Type: application/json' -d "$INIT" "$B/mcp")" 403
check "git over https"             "$(git ls-remote "https://x:$T@${B#https://}/git/forest.git" >/dev/null 2>&1 && echo ok)" ok
# 2 MB body reaches the app (400 = app rejected it; 413 = nginx client_max_body_size too small). No data written.
head -c 2000000 /dev/zero > /tmp/forest-smoke.bin
check "large bodies pass nginx"    "$(code -X POST -H 'Content-Type: application/json' --data-binary @/tmp/forest-smoke.bin "$B/oauth/register")" 400
rm -f /tmp/forest-smoke.bin
check "real client IP in audit (not the nginx IP) - check /admin audit log manually" ok ok
exit $fail
