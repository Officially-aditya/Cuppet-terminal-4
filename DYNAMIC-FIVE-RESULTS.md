# Cuppet dynamic enrichment: five-task run

GPT-6 Luna/max; source `9575f2391b4e15248edb8a534f0e1556af24a5cf`.

Enrichment intervals: 1, 2, 4, 8… minutes after the first primary tool call, then after each completed enrichment attempt. The schedule resets per task.
Both foreground and background use Luna/max. Task prompts, final graders and original total solve budgets are unchanged.

| Task | Reward | Error | Enrichment rounds | Deliveries |
| --- | ---: | --- | ---: | ---: |
| terminal-bench/session-window-debug | 0.0 |  | 2 | 2 |
| terminal-bench/mvcc-lsm-compaction | 0.0 |  | 1 | 1 |
| terminal-bench/cargo-flight-dispatch | 0.0 |  | 2 | 2 |
| terminal-bench/embedding-drift-monitor | 0.0 |  | 2 | 2 |
| terminal-bench/bun-sourcemap-leak | 0.0 |  | 2 | 2 |

Passed: 0; graded failures: 5; errors: 0.

Detailed evidence: `reports/terminal-bench-4-cuppet-dynamic-five-20261030T191233Z/status.json` and `jobs/terminal-bench-4-cuppet-dynamic-five-20261030T191233Z`.

The updated prompt and runtime differ from the previous October 17 baseline, so any difference cannot be attributed solely to memory enrichment.
