'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map(),requests=[];let historyReads=0;
const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:''});return nodes.get(id);};
const context=vm.createContext({AbortController,encodeURIComponent,Error,setTimeout,clearTimeout,
  document:{},$:node,esc:String,icon:()=>'',icons:()=>{},queueCollectionPoll:()=>{},render:()=>{},refreshMonitorHistory:()=>{historyReads++;},
  fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
});
const run=code=>vm.runInContext(code,context);
run("let S,mode='live',page='monitor',loadSequence=0; const pages={monitor:['监控中心'],leads:['需求筛选'],overview:['工作总览']},navPages=Object.keys(pages);");
const source=fs.readFileSync('static/app.js','utf8');
run(source.slice(source.indexOf('function renderNavigation()'),source.indexOf('async function api(')));
(async()=>{
  const first=run('load()');
  assert.match(node('#nav').innerHTML,/需求筛选/,'Navigation exists before the API responds');
  assert.match(node('#main').innerHTML,/正在加载监控中心/);
  assert.match(requests[0].url,/view=monitor/);
  assert.equal(historyReads,1,'Comment history starts alongside the initial state read');
  assert.ok(requests[0].options.signal,'A stalled initial read is bounded');
  run("page='leads'");const next=run('load()');
  requests[1].resolve({ok:true,json:async()=>({view:'leads',marker:'destination'})});await next;
  requests[0].reject(Error('late old-view failure'));await first;
  assert.equal(run('S.marker'),'destination','An old-page failure cannot replace the newly loaded view');
  const old=run('load()');run("page='monitor'");const newest=run('load()');
  requests[3].resolve({ok:true,json:async()=>({view:'monitor',marker:'monitor-now'})});await newest;
  requests[2].resolve({ok:true,json:async()=>({view:'leads',marker:'stale'})});await old;
  assert.equal(run('S.marker'),'monitor-now','An old-page result cannot overwrite current records');
  console.log('PASS: immediate navigation, scoped requests, bounded fetch and page result/error races. Synthetic only.');
})().catch(error=>{console.error(error);process.exitCode=1;});
