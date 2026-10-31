import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function createNativeTst({ root, dataDir, database, workspace }) {
  const { RuntimeTstManager } = await import(pathToFileURL(join(root, 'src/runtime/runtime-tst-manager.mjs')));
  const binaryPath = join(root, 'vendor/tst/darwin-arm64/tst-daemon');
  const manifest = JSON.parse(await readFile(join(root, 'vendor/tst/darwin-arm64/tst-runtime.json'), 'utf8'));
  const sha256 = createHash('sha256').update(await readFile(binaryPath)).digest('hex');
  if (manifest.sourceRevision !== '9f2a8ae63555b7534ee1ccfe211efce1ad46f630' || sha256 !== manifest.sha256) throw new Error('Native TST binary does not match the pinned manifest');
  if (execFileSync(binaryPath, ['--protocol'], { encoding: 'utf8', timeout: 5000 }).trim() !== 'cuppet.tst.v3') throw new Error('Wrong native TST protocol');
  const manager = new RuntimeTstManager({
    binaryPath,
    dataDir: join(dataDir, 'tst'),
    resolveProjectForSession: (sessionId) => {
      const session = database.getSessionSummary(sessionId);
      const project = session?.projectId ? database.getProject(session.projectId) : null;
      return project?.canonicalPath ? { projectId: project.id, projectRoot: project.canonicalPath } : null;
    },
  });
  const calls = {};
  const tst = new Proxy(manager, {
    get(target, key) {
      const value = Reflect.get(target, key, target);
      if (typeof value !== 'function') return value;
      return (...args) => {
        calls[key] = (calls[key] || 0) + 1;
        return value.apply(target, args);
      };
    },
  });
  let before = null;
  let beforeCalls = {};
  const binary = { path: binaryPath, sha256, sourceRevision: manifest.sourceRevision, protocol: manifest.protocol };
  return {
    tst,
    async warm({ sessionId, projectId }) {
      await tst.runWithProject({ sessionId, projectId, projectRoot: workspace }, async () => {
        await tst.graphWorkspace(1);
        if (!await tst.supports('edit.parse_staged', sessionId)) throw new Error('Native edit capability missing');
      });
      before = JSON.parse(JSON.stringify(manager.status));
      if (before.mode !== 'managed-native' || !before.connected) throw new Error('TST is not connected to its native daemon');
      beforeCalls = { ...calls };
    },
    snapshot() {
      return {
        binary, before, after: JSON.parse(JSON.stringify(manager.status)), calls: { ...calls },
        taskCalls: Object.fromEntries(Object.entries(calls).map(([key, value]) => [key, value - (beforeCalls[key] || 0)])),
      };
    },
  };
}
