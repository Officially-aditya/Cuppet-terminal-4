#!/bin/sh
set -eu
mkdir -p /logs/verifier
if test -f /app/result.txt && test "$(cat /app/result.txt)" = 'CUPPET CODEX PREFLIGHT' && test "$(wc -c < /app/result.txt | tr -d ' ')" = 23 && test -f /app/shell-ok.txt && test "$(cat /app/shell-ok.txt)" = 'SHELL_OK'; then
  printf '1.0\n' > /logs/verifier/reward.txt
else
  printf '0.0\n' > /logs/verifier/reward.txt
  exit 1
fi
