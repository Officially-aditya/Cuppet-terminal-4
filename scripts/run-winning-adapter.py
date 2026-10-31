"""Replay the saved passing adapter's Original arm once, from broken code."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/Users/addy/Documents/Cuppet-desktop')
HISTORICAL = Path('/Users/addy/Documents/Codex/2026-10-29/che/work/agentstress-mvcc/mvcc-lsm-compaction-run-20261031T062913Z')
PIN = '63a4febd561630d67f126caa9ea2b2154fa87d6b'
NODE = Path('/Users/addy/.nvm/versions/node/v22.21.0/bin/node')
EXPECTED = {
    'harbor_cuppet_agent.py': 'd1bf3373ad1926b8fdeca278e3990797e8ffe51fb1b969c6c1e20a0a09d69944',
    'cuppet_trial.mjs': '4daaefb1b140b80c5b263314d93c6af34b2e7b8b338480a3093c62b2b2be69ca',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def run():
    assert subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, capture_output=True, text=True, check=True).stdout.strip() == PIN
    assert subprocess.run([NODE, '--version'], capture_output=True, text=True, check=True).stdout.strip() == 'v22.21.0'
    assert '0.162.1' in subprocess.run(['codex', '--version'], capture_output=True, text=True, check=True).stdout
    for name, expected in EXPECTED.items():
        assert digest((ROOT / 'winning_adapter' / name).read_bytes()) == expected
        assert (ROOT / 'winning_adapter' / name).read_bytes() == (HISTORICAL / name).read_bytes()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'mvcc-winning-adapter-{stamp}'
    folder = ROOT / 'reports' / name
    folder.mkdir(parents=True)
    state_dir = folder / 'state'
    attempt = state_dir / 'mvcc-lsm-compaction/original'
    audit_dir = attempt / 'data'
    workspace = attempt / 'workspace'
    wrapper = folder / 'node-with-audit'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(str(NODE)) + ' --import ' + shlex.quote(str(ROOT / 'scripts/winning-input-audit.mjs')) + ' "$@"\n')
    wrapper.chmod(0o755)
    job_name = f'cuppet-{name}-original'
    config = json.loads((HISTORICAL / 'jobs/agentstress-20261031T062913Z-mvcc-lsm-compaction-original/config.json').read_text())
    config['job_name'] = job_name
    config['jobs_dir'] = str(ROOT / 'jobs')
    config['agents'][0]['name'] = 'winning_adapter.harbor_cuppet_agent:CuppetAgent'
    config['agents'][0]['kwargs'].update(state_dir=str(state_dir), node_bin=str(wrapper))
    config_path = folder / 'config.json'
    save(config_path, config)
    home = Path(os.environ.get('CODEX_HOME', '/Users/addy/.codex'))
    profiles = {name: digest((home / name).read_bytes()) if (home / name).is_file() else None
                for name in ['config.toml', 'AGENTS.md', 'AGENTS.override.md']}
    state = dict(name=name, pid=os.getpid(), state='running', started_at=now(), historical_run=str(HISTORICAL),
                 source_revision=PIN, adapter_hashes=EXPECTED, harbor_version='0.25.0', node_version='v22.21.0',
                 model='gpt-6-luna', reasoning_effort='max', condition='original',
                 background_enrichment=False, native_tst_configured=False, headless_policy=True,
                 fresh_workspace=True, fresh_conversation=True, host_profile=str(home),
                 current_host_profile_hashes=profiles, historical_host_profile_hashes_available=False,
                 source_revision_at_historical_run_was_not_recorded=True,
                 job=f'jobs/{job_name}', state_dir=str(state_dir),
                 adapter_revision=subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
                 no_added_agent_or_wall_cap=True, host_process_cwd=str(HISTORICAL.parents[2]),
                 original_task_config=str(config['tasks'][0]['path']))
    latest = ROOT / 'reports/mvcc-winning-adapter-latest.json'

    def persist():
        state['updated_at'] = now()
        for path in [folder / 'status.json', latest]:
            temporary = path.with_suffix('.tmp')
            save(temporary, state)
            temporary.replace(path)

    env = dict(os.environ)
    for key in ['OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'DOCKER_DEFAULT_PLATFORM',
                'CUPPET_PROVIDER_BENCHMARK', 'CUPPET_EXECUTION_BENCHMARK_POLICY',
                'CUPPET_DYNAMIC_ENRICHMENT', 'CUPPET_PRERUN_ENRICHMENT',
                'CUPPET_MATCHED_TST_MODE', 'CUPPET_MATCHED_COMPARISON']:
        env.pop(key, None)
    env.update(DOCKER_HOST='unix:///Users/addy/.colima/benchmark/docker.sock',
               DOCKER_CONFIG='/Users/addy/Documents/Codex/2026-10-29/che/work/docker-config',
               UV_CACHE_DIR='/Users/addy/Documents/Codex/2026-10-29/che/work/uv-cache',
               UV_TOOL_DIR='/private/tmp/cuppet-winning-retry-tools',
               PYTHONPATH=str(ROOT), AGENTSTRESS_UNLIMITED='1',
               AGENTSTRESS_HEADLESS_TOOL_POLICY='1', CUPPET_WINNING_REPLICATION='1',
               CUPPET_REPLICATION_AUDIT_DIR=str(audit_dir), CUPPET_REPLICATION_WORKSPACE=str(workspace))
    persist()
    try:
        command = ['uvx', '--from', 'harbor==0.25.0', '--python', '3.14', 'harbor', 'run', '--config', str(config_path)]
        with (folder / 'harbor.log').open('w') as output:
            subprocess.run(command, cwd=HISTORICAL.parents[2], env=env, stdout=output, stderr=subprocess.STDOUT, check=True)
        paths = list((ROOT / 'jobs' / job_name).glob('*/result.json'))
        assert len(paths) == 1
        trial_path = paths[0].parent
        raw = json.loads(paths[0].read_text())
        agent = json.loads((attempt / 'agentstress.json').read_text())
        frames = [json.loads(line) for line in (audit_dir / 'model-context.jsonl').read_text().splitlines()]
        historical_frames = [json.loads(line) for line in (HISTORICAL / 'state/mvcc-lsm-compaction/original/data/model-context.jsonl').read_text().splitlines()]
        records = [json.loads(line) for line in (audit_dir / 'provider-inputs.jsonl').read_text().splitlines()]
        threads = [r for r in records if r['message']['method'] == 'thread/start']
        turns = [r for r in records if r['message']['method'] == 'turn/start']
        assert len(frames) == len(historical_frames) == len(threads) == len(turns) == 1
        assert frames[0]['messages'] == historical_frames[0]['messages'] and frames[0]['selection'] == historical_frames[0]['selection']
        params = threads[0]['message']['params']
        tools = [tool['name'] for tool in params['dynamicTools']]
        assert set(tools) == {'tst_explore', 'tst_read', 'tst_edit_batch', 'tst_validate', 'cuppet_execute'}
        assert params['model'] == 'gpt-6-luna' and params['config']['model_reasoning_effort'] == 'max'
        developer_hash = digest(params['developerInstructions'].encode())
        assert developer_hash == '2928f9bd0bc6e592859e4cb624bfdd86c3edc58c917da37af5642efe55a30837'
        initial = json.loads((audit_dir / 'initial-workspace.json').read_text())
        supplied = Path(config['tasks'][0]['path']) / 'environment/app'
        expected = [{'path': str(p.relative_to(supplied)), 'bytes': p.stat().st_size, 'sha256': digest(p.read_bytes())}
                    for p in sorted(supplied.rglob('*')) if p.is_file()]
        assert sorted(initial, key=lambda row: row['path']) == expected and len(initial) == 14
        assert agent['natural_completion'] and not agent.get('final_sync_error') and not agent.get('agent_error')
        reward = (raw.get('verifier_result') or {}).get('rewards', {}).get('reward')
        error = (raw.get('exception_info') or {}).get('exception_type')
        ctrf = json.loads((trial_path / 'verifier/ctrf.json').read_text())['results']
        execution = raw.get('agent_execution') or {}
        seconds = (datetime.fromisoformat(execution['finished_at']) - datetime.fromisoformat(execution['started_at'])).total_seconds()
        state.update(state='complete', finished_at=now(), reward=reward, error=error, trial=trial_path.name,
                     agent_seconds=seconds, bridge_seconds=agent['duration_ms']/1000,
                     primary_seconds=agent['provider_wall_clock_duration_ms']/1000,
                     official_tests=ctrf['summary'], failed_tests=[t['name'] for t in ctrf['tests'] if t['status']=='failed'],
                     usage=agent.get('provider_usage'), natural_completion=agent['natural_completion'],
                     historical_primary_messages_match=True, initial_workspace_verified=True,
                     captured_tools=tools, native_developer_hash=developer_hash,
                     historical_native_developer_capture_available=False,
                     primary_request_count=len(turns), initial_workspace=initial,
                     final_message=agent.get('final_message'), task_checksum=raw.get('task_checksum'))
        persist()
        save(ROOT / 'MVCC-WINNING-ADAPTER-RESULTS.json', state)
        lines = ['# MVCC winning-adapter retry', '',
                 f"Reward: **{reward}**; official tests: **{ctrf['summary']['passed']}/{ctrf['summary']['tests']}**; infrastructure error: **{error or 'none'}**.",
                 f"Agent time: {seconds:.2f}s; controller execution: {state['primary_seconds']:.2f}s. Natural completion: {agent['natural_completion']}.", '',
                 'One fresh Original-arm repair with the archived Python and JavaScript adapter files unchanged. No reviews, handoffs, previous implementation or grading feedback were given to the model.',
                 'GPT-6 Luna/max, Harbor 0.25.0, host Node v22.21.0, inherited host Codex profile, five tools, the archived short headless policy, TST unconfigured, background enrichment/orchestration/PE3 disabled. The original uncapped task configuration and Docker build overlay were reused.',
                 'Read-only instrumentation captured actual thread/start and turn/start requests and the broken starting workspace before the first model request. All 14 initial files matched; primary messages and selection matched the historical capture exactly. The five actual tool schemas and current native developer instructions were captured.',
                 'The passing historical run did not capture its native developer instructions, source revision or profile hashes. Those cannot be proven identical retrospectively; this run records the current source revision and profile hashes. Paths, session IDs and timestamps are necessarily fresh.', '',
                 f"Cuppet source: `{PIN}`. Original adapter hashes: `{EXPECTED}`.",
                 f"Raw evidence: `reports/{name}/status.json`, `reports/{name}/state/`, and `{state['job']}`.",
                 'Structured result: [MVCC-WINNING-ADAPTER-RESULTS.json](MVCC-WINNING-ADAPTER-RESULTS.json).']
        (ROOT / 'MVCC-WINNING-ADAPTER-RESULTS.md').write_text('\n'.join(lines)+'\n')
        print(json.dumps({key: state[key] for key in ['name','state','reward','official_tests','agent_seconds','historical_primary_messages_match']}), flush=True)
    except BaseException as error:
        state.update(state='needs_attention', error=str(error))
        persist()
        raise


if __name__ == '__main__':
    run()
