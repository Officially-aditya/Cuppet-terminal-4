"""Six independent MVCC repairs, varying only native TST availability."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('CUPPET_BENCHMARK_SETTINGS', str(ROOT / 'settings-mvcc-matched.json'))
import benchmark


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def elapsed(record):
    if not record or not record.get('finished_at'):
        return None
    return (datetime.fromisoformat(record['finished_at'].replace('Z', '+00:00')) -
            datetime.fromisoformat(record['started_at'].replace('Z', '+00:00'))).total_seconds()


def run():
    settings = benchmark.SETTINGS
    assert settings['matched_comparison'] and not settings['dynamic_enrichment'] and not settings['pre_run_enrichment']
    assert settings['model'] == 'gpt-6-luna' and settings['reasoning_effort'] == 'max'
    assert settings['order'] == ['native', 'unconfigured', 'unconfigured', 'native', 'native', 'unconfigured']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'mvcc-matched-{stamp}'
    folder = ROOT / 'reports' / name
    folder.mkdir(parents=True)
    lock = (ROOT / 'reports/mvcc-matched.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env = benchmark.docker_environment()
    identity = json.loads((benchmark.CACHE / settings['bundle_identity']).read_text())
    if digest(benchmark.BUNDLE.read_bytes()) != identity['bundle_sha256']:
        raise RuntimeError('Pinned runtime bundle digest does not match its identity')
    task = benchmark.DATASET / 'mvcc-lsm-compaction'
    expected_files = [{'path': str(path.relative_to(task / 'environment/app')), 'bytes': path.stat().st_size,
                       'sha256': digest(path.read_bytes())}
                      for path in sorted((task / 'environment/app').rglob('*')) if path.is_file()]
    expected_files.sort(key=lambda row: row['path'])
    assert len(expected_files) == 14
    task_files = [{'path': str(path.relative_to(task)), 'sha256': digest(path.read_bytes())}
                  for path in sorted(task.rglob('*')) if path.is_file()]
    state = dict(name=name, pid=os.getpid(), state='running', phase='preflight', started_at=now(),
                 settings=settings, bundle=identity, source_files=expected_files,
                 task_files=task_files, preflight=[], trials=[], current=None, completed=0,
                 audit_reference=None, order=settings['order'], fresh_workspace_per_attempt=True,
                 no_grader_feedback_to_agents=True, retries=0,
                 adapter_revision=subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip())
    latest = ROOT / 'reports/mvcc-matched-latest.json'

    def persist():
        state['updated_at'] = now()
        for path in (folder / 'status.json', latest):
            temporary = path.with_suffix('.tmp')
            benchmark.save(temporary, state)
            temporary.replace(path)

    def attempt(mode, index, preflight):
        config = benchmark.configuration('cuppet', preflight=preflight)
        config['job_name'] = f'{"preflight" if preflight else "terminal-bench-4"}-cuppet-{name}-{index}-{mode}'
        config['quiet'] = True
        config['agents'][0]['kwargs']['matched_tst_mode'] = mode
        if not preflight:
            assert config['agents'][0]['kwargs']['timeout_sec'] == 0
            assert len(config['tasks']) == 1 and Path(config['tasks'][0]['path']).name == 'mvcc-lsm-compaction'
        state['current'] = dict(mode=mode, index=index, preflight=preflight, job=f"jobs/{config['job_name']}")
        persist()
        benchmark.save(folder / f'{"preflight" if preflight else "attempt"}-{index}-{mode}.json', config)
        job = benchmark.run_job(config, env)
        paths = list(job.glob('*/result.json'))
        if len(paths) != 1:
            raise RuntimeError(f'Expected exactly one retained result in {job}; found {len(paths)}')
        result_path = paths[0]
        result = json.loads(result_path.read_text())
        summary_path = result_path.parent / 'agent/cuppet-result.json'
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        audit = summary.get('matchedComparison') or {}
        error = (result.get('exception_info') or {}).get('exception_type')
        row = dict(index=index, mode=mode, preflight=preflight, job=str(job.relative_to(ROOT)),
                   trial=result_path.parent.name, reward=(result.get('verifier_result') or {}).get('rewards', {}).get('reward'),
                   error=error, agent_seconds=elapsed(result.get('agent_execution')),
                   primary_seconds=((summary.get('sample') or {}).get('elapsedMs') or 0) / 1000,
                   usage=(summary.get('sample') or {}).get('usage'), tst=summary.get('tst'),
                   audit=audit, source=summary.get('source'), task_checksum=result.get('task_checksum'))
        ctrf = result_path.parent / 'verifier/ctrf.json'
        if ctrf.exists():
            official = json.loads(ctrf.read_text())['results']
            row['official_tests'] = official['summary']
            row['failed_tests'] = [item['name'] for item in official.get('tests', []) if item.get('status') == 'failed']
        events_path = result_path.parent / 'agent/cuppet-events.jsonl'
        if events_path.exists():
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            row['tool_operations'] = sum(event.get('type') == 'tool.finished' for event in events)
            row['validation_rounds'] = sum(event.get('type') == 'tool.finished' and event.get('tool') == 'tst_validate' for event in events)
            row['enrichment_events'] = sum(str(event.get('type', '')).startswith('background.dynamic.') for event in events)
        if not error:
            if not audit.get('sourceVerified') or not audit.get('inputContainsOriginalTask'):
                raise RuntimeError(f'Model input audit failed for {mode}: {audit}')
            assert audit['mode'] == mode and audit['model'] == settings['model'] and audit['effort'] == settings['reasoning_effort']
            assert audit['developerInstructionsSha256'] == settings['developer_instructions_sha256']
            assert audit['policySha256'] == settings['system_prompt_sha256'] and len(audit['tools']) == 10
            assert not row.get('enrichment_events') and not audit['backgroundEnrichment']
            assert summary['source']['cuppet_revision'] == settings['cuppet_revision']
            assert audit['isolatedCodexHome'] == '/tmp/cuppet-codex-home'
            tst = summary['tst']
            assert tst['configured'] == (mode == 'native') and tst['connected'] == (mode == 'native')
            assert tst['mode'] == ('managed-native' if mode == 'native' else 'unconfigured-control')
            if not preflight:
                assert sorted(audit['initialWorkspace'], key=lambda file: file['path']) == expected_files
                keys = ['instructionSha256', 'initialWorkspaceSha256', 'policySha256',
                        'developerInstructionsSha256', 'toolSchemasSha256', 'tools', 'model', 'effort', 'controlSha256']
                shared = {key: audit[key] for key in keys}
                if state['audit_reference'] is None:
                    state['audit_reference'] = shared
                elif shared != state['audit_reference']:
                    raise RuntimeError('A supposedly matched model configuration or starting workspace changed')
                prior = [trial['audit'].get('initialInputSha256') for trial in state['trials'] if trial.get('audit')]
                row['initial_input_matches_prior'] = not prior or all(value == audit['initialInputSha256'] for value in prior)
        else:
            row['input_audit_available'] = bool(audit)
        state['preflight' if preflight else 'trials'].append(row)
        state['completed'] = len(state['trials'])
        persist()
        print(json.dumps({key: row.get(key) for key in ['mode', 'index', 'preflight', 'reward', 'error', 'agent_seconds', 'official_tests']}), flush=True)
        if preflight and (error or row['reward'] != 1 or 'PREFLIGHT_OK' not in summary.get('text', '')):
            raise RuntimeError(f'The {mode} integration preflight failed; no scored repairs were started')
        return row

    persist()
    try:
        for index, mode in enumerate(settings['arms'], 1):
            attempt(mode, index, True)
        state['phase'] = 'scored_attempts'
        persist()
        for index, mode in enumerate(settings['order'], 1):
            attempt(mode, index, False)
        state.update(state='complete', phase='complete', current=None, finished_at=now())
        state['summary'] = {}
        for mode in settings['arms']:
            rows = [row for row in state['trials'] if row['mode'] == mode]
            state['summary'][mode] = dict(attempts=len(rows), passed=sum(row['reward'] == 1 and not row['error'] for row in rows),
                errors=sum(bool(row['error']) for row in rows),
                median_agent_seconds=statistics.median([row['agent_seconds'] for row in rows if row['agent_seconds'] is not None]) if any(row['agent_seconds'] is not None for row in rows) else None)
        state['initial_inputs_identical'] = len({row['audit'].get('initialInputSha256') for row in state['trials'] if row.get('audit')}) == 1
        persist()
        benchmark.save(ROOT / 'MVCC-MATCHED-RESULTS.json', state)
        lines = ['# MVCC matched native TST comparison', '',
                 'Six fresh repairs with GPT-6 Luna/max, three per arm, sequential order native/control, control/native, native/control.',
                 'Both arms use the same pinned Cuppet runtime inside Docker, isolated Codex profile, full policy and ten tools. Background enrichment, PE3, and orchestration are disabled.',
                 'Only the native TST interface is varied. The control delegates it to Cuppet’s unconfigured bridge, retaining the existing fallback behavior. This is not native Codex.', '',
                 '| Attempt | Arm | Agent seconds | Reward | Official tests | Error |',
                 '| --- | --- | ---: | ---: | --- | --- |']
        for row in state['trials']:
            tests = row.get('official_tests') or {}
            seconds = f"{row['agent_seconds']:.2f}" if row['agent_seconds'] is not None else 'unavailable'
            lines.append(f"| {row['index']} | {row['mode']} | {seconds} | {row['reward']} | {tests.get('passed', '?')}/{tests.get('tests', '?')} | {row['error'] or 'none'} |")
        lines += ['', f"Both integration preflights passed. Identical initial model text across scored attempts: {state['initial_inputs_identical']}.",
                  'Starting workspace file hashes, original instruction, tool policy, developer instructions, model/effort, and complete tool schemas were checked against the first scored attempt. All 14 starting files also matched the supplied broken project.', '',
                  f"Source: `{settings['cuppet_revision']}`; bundle SHA-256: `{identity['bundle_sha256']}`.",
                  'Each attempt uses a separate clean container, conversation, and state directory, with the original eight-hour task budget and no added time cap. Every scheduled attempt is retained, including failures; no score-based retries.',
                  'No official test contents, grader feedback, prior solution, or prior-run conversation is passed to any agent.', '',
                  'Three attempts per arm can reveal repeated behavior, but remain too few to establish a reliable general performance difference.', '',
                  f'Raw evidence: `reports/{name}/status.json` and the job paths listed in `MVCC-MATCHED-RESULTS.json`.']
        (ROOT / settings['result_report']).write_text('\n'.join(lines) + '\n')
    except BaseException as error:
        state.update(state='needs_attention', error=str(error))
        persist()
        raise


if __name__ == '__main__':
    run()
