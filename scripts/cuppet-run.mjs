// Drive the same JSON protocol and runtime entry point used by the Electron app.
import { spawn, execFileSync } from 'node:child_process';
import { randomUUID, createHash } from 'node:crypto';
import { readFile, mkdir, writeFile, access, cp } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';

const [sourcePath, instructionPath, outputPath, projectPath, model, effort, timeout = '0', permission = 'full'] = process.argv.slice(2);
if (!sourcePath || !instructionPath || !outputPath || !projectPath || !model || !effort) {
  throw new Error('Expected source, instruction, output, project, model, effort, timeout and permission arguments.');
}
if (permission === 'full' && !await access('/.dockerenv').then(() => true, () => false)) {
  throw new Error('Full access is restricted to the disposable Docker task container.');
}
const source = resolve(sourcePath);
const { resolveManagedTstBinary } = await import(pathToFileURL(join(source, 'src/runtime/tst-supervisor.mjs')));
const nativeTst = resolveManagedTstBinary();
if (!nativeTst || execFileSync(nativeTst, ['--protocol'], { encoding: 'utf8', timeout: 10_000 }).trim() !== 'cuppet.tst.v3') {
  throw new Error('The matching native TST daemon is unavailable; no model task was started.');
}
const output = resolve(outputPath);
// TST's native store requires filesystem locking unavailable on the Mac's
// shared log mount. Keep live state on the container filesystem, then export it.
const dataDir = permission === 'full' ? join(tmpdir(), 'cuppet-task-data') : join(output, 'cuppet-data');
await mkdir(dataDir, { recursive: true, mode: 0o700 });
const instruction = await readFile(instructionPath, 'utf8');
const events = createWriteStream(join(output, 'cuppet-events.jsonl'), { mode: 0o600 });
const stderr = createWriteStream(join(output, 'cuppet-stderr.log'), { mode: 0o600 });
const dynamicEnabled = process.env.CUPPET_DYNAMIC_ENRICHMENT === '1';
const preRunEnabled = process.env.CUPPET_PRERUN_ENRICHMENT === '1';
if (dynamicEnabled && preRunEnabled) throw new Error('Select one enrichment mode');
const dynamicEvents = [];

const { listCodexModels } = await import(pathToFileURL(join(source, 'src/runtime/providers/backends/codex.mjs')));
const catalog = await listCodexModels();
const selected = catalog.models?.find((entry) => entry.id === model);
if (!catalog.loggedIn || !selected?.efforts?.includes(effort)) {
  throw new Error(`The signed-in Codex provider does not offer ${model} with ${effort} reasoning.`);
}

