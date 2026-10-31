import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, copyFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const source = process.env.CUPPET_SOURCE || '/Users/addy/Documents/Cuppet-desktop';
const harness = resolve('scripts/cuppet-matched-control.mjs');

async function fixture(t) {
  const path = await mkdtemp(join(tmpdir(), 'cuppet-matched-test-'));
  t.after(() => rm(path, { recursive: true, force: true }));
  await symlink(join(source, 'src'), join(path, 'src'));
  await copyFile(harness, join(path, 'cuppet-matched-control.mjs'));
  return path;
}

function run(path, mode, code, enabled = true) {
  const env = { ...process.env, CUPPET_PROVIDER_BENCHMARK: enabled ? '1' : '0',
    CUPPET_MATCHED_COMPARISON: '1', CUPPET_MATCHED_TST_MODE: mode, CUPPET_DATA_DIR: path };
  delete env.CUPPET_TST_SOCKET;
  delete env.CUPPET_TST_TOKEN;
  return spawnSync(process.execPath, ['--input-type=module', '-e', code], { cwd: path, env, encoding: 'utf8' });
}

test('control cannot run outside the explicit benchmark', async (t) => {
  const path = await fixture(t);
  const result = run(path, 'unconfigured', "await import('./cuppet-matched-control.mjs')", false);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /restricted to the explicit benchmark/);
});

test('native arm leaves the existing TST behavior unchanged', async (t) => {
  const path = await fixture(t);
  const result = run(path, 'native', `
    import assert from 'node:assert/strict';
    const { RuntimeTstManager } = await import('./src/runtime/runtime-tst-manager.mjs');
    const before = RuntimeTstManager.prototype.graphWorkspace;
    await import('./cuppet-matched-control.mjs');
    assert.equal(RuntimeTstManager.prototype.graphWorkspace, before);
    const manager = new RuntimeTstManager({ manager: { configured: true, status: { mode: 'managed-native' } } });
    assert.equal(manager.configured, true);
    assert.equal(manager.status.mode, 'managed-native');
  `);
  assert.equal(result.status, 0, result.stderr);
});

test('control exposes real unavailable errors while preserving session dispatch and cleanup', async (t) => {
  const path = await fixture(t);
  const result = run(path, 'unconfigured', `
    import assert from 'node:assert/strict';
    const { RuntimeTstManager } = await import('./src/runtime/runtime-tst-manager.mjs');
    await import('./cuppet-matched-control.mjs');
    let closed = 0;
    const manager = new RuntimeTstManager({ manager: { configured: true, close: async () => { closed++; } } });
    assert.equal(manager.configured, false);
    assert.equal(manager.status.connected, false);
    assert.equal(await manager.supports('edit.resolve_targets'), false);
    await assert.rejects(manager.graphWorkspace(), /TST is not configured/);
    await assert.rejects(manager.prepareContext('s', 'q'), /TST is not configured/);
    assert.equal(await manager.runWithProject({ sessionId: 's' }, async () => 'ok'), 'ok');
    await manager.close();
    assert.equal(closed, 1);
  `);
  assert.equal(result.status, 0, result.stderr);
});

test('request audit preserves parameters and omits authentication calls', async (t) => {
  const path = await fixture(t);
  const result = run(path, 'native', `
    import assert from 'node:assert/strict';
    import { readFile } from 'node:fs/promises';
    const { CodexAppServerClient } = await import('./src/runtime/codex-app-server.mjs');
    const calls = [];
    CodexAppServerClient.prototype.request = function (...args) { calls.push(args); return Promise.resolve('ok'); };
    await import('./cuppet-matched-control.mjs');
    const client = new CodexAppServerClient({ command: 'unused' });
    const params = { model: 'gpt-6-luna', dynamicTools: [{ name: 'test' }] };
    assert.equal(await client.request('thread/start', params), 'ok');
    await client.request('account/login/start', { secret: 'not-logged' });
    assert.equal(calls[0][1], params);
    const audit = (await readFile('benchmark-provider-inputs.jsonl', 'utf8')).trim().split('\\n').map(JSON.parse);
    assert.equal(audit.length, 1);
    assert.deepEqual(audit[0].message.params, params);
    assert.equal(audit[0].message.method, 'thread/start');
  `);
  assert.equal(result.status, 0, result.stderr);
});
