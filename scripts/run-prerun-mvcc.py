"""Run MVCC after exactly one repository enrichment pass, with no timed updates."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('CUPPET_BENCHMARK_SETTINGS', str(ROOT / 'settings-mvcc-prerun.json'))
import benchmark


def run(preflight=False):
    config = benchmark.configuration('cuppet', preflight=preflight)
    settings = benchmark.SETTINGS
    label = settings.get('run_label', 'mvcc-prerun')
    kwargs = config['agents'][0]['kwargs']
    assert kwargs['pre_run_enrichment'] and not kwargs['dynamic_enrichment']
    assert config['agents'][0]['model_name'] == 'gpt-6-luna' and kwargs['reasoning_effort'] == 'max'
    if preflight:
        config['tasks'] = [{'path': str(ROOT / 'preflight-dynamic')}]
    else:
        assert [Path(task['path']).name for task in config['tasks']] == ['mvcc-lsm-compaction']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f"{'preflight' if preflight else 'terminal-bench-4'}-cuppet-{label}-{stamp}"
    config['job_name'] = name
    config['quiet'] = True
    folder = ROOT / 'reports' / name
    latest = ROOT / 'reports' / f"{label}-{'preflight' if preflight else 'latest'}.json"
    state = {'pid': os.getpid(), 'name': name, 'state': 'running', 'preflight': preflight,
             'started_at': datetime.now(timezone.utc).isoformat(), 'job': f'jobs/{name}',
             'settings': settings, 'config': f'reports/{name}/config.json', 'log': f'reports/{name}.log'}

    def persist():
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        benchmark.save(folder / 'status.json', state)
        temporary = latest.with_suffix('.tmp')
        benchmark.save(temporary, state)
        temporary.replace(latest)

    benchmark.save(folder / 'config.json', config)
    persist()
    try:
        job = benchmark.run_job(config, benchmark.docker_environment())
        trials = list(job.glob('*/result.json'))
        if len(trials) != 1:
            raise RuntimeError(f'Expected exactly one final trial, found {len(trials)}')
        result = json.loads(trials[0].read_text())
        summary = json.loads((trials[0].parent / 'agent' / 'cuppet-result.json').read_text())
        preparation = summary.get('preRunEnrichment') or {}
        if not preparation.get('initialContextVerified') or summary.get('dynamicEnrichment', {}).get('events'):
            raise RuntimeError('Initial input did not prove one preparation pass with no timed enrichment')
        if settings.get('developer_instructions_sha256') and not preparation.get('developerInstructionsVerified'):
            raise RuntimeError('The captured developer instructions did not match the requested prompt')
        if summary['source']['cuppet_revision'] != settings['cuppet_revision'] or summary['source']['system_prompt_sha256'] != settings['system_prompt_sha256']:
            raise RuntimeError('The source or updated system prompt did not match the pinned configuration')
        reward = (result.get('verifier_result') or {}).get('rewards', {}).get('reward')
        error = (result.get('exception_info') or {}).get('exception_type')
        execution = result.get('agent_execution') or {}
        elapsed = lambda start, finish: (datetime.fromisoformat(finish.replace('Z', '+00:00')) - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds()
        seconds = elapsed(execution['started_at'], execution['finished_at']) if execution.get('finished_at') else None
        state.update(state='complete', finished_at=datetime.now(timezone.utc).isoformat(), reward=reward, error=error,
                     trial=trials[0].parent.name, agent_execution_seconds=seconds,
                     primary_execution_ms=(summary.get('sample') or {}).get('elapsedMs'),
                     foreground_usage=(summary.get('sample') or {}).get('usage'),
                     preparation=preparation, source=summary['source'], tst=summary['tst'])
        if preflight and (reward != 1 or error or not summary['tst']['connected']):
            raise RuntimeError('The isolated pre-run enrichment fixture did not pass')
        persist()
        if not preflight:
            lines = [settings.get('report_title', '# MVCC: one enrichment pass before execution'), '',
                     f"GPT-6 Luna/max, source `{settings['cuppet_revision']}`; the updated system prompt is preserved.", '',
                     f"- Final reward: {reward}; infrastructure error: {error or 'none'}.",
                     f'- Total agent execution, including preparation: {seconds:.2f} seconds.',
                     f"- Repository preparation: {preparation['durationMs'] / 1000:.2f} seconds.",
                     f"- Primary execution: {state['primary_execution_ms'] / 1000:.2f} seconds.",
                     f"- Supplied files read: {len(preparation['files'])}; omitted: {len(preparation['omitted'])}.",
                     f"- Admitted memories: {preparation['candidates']}; keys present in the initial request: {len(preparation['matchedKeys'])}.",
                     '- Exactly one preparation phase; no periodic enrichment.',
                     '- Initial model input was captured and verified before interpreting the grade.', '',
                     f"- Requested developer instructions verified: {preparation.get('developerInstructionsVerified')}.", '',
                     f"Evidence: `{state['job']}` and `reports/{name}/status.json`.", '',
                     'This is a separate fresh attempt. Previous dynamic-enrichment results remain unchanged.']
            (ROOT / settings.get('result_report', 'MVCC-PRERUN-RESULTS.md')).write_text('\n'.join(lines) + '\n')
        print(json.dumps({'name': name, 'reward': reward, 'error': error, 'agent_execution_seconds': seconds,
                          'preparation_seconds': preparation['durationMs'] / 1000,
                          'initial_memory_keys': preparation['matchedKeys'], 'timed_enrichment': False}), flush=True)
    except BaseException as error:
        state.update(state='needs_attention', error=str(error))
        persist()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true')
    run(preflight=parser.parse_args().preflight)
