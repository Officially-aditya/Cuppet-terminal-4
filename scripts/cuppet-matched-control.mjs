// Benchmark-only control. The same preloader runs in both arms; native leaves
// the pinned runtime untouched. The other arm delegates its TST interface to
// Cuppet's existing unconfigured TstBridge, retaining normal tool fallbacks.
import { appendFileSync } from 'node:fs';
import { join } from 'node:path';

if (process.env.CUPPET_PROVIDER_BENCHMARK !== '1' || process.env.CUPPET_MATCHED_COMPARISON !== '1') {
  throw new Error('Matched control is restricted to the explicit benchmark harness');
}
if (process.env.CUPPET_TST_SOCKET || process.env.CUPPET_TST_TOKEN) throw new Error('External TST overrides are forbidden in matched runs');
const mode = process.env.CUPPET_MATCHED_TST_MODE;
if (!['native', 'unconfigured'].includes(mode)) throw new Error('Unknown matched control mode');

const [{ RuntimeTstManager }, { TstBridge }, { CodexAppServerClient }] = await Promise.all([
  import(new URL('./src/runtime/runtime-tst-manager.mjs', import.meta.url)),
  import(new URL('./src/runtime/tst-client.mjs', import.meta.url)),
  import(new URL('./src/runtime/codex-app-server.mjs', import.meta.url)),
]);

if (mode === 'unconfigured') {
  const bridge = new TstBridge({ socketPath: '', token: '' });
  const prototype = RuntimeTstManager.prototype;
  Object.defineProperty(prototype, 'configured', { configurable: true, get: () => false });
  Object.defineProperty(prototype, 'status', { configurable: true, get: () => ({
    ...bridge.status, mode: 'unconfigured-control', managed: false, projects: [],
  }) });
  for (const name of Object.getOwnPropertyNames(TstBridge.prototype)) {
    if (['constructor', 'configured', 'status', 'close'].includes(name)) continue;
    if (typeof Object.getOwnPropertyDescriptor(TstBridge.prototype, name)?.value !== 'function') continue;
    Object.defineProperty(prototype, name, { configurable: true, writable: true,
      value: function (...args) { return bridge[name](...args); } });
  }
  prototype.forProject = async () => bridge;
  prototype.forProjectRoot = async () => bridge;
  prototype.unregisterProject = async () => ({ closed: false });
  const close = prototype.close;
  prototype.close = async function () { bridge.close(); return close.call(this); };
}

// Capture the actual app-server request parameters without changing them.
// Account/auth requests and tool replies are deliberately excluded.
const request = CodexAppServerClient.prototype.request;
CodexAppServerClient.prototype.request = function (method, params, ...rest) {
  const pending = request.call(this, method, params, ...rest);
  if (['thread/start', 'turn/start'].includes(method)) {
    appendFileSync(join(process.env.CUPPET_DATA_DIR, 'benchmark-provider-inputs.jsonl'),
      `${JSON.stringify({ at: Date.now(), message: { method, params } })}\n`, { mode: 0o600 });
  }
  return pending;
};
