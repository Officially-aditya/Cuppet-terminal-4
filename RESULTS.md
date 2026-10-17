# Terminal-Bench 4: local 29-task comparison

GPT-6 Luna/max, Codex subscription, one retained result per task. Codex ran first, then Cuppet.

| Harness | Passed | Failed | Errors | Total |
| --- | ---: | ---: | ---: | ---: |
| codex | 3 | 24 | 2 | 29 |
| cuppet | 4 | 25 | 0 | 29 |

Infrastructure errors are listed separately from graded failures.
Setup timeouts, authentication failures and interrupted trials are retained as superseded records when retried; completed graded results are preserved.
User-requested retries after model execution are recorded in `manual_retries`; these retained results are not first-attempt scores.
This is a CPU subset score, not a full Terminal-Bench leaderboard score.

Full analysis: [REPORT.md](REPORT.md), covering all task grades, tokens, times, tools, retries, and cleanup.

Detailed local evidence: `reports/comparison.json`, `reports/final-metrics.json`, and the verified private archive `reports/benchmark-evidence-20261017.tar.gz`. Unpacked job directories were removed after archive verification.
