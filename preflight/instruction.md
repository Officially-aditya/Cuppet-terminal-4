This is a disposable integration preflight, not a benchmark task.

Read `/app/input.txt`, create `/app/result.txt` containing exactly the same text
converted to uppercase (preserving the final newline). Then run
`sh /app/check.sh` using your command execution tool. That script reads the
result, verifies its contents, and writes `/app/shell-ok.txt` as evidence that
the command ran. Let the script create that marker. Do not use the network or
change other task files.

Finish by saying `PREFLIGHT_OK`.