const child = spawn(process.execPath, [join(source, 'src/runtime/main.mjs')], {
  cwd: projectPath,
  env: {
    ...process.env,
    CUPPET_DATA_DIR: dataDir,
    CUPPET_NONINTERACTIVE: '1',
    CUPPET_PROVIDER_BENCHMARK: '1',
    CUPPET_EXECUTION_BENCHMARK_POLICY: 'optimized',
    CUPPET_ORCHESTRATOR: '0',
  },
  stdio: ['pipe', 'pipe', 'pipe'],
});
child.stderr.pipe(stderr);
const pending = new Map();
let sessionId;
let sample;
let settled = false;
let finish;
let fail;
const finished = new Promise((res, rej) => { finish = res; fail = rej; });
// Register the rejection handler before the first asynchronous runtime request.
finished.catch(() => {});
const lines = createInterface({ input: child.stdout });
lines.on('line', (line) => {
  let value;
  try { value = JSON.parse(line); } catch { return; }
  if (value.kind === 'response') {
    const request = pending.get(value.id);
    if (!request) return;
    pending.delete(value.id);
    clearTimeout(request.timer);
    if (value.ok) request.resolve(value.result);
    else request.reject(new Error(value.error || 'Cuppet request failed'));
  } else if (value.kind === 'event') {
    events.write(`${JSON.stringify(value.event)}\n`);
    const event = value.event;
    if (event.type?.startsWith('background.dynamic.')) dynamicEvents.push(event);
    if (event.type === 'runtime.benchmark.sample' && event.sessionId === sessionId) sample = event.sample;
    if (event.type === 'message.completed' && event.message?.sessionId === sessionId && !settled) {
      settled = true;
      finish(event.message);
    }
  }
});
child.on('error', (error) => fail(error));
child.on('exit', (code, signal) => {
  const error = new Error(`Cuppet runtime exited (${code ?? signal}). See cuppet-stderr.log.`);
  for (const request of pending.values()) { clearTimeout(request.timer); request.reject(error); }
  pending.clear();
  if (!settled) fail(error);
});
function request(method, params = {}, timeoutMs = 45_000) {
  const id = randomUUID();
  return new Promise((resolveRequest, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Cuppet request timed out: ${method}`)); }, timeoutMs);
    pending.set(id, { resolve: resolveRequest, reject, timer });
    child.stdin.write(`${JSON.stringify({ id, method, params })}\n`);
  });
}

// For real tasks Harbor owns the timeout, using each task's original budget.
const deadline = Number(timeout) > 0
  ? setTimeout(() => fail(new Error(`Cuppet preflight/task timed out after ${timeout}s`)), Number(timeout) * 1000)
  : null;
let result;
let preRun = null;
let primaryStartedAt = null;
try {
  await request('health');
  await request(dynamicEnabled ? 'background.resume' : 'background.pause');
  const project = await request('project.add-local', { path: resolve(projectPath), name: 'Terminal-Bench' });
  await request('tst.graph.locate', { projectId: project.id, pattern: '*', limit: 1 });
  if (!(await request('tst.status')).connected) throw new Error('Native TST did not connect; no model task was started.');
  const session = await request('session.create', { projectId: project.id });
  sessionId = session.id;
  await request('session.rename', { sessionId, title: 'Terminal-Bench task' });
  await request('session.auto.set', { sessionId, enabled: permission === 'full' ? 'full' : true });
  const provider = {
    providerID: 'codex', model, primaryEffort: effort,
    primary: { providerID: 'codex', modelID: model, variant: effort },
    secondary: { providerID: 'codex', modelID: model, variant: effort },
    secondaryEffort: effort,
  };
  if (preRunEnabled) {
    preRun = await request('background.prepare-repository', { sessionId, prompt: instruction, provider }, 300_000);
    await writeFile(join(output, 'pre-run-enrichment.json'), `${JSON.stringify(preRun, null, 2)}\n`, { mode: 0o600 });
    if (preRun.status !== 'completed' || !preRun.admittedKeys?.some((key) => preRun.retrievedKeys?.includes(key))) {
      throw new Error('The pre-run pass did not store and retrieve any repository memories; the primary was not started.');
    }
  }
  primaryStartedAt = Date.now();
  const accepted = await request('session.send', { sessionId, text: instruction, provider });
  if (accepted.sessionId !== sessionId) throw new Error('Cuppet unexpectedly routed the task to another session.');
  const message = await finished;
  const tst = await request('tst.status');
  result = {
    status: message.status,
    text: message.content,
    model,
    effort,
    projectRoot: project.canonicalPath,
    permission,
    tst,
    sample: sample ?? null,
  };
  await writeFile(join(output, 'cuppet-result.json'), `${JSON.stringify(result, null, 2)}\n`, { mode: 0o600 });
  if (message.status !== 'complete') throw new Error(`Cuppet task ${message.status}: ${message.content}`);
  process.stdout.write(`${JSON.stringify({ status: result.status, model, effort, tstMode: tst.mode })}\n`);
} finally {
  clearTimeout(deadline);
  if (sessionId && !settled) await request('session.stop', { sessionId }).catch(() => {});
  child.stdin.end();
  const killTimer = setTimeout(() => child.kill('SIGKILL'), 10_000);
  if (child.exitCode === null && child.signalCode === null) await new Promise((res) => child.once('exit', res));
  clearTimeout(killTimer);
  if (permission === 'full') await cp(dataDir, join(output, 'cuppet-data'), { recursive: true });
  if (result) {
    const runs = dynamicEvents.filter((event) => event.type === 'background.dynamic.completed');
    result.dynamicEnrichment = {
      enabled: dynamicEnabled,
      initialIntervalMs: Number(process.env.CUPPET_DYNAMIC_ENRICHMENT_INTERVAL_MS || 60000),
      events: dynamicEvents,
      usageComplete: runs.length === dynamicEvents.filter((event) => event.type === 'background.dynamic.running').length && runs.every((run) => run.status === 'completed'),
      usage: runs.reduce((sum, run) => {
        for (const [key, value] of Object.entries(run.usage ?? {})) if (typeof value === 'number') sum[key] = (sum[key] ?? 0) + value;
        return sum;
      }, {}),
    };
    result.source = JSON.parse(await readFile(join(source, 'benchmark-source.json'), 'utf8'));
    if (preRunEnabled) {
      const requests = (await readFile(join(dataDir, 'benchmark-provider-inputs.jsonl'), 'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
      const inputs = requests.filter((record) => record.message.method === 'turn/start');
      const primaryThread = requests.find((record) => record.message.method === 'thread/start' && record.message.params.dynamicTools?.length);
      const developerInstructions = primaryThread?.message.params.developerInstructions ?? '';
      const developerHash = createHash('sha256').update(developerInstructions).digest('hex');
      const textOf = (record) => (record.message.params.input ?? []).filter((item) => item.type === 'text').map((item) => item.text).join('\n');
      const primaryInputs = inputs.filter((record) => textOf(record).includes('<CUPPET_TOOL_POLICY'));
      const primaryInput = primaryInputs[0];
      const context = primaryInput ? (textOf(primaryInput).match(/<CUPPET_CONTEXT\b[\s\S]*?<\/CUPPET_CONTEXT>/g) ?? []).join('\n') : '';
      const matchedKeys = preRun.admittedKeys.filter((key) => context.includes(`- ${key}:`));
      const verified = inputs.length === 2 && primaryInputs.length === 1 && matchedKeys.length > 0 && primaryInput.at >= preRun.finishedAt;
      result.preRunEnrichment = {
        ...preRun,
        durationMs: preRun.finishedAt - preRun.startedAt,
        primaryStartedAt,
        initialContextVerified: verified,
        initialContext: context,
        matchedKeys,
        outboundModelRequests: inputs.length,
        primaryModelRequests: primaryInputs.length,
        primaryDeveloperInstructions: developerInstructions,
        developerInstructionsSha256: developerHash,
        developerInstructionsVerified: result.source.developer_instructions_sha256 ? developerHash === result.source.developer_instructions_sha256 : null,
        inputAuditFile: 'cuppet-data/benchmark-provider-inputs.jsonl',
      };
    }
    await writeFile(join(output, 'cuppet-result.json'), `${JSON.stringify(result, null, 2)}\n`, { mode: 0o600 });
    if (preRunEnabled && (!result.preRunEnrichment?.initialContextVerified || dynamicEvents.length || result.preRunEnrichment?.developerInstructionsVerified === false)) {
      throw new Error('The initial context audit did not verify exactly one preparation pass and no timed enrichment.');
    }
  }
  events.end();
  stderr.end();
}
