"""Replay the saved passing adapter's Original arm once, from broken code."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import re
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
CODEX = NODE.parent / 'codex'
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


def run(label='mvcc-winning-adapter', reset_evidence=None, native_tst=False):
    assert re.fullmatch(r'[a-z0-9-]+', label), 'Use a simple experiment label'
    reset = json.loads(Path(reset_evidence).read_text()) if reset_evidence else None
    if reset:
        assert reset['state'] == 'complete' and reset['new_docker_identity_verified']
        actual = subprocess.run(['docker', '--context', 'colima-benchmark', 'info', '--format', '{{.ID}}'], capture_output=True, text=True, check=True).stdout.strip()
        assert actual == reset['docker_after']['ID'], 'Docker changed after the recorded reset'
    assert subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, capture_output=True, text=True, check=True).stdout.strip() == PIN
    assert subprocess.run([NODE, '--version'], capture_output=True, text=True, check=True).stdout.strip() == 'v22.21.0'
    assert '0.162.1' in subprocess.run([CODEX, '--version'], capture_output=True, text=True, check=True).stdout
    for name, expected in EXPECTED.items():
        assert digest((ROOT / 'winning_adapter' / name).read_bytes()) == expected
        assert (ROOT / 'winning_adapter' / name).read_bytes() == (HISTORICAL / name).read_bytes()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'{label}-{stamp}'
    result_prefix = label.upper()
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
    config['agents'][0]['name'] = ('native_tst_adapter' if native_tst else 'winning_adapter') + '.harbor_cuppet_agent:CuppetAgent'
    if native_tst:
        assert digest((ROOT / 'native_tst_adapter/harbor_cuppet_agent.py').read_bytes()) == EXPECTED['harbor_cuppet_agent.py']
    config['agents'][0]['kwargs'].update(state_dir=str(state_dir), node_bin=str(wrapper))
    if reset:
        config.setdefault('environment', {}).setdefault('extra_docker_compose', []).append(str(ROOT / 'configs/mvcc-build-network.yaml'))
    config_path = folder / 'config.json'
    save(config_path, config)
    home = Path(os.environ.get('CODEX_HOME', '/Users/addy/.codex'))
    profiles = {name: digest((home / name).read_bytes()) if (home / name).is_file() else None
                for name in ['config.toml', 'AGENTS.md', 'AGENTS.override.md']}
    state = dict(name=name, pid=os.getpid(), state='running', started_at=now(), historical_run=str(HISTORICAL),
                 source_revision=PIN, adapter_hashes=EXPECTED, harbor_version='0.25.0', node_version='v22.21.0',
                 codex_version='0.162.1', codex_command=str(CODEX),
                 model='gpt-6-luna', reasoning_effort='max', condition='original',
                 background_enrichment=False, native_tst_configured=native_tst, headless_policy=True,
                 fresh_workspace=True, fresh_conversation=True, host_profile=str(home),
                 current_host_profile_hashes=profiles, historical_host_profile_hashes_available=False,
                 source_revision_at_historical_run_was_not_recorded=True,
                 job=f'jobs/{job_name}', state_dir=str(state_dir),
                 adapter_revision=subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(),
                 no_added_agent_or_wall_cap=True, host_process_cwd=str(HISTORICAL.parents[2]),
                 original_task_config=str(config['tasks'][0]['path']))
    state['experiment_label'] = label
    state['docker_reset'] = reset
    state['container_path_aliases_enabled'] = native_tst
    if reset:
        state['build_network_mode'] = 'default (build phase only; runtime networking unchanged)'
    previous_path = ROOT / ('MVCC-DOCKER-RESET-RESULTS.json' if native_tst else 'MVCC-WINNING-ADAPTER-RESULTS.json')
    if (reset or native_tst) and previous_path.is_file():
        previous_profiles = json.loads(previous_path.read_text())['current_host_profile_hashes']
        state['host_profile_matches_previous_retry'] = profiles == previous_profiles
        state['changed_host_profile_files'] = [key for key, value in profiles.items() if value != previous_profiles.get(key)]
    latest = ROOT / 'reports' / f'{label}-latest.json'

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
    env['PATH'] = str(NODE.parent) + os.pathsep + env['PATH']
    env['CUPPET_CODEX_BIN'] = str(CODEX)
    assert shutil.which('codex-app-server', path=env['PATH']) is None, 'Unexpected standalone app-server would bypass the pinned CLI'
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
        agent_path = attempt / 'agentstress.json'
        if not agent_path.is_file():
            state['infrastructure_failure'] = raw.get('exception_info')
            state['model_started'] = False
            persist()
            raise RuntimeError((raw.get('exception_info') or {}).get('exception_message') or 'Agent result missing before model execution')
        agent = json.loads(agent_path.read_text())
        frames = [json.loads(line) for line in (audit_dir / 'model-context.jsonl').read_text().splitlines()]
        historical_frames = [json.loads(line) for line in (HISTORICAL / 'state/mvcc-lsm-compaction/original/data/model-context.jsonl').read_text().splitlines()]
        records = [json.loads(line) for line in (audit_dir / 'provider-inputs.jsonl').read_text().splitlines()]
        threads = [r for r in records if r['message']['method'] == 'thread/start']
        turns = [r for r in records if r['message']['method'] == 'turn/start']
        assert len(frames) == len(historical_frames) == len(threads) == len(turns) == 1
        historical_match = frames[0]['messages'] == historical_frames[0]['messages']
        contexts = []
        if native_tst:
            expected_messages = json.loads(json.dumps(historical_frames[0]['messages']))
            for message in expected_messages:
                if message['role'] == 'system':
                    message['content'] = message['content'].replace('without a configured TST edit-batch daemon', 'with a configured native TST edit-batch daemon')
            contexts = [message for message in frames[0]['messages'] if message['role'] == 'system' and message['content'].startswith('<CUPPET_CONTEXT')]
            core = [message for message in frames[0]['messages'] if message not in contexts]
            assert core == expected_messages, 'Changes exceeded the TST availability sentence and derived TST context'
            native = agent['native_tst']
            assert native['before']['mode'] == native['after']['mode'] == 'managed-native'
            assert native['before']['connected'] and native['after']['connected']
            assert native['taskCalls'].get('prepareContext', 0) > 0
        else:
            assert historical_match
        assert frames[0]['selection'] == historical_frames[0]['selection']
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
                     historical_primary_messages_match=historical_match, initial_workspace_verified=True,
                     primary_task_and_policy_verified=True, initial_tst_context=contexts, native_tst=agent.get('native_tst'),
                     captured_tools=tools, native_developer_hash=developer_hash,
                     historical_native_developer_capture_available=False,
                     primary_request_count=len(turns), initial_workspace=initial,
                     final_message=agent.get('final_message'), task_checksum=raw.get('task_checksum'))
        if native_tst:
            aliases_path = audit_dir / 'container-path-aliases.jsonl'
            state['container_path_aliases'] = [json.loads(line) for line in aliases_path.read_text().splitlines()] if aliases_path.is_file() else []
            previous = json.loads(previous_path.read_text())
            previous_data = Path(previous['state_dir']) / 'mvcc-lsm-compaction/original/data/provider-inputs.jsonl'
            previous_records = [json.loads(line) for line in previous_data.read_text().splitlines()]
            previous_thread = next(record['message']['params'] for record in previous_records if record['message']['method'] == 'thread/start')
            assert params['dynamicTools'] == previous_thread['dynamicTools'], 'Tool schemas changed'
            assert params['developerInstructions'] == previous_thread['developerInstructions'], 'Native developer instructions changed'
            assert state['task_checksum'] == previous['task_checksum']
            state['tool_schemas_match_latest_without_tst'] = True
            state['native_developer_match_latest_without_tst'] = True
            state['tst_adapter_hashes'] = {name: digest((ROOT / 'native_tst_adapter' / name).read_bytes()) for name in ['cuppet_trial.mjs', 'native-tst.mjs', 'container-tool-paths.mjs']}
        persist()
        save(ROOT / f'{result_prefix}-RESULTS.json', state)
        lines = ['# MVCC with native TST after Docker reset' if native_tst else ('# MVCC after Docker VM reset' if reset else '# MVCC winning-adapter retry'), '',
                 f"Reward: **{reward}**; official tests: **{ctrf['summary']['passed']}/{ctrf['summary']['tests']}**; infrastructure error: **{error or 'none'}**.",
                 f"Agent time: {seconds:.2f}s; controller execution: {state['primary_seconds']:.2f}s. Natural completion: {agent['natural_completion']}.", '',
                 'One fresh Original-arm repair. The archived Python bridge is unchanged; native mode adds the product RuntimeTstManager, maps public /app filesystem arguments to the host mirror, and changes one TST availability sentence. No reviews, handoffs, previous implementation or grading feedback were given to the model.' if native_tst else 'One fresh Original-arm repair with the archived Python and JavaScript adapter files unchanged. No reviews, handoffs, previous implementation or grading feedback were given to the model.',
                 f"GPT-6 Luna/max, Harbor 0.25.0, host Node v22.21.0, inherited host Codex profile, five tools and the short headless policy. Native TST enabled: {native_tst}. Background enrichment/orchestration/PE3 disabled. The original uncapped task configuration was reused.",
                 'Read-only instrumentation captured actual thread/start and turn/start requests and all 14 broken starting files. Native mode verifies the original task, policy with one availability-sentence change, and separately records any TST-derived context. Tool schemas and native developer instructions match the latest TST-disabled run.' if native_tst else 'Read-only instrumentation captured actual thread/start and turn/start requests and the broken starting workspace before the first model request. All 14 initial files matched; primary messages and selection matched the historical capture exactly. The five actual tool schemas and current native developer instructions were captured.',
                 'The passing historical run did not capture its native developer instructions, source revision or profile hashes. Those cannot be proven identical retrospectively; this run records the current source revision and profile hashes. Paths, session IDs and timestamps are necessarily fresh.', '',
                 f"Cuppet source: `{PIN}`. Original adapter hashes: `{EXPECTED}`.",
                 f"Raw evidence: `reports/{name}/status.json`, `reports/{name}/state/`, and `{state['job']}`.",
                 f'Structured result: [{result_prefix}-RESULTS.json]({result_prefix}-RESULTS.json).']
        if reset:
            lines += ['', f"Docker profile benchmark was deleted with its data and recreated. Engine ID changed from `{reset['docker_before']['ID']}` to `{reset['docker_after']['ID']}`. VM resources were preserved: {reset['resources_preserved']}.",
                      'The Docker instance was already empty before reset; it was empty again after recreation. Other VM profiles and host-side benchmark evidence were preserved. Full reset evidence is embedded in the structured result.',
                      'Docker Compose 5.6/buildx 0.38 rejected the archived host-network build workaround. A build-only overlay switched image construction to the standard Docker network; task files, Dockerfile and runtime networking were unchanged. Setup-only failures were retained before any model execution.',
                      f"Host profile matched the preceding retry: {state.get('host_profile_matches_previous_retry')}; changed files: {state.get('changed_host_profile_files')}. The model and effort were explicitly requested and the original Codex 0.162.1 executable was pinned for this job."]
        if native_tst:
            lines += ['', f"Native daemon connected before and after model execution. TST provenance and actual call counts: `{state['native_tst']}`.", f"Host profile matched the latest TST-disabled run: {state.get('host_profile_matches_previous_retry')}.", 'The Docker VM was reused after the recorded reset, with a fresh task container, session, workspace and TST store.']
        (ROOT / f'{result_prefix}-RESULTS.md').write_text('\n'.join(lines)+'\n')
        print(json.dumps({key: state[key] for key in ['name','state','reward','official_tests','agent_seconds','historical_primary_messages_match']}), flush=True)
    except BaseException as error:
        state.update(state='needs_attention', error=str(error))
        persist()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='mvcc-winning-adapter')
    parser.add_argument('--reset-evidence', type=Path)
    parser.add_argument('--native-tst', action='store_true')
    args = parser.parse_args()
    run(args.label, args.reset_evidence, args.native_tst)
