// Read-only instrumentation for the unchanged passing adapter.
import { appendFileSync, existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

if (process.env.CUPPET_WINNING_REPLICATION !== '1') throw new Error('Explicit winning-adapter replication required');
const root = process.env.CUPPET_ROOT;
const auditDir = process.env.CUPPET_REPLICATION_AUDIT_DIR;
const workspace = process.env.CUPPET_REPLICATION_WORKSPACE;
if (!root || !auditDir || !workspace) throw new Error('Missing replication paths');
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');

function manifest(directory, prefix = '') {
  const rows = [];
  for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) rows.push(...manifest(join(directory, entry.name), path));
    else if (entry.isFile()) {
      const bytes = readFileSync(join(directory, entry.name));
      rows.push({ path, bytes: bytes.length, sha256: hash(bytes) });
    } else throw new Error(`Unexpected initial workspace entry: ${path}`);
  }
  return rows;
}

const { CodexAppServerClient } = await import(pathToFileURL(join(root, 'src/runtime/codex-app-server.mjs')));
const request = CodexAppServerClient.prototype.request;
CodexAppServerClient.prototype.request = function (method, params, ...rest) {
  if (method === 'thread/start' && !existsSync(join(auditDir, 'initial-workspace.json'))) {
    writeFileSync(join(auditDir, 'initial-workspace.json'), JSON.stringify(manifest(workspace), null, 2) + '\n', { mode: 0o600 });
  }
  const pending = request.call(this, method, params, ...rest);
  if (['thread/start', 'turn/start'].includes(method)) {
    appendFileSync(join(auditDir, 'provider-inputs.jsonl'),
      `${JSON.stringify({ at: Date.now(), node: process.version, message: { method, params } })}\n`, { mode: 0o600 });
  }
  return pending;
};
