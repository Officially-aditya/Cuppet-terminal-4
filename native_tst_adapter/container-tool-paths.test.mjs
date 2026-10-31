import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mapContainerToolArguments, wrapContainerToolExecution } from '/Users/addy/Documents/Benchmark/native_tst_adapter/container-tool-paths.mjs';
import { createNativeTst } from '/Users/addy/Documents/Benchmark/native_tst_adapter/native-tst.mjs';
import { MutationJournal } from '/Users/addy/Documents/Cuppet-desktop/src/runtime/mutation-journal.mjs';
import { TstBatchEditManager } from '/Users/addy/Documents/Cuppet-desktop/src/runtime/tst-edit-batches.mjs';

test('maps filesystem fields while preserving commands and source text', () => {
  const args = { path: '/app/src/a.cc', reads: [{ path: '/app/include/a.h' }], paths: ['/app/tests/a.cc'],
    prefix: '/app', command: 'cat /app/src/a.cc', old_text: '"/app/config"', new_text: '"/app/other"' };
  const mapped = mapContainerToolArguments(args);
  assert.equal(mapped.path, 'src/a.cc'); assert.equal(mapped.reads[0].path, 'include/a.h');
  assert.deepEqual(mapped.paths, ['tests/a.cc']); assert.equal(mapped.prefix, '');
  assert.equal(mapped.command, args.command); assert.equal(mapped.old_text, args.old_text); assert.equal(mapped.new_text, args.new_text);
  assert.equal(args.path, '/app/src/a.cc');
});

test('does not alias paths outside the container project', () => {
  for (const path of ['/app/../outside', '/app-other/private', '/etc/passwd']) {
    assert.equal(mapContainerToolArguments({path}).path, path);
  }
});

test('execution bridge preserves the original call and returned result', async () => {
  const original = {name: 'tst_read', arguments: '{"path":"/app/src/a.cc"}'};
  const result = {success: true}; const seen = []; const records = [];
  const run = wrapContainerToolExecution(async (call) => {seen.push(call); return result;}, async (record) => records.push(record));
  assert.equal(await run(original), result); assert.equal(JSON.parse(seen[0].arguments).path, 'src/a.cc');
  assert.equal(JSON.parse(original.arguments).path, '/app/src/a.cc'); assert.equal(records.length, 1);
});

for (const fixture of [
  {extension:'cc',before:'int answer() { return 42; }\n',after:'int answer() { return 43; }\n',indexed:false},
  {extension:'ts',before:'export const answer = 42;\n',after:'export const answer = 43;\n',indexed:true},
]) test(`real native editing preserves capability reporting for ${fixture.extension}`, async () => {
  const path = await mkdtemp(join(tmpdir(), 'cuppet-native-path-edit-'));
  const workspace = join(path, 'workspace'); const dataDir = join(path, 'data');
  const filename = `main.${fixture.extension}`;
  await mkdir(workspace); await mkdir(dataDir); await writeFile(join(workspace, filename), fixture.before);
  const database = {getSessionSummary: () => ({projectId:'p'}), getProject: () => ({id:'p',canonicalPath:workspace})};
  let native;
  try {
    native = await createNativeTst({root:'/Users/addy/Documents/Cuppet-desktop',dataDir,database,workspace});
    await native.warm({sessionId:'s',projectId:'p'});
    const journal = new MutationJournal(join(dataDir,'journal'));
    const edits = new TstBatchEditManager({tst:native.tst,journal});
    await native.tst.runWithProject({sessionId:'s',projectId:'p',projectRoot:workspace}, async () => {
      await assert.rejects(edits.prepare({sessionId:'s',projectRoot:workspace,operations:[{op:'replace_text',path:'/app/../outside',old_text:'a',new_text:'b'}]}),/escapes the project workspace/);
      const args = mapContainerToolArguments({operations:[{op:'replace_text',path:`/app/${filename}`,old_text:'42',new_text:'43'}]});
      const prepared = await edits.prepare({sessionId:'s',projectRoot:workspace,operations:args.operations});
      assert.equal(await readFile(join(workspace,filename),'utf8'),fixture.before);
      const applied = await edits.apply({batchId:prepared.id,sessionId:'s',projectRoot:workspace,executionId:'fixture',authorize:async()=>({allowed:true,source:'fixture'})});
      assert.equal(applied.applied,true);
      assert.equal(applied.files[0].parse.supported,fixture.indexed);
      assert.equal(applied.graphReady,fixture.indexed,JSON.stringify(applied));
      if (fixture.indexed) {
        assert.equal(applied.graphError,null); assert.equal(applied.refresh.paths[0].content_hash,applied.files[0].afterHash);
      } else {
        assert.match(applied.graphError,/different post-edit hashes/); assert.equal(applied.refresh.paths[0].content_hash,null);
      }
      assert.equal(await readFile(join(workspace,filename),'utf8'),fixture.after);
    });
    const counts = native.snapshot().taskCalls;
    assert.ok(counts.parseStaged > 0); assert.ok(counts.refreshGraphPaths > 0);
  } finally {if(native) await native.tst.close(); await rm(path,{recursive:true,force:true});}
});
