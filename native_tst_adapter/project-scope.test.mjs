import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, realpath, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ConversationDatabase } from '/Users/addy/Documents/Cuppet-desktop/src/runtime/database.mjs';
import { ProjectManager } from '/Users/addy/Documents/Cuppet-desktop/src/runtime/projects.mjs';
import { RuntimeService } from '/Users/addy/Documents/Cuppet-desktop/src/runtime/service.mjs';
import { createNativeTst } from './native-tst.mjs';
import { wrapContainerToolExecution } from './container-tool-paths.mjs';

test('pre-model guard rejects Git promotion to a parent repository', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'cuppet-parent-scope-'));
  const workspace = join(directory,'repo','nested'); const dataDir = join(directory,'data');
  await mkdir(workspace,{recursive:true});await mkdir(dataDir);
  execFileSync('git',['init','-q',join(directory,'repo')]);
  const db = new ConversationDatabase(join(dataDir,'db.sqlite3'));
  let native;
  try {
    const projects = new ProjectManager({db}); const project = await projects.addLocal({id:'p',path:workspace});
    assert.notEqual(project.canonicalPath,await realpath(workspace));
    db.createSession({id:'s',title:'fixture',projectId:project.id});
    native = await createNativeTst({root:'/Users/addy/Documents/Cuppet-desktop',dataDir,database:db,workspace});
    await assert.rejects(native.warm({sessionId:'s',projectId:project.id}),/Registered project root does not match/);
  } finally {if(native)await native.tst.close();db.close();await rm(directory,{recursive:true,force:true,maxRetries:20,retryDelay:50});}
});

test('isolated project executes container paths through the complete native tool runtime', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'cuppet-isolated-scope-'));
  const workspace = join(directory,'workspace');const dataDir = join(directory,'data');
  await mkdir(workspace);await mkdir(dataDir);await writeFile(join(workspace,'main.ts'),'export const answer = 42;\n');
  const databasePath = join(dataDir,'db.sqlite3'); const db = new ConversationDatabase(databasePath);
  let native,service,sessionId;let resolveFinished;const finished=new Promise(resolve=>{resolveFinished=resolve;});
  const calls=[];let failure;
  const noBackground=()=>({stats:{},ready:async()=>{},pause(){},foregroundStarted(){},foregroundIdle(){},setProviderConfig(){},recordTurn:async()=>{},flushNow:async()=>({status:'disabled'}),forgetSession(){},close:async()=>{}});
  try {
    native=await createNativeTst({root:'/Users/addy/Documents/Cuppet-desktop',dataDir,database:db,workspace});
    const adapter={async stream(messages,hooks){
      if(typeof hooks.executeTool!=='function')return {text:'Fixture',toolCalls:[],usage:null};
      const run=wrapContainerToolExecution(hooks.executeTool);
      try {
        const read=await run({id:'read',name:'tst_read',arguments:JSON.stringify({path:'/app/main.ts'})});calls.push(read);assert.equal(read.success,true,read.output);assert.match(read.output,/42/);
        const prepared=await run({id:'prepare',name:'tst_edit_batch',arguments:JSON.stringify({action:'prepare',operations:[{op:'replace_text',path:'/app/main.ts',old_text:'42',new_text:'43'}]})});calls.push(prepared);assert.equal(prepared.success,true,prepared.output);
        const batch=prepared.output.match(/batch_[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/)?.[0];assert.ok(batch,prepared.output);
        const applied=await run({id:'apply',name:'tst_edit_batch',arguments:JSON.stringify({action:'apply',batch_id:batch})});calls.push(applied);assert.equal(applied.success,true,applied.output);
      }catch(error){failure=error;}
      return {text:'fixture complete',toolCalls:[],usage:null};
    }};
    service=new RuntimeService({database:db,databasePath,dataDir,tst:native.tst,interactive:false,backgroundFactory:noBackground,providerFactory:()=>adapter,emit(event){if(event.type==='run.finished'&&event.sessionId===sessionId)resolveFinished();}});
    const project=await service.handle('project.add-local',{path:workspace});
    assert.equal(project.canonicalPath,await realpath(workspace));
    const session=await service.handle('session.create',{projectId:project.id});sessionId=session.id;
    await service.handle('session.auto.set',{sessionId,enabled:'full'});
    await service.handle('session.mode.set',{sessionId,mode:'build'});
    await native.warm({sessionId,projectId:project.id});
    await service.handle('session.send',{sessionId,text:'Disposable native tool fixture.',provider:{providerID:'codex',model:'gpt-6-luna',primary:{providerID:'codex',modelID:'gpt-6-luna',variant:'max'}}});
    await Promise.race([finished,new Promise((_,reject)=>setTimeout(()=>reject(new Error('fixture timed out')),15000).unref())]);
    if(failure)throw failure;
    assert.equal(calls.length,3);assert.equal(await readFile(join(workspace,'main.ts'),'utf8'),'export const answer = 43;\n');
    const status=native.snapshot();assert.equal(status.scopeVerified,true);assert.ok(status.taskCalls.parseStaged>0);assert.ok(status.taskCalls.refreshGraphPaths>0);
  }finally{if(service)await service.close();if(native)await native.tst.close();db.close();await rm(directory,{recursive:true,force:true,maxRetries:20,retryDelay:50});}
});
