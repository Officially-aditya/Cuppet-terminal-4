#!/bin/sh
set -eu
test "$(cat /app/result.txt)" = 'CUPPET CODEX PREFLIGHT'
test "$(wc -c < /app/result.txt | tr -d ' ')" = 23
printf 'SHELL_OK\n' > /app/shell-ok.txt
printf 'SHELL_OK\n'
