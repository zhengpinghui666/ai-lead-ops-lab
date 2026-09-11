'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {frame,response,message,ROOM}=require('./test_live_protocol.cjs');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'clubops-live-worker-'));
const modulePath=path.join(directory,'fake-browser.cjs');
fs.writeFileSync(modulePath,`
const {EventEmitter}=require('node:events');
if(['http_idle','http_empty_budget','disconnect_budget'].includes(process.env.SCENARIO)){
 const nativeTimeout=setTimeout;global.setTimeout=(callback,ms,...args)=>nativeTimeout(callback,ms===10000?80:ms,...args);
}
const context=new EventEmitter();context.close=async()=>context.emit('close');
const page=new EventEmitter();page.url=()=> 'https://live.douyin.com/12345';page.locator=()=>({innerText:async()=>''});context.pages=()=>[page];
page.goto=async()=>{
 if(process.env.SCENARIO.startsWith('http')){
   setImmediate(()=>page.emit('response',{url:()=> 'https://live.douyin.com/webcast/im/fetch/?room_id=${ROOM}',status:()=>process.env.SCENARIO==='http_rejected'?403:200,
     headerValue:async()=>process.env.SCENARIO==='http_big'?String(3*1024*1024):null,
     body:async()=>{if(process.env.SCENARIO==='http_late'){process.stdout.write('{"type":"test_body_pending"}\\n');await new Promise(r=>setTimeout(r,100));}if(process.env.SCENARIO==='http_empty')setTimeout(()=>process.stdout.write('{"type":"test_empty_observed"}\\n'),50);return Buffer.from(process.env.POLL_PAYLOAD,'base64');}}));
   return;
 }
 page.emit('request',{});page.emit('requestfailed',{failure:()=>({errorText:'net::ERR_CONNECTION_CLOSED'})});page.emit('pageerror',new TypeError('private test content'));page.emit('response',{url:()=> 'https://live.douyin.com/webcast/im/fetch/?token=secret',status:()=>200});
 const unrelated=new EventEmitter();unrelated.url=()=> 'wss://other.example/socket.bad?token=secret';page.emit('websocket',unrelated);
 const makeSocket=()=>{const socket=new EventEmitter();socket.url=()=> 'wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?room_id=${ROOM}';return socket;};
 let ws=makeSocket();page.emit('websocket',ws);
 setImmediate(()=>{
  if(process.env.SCENARIO==='reconnect'){ws.emit('close');ws=makeSocket();page.emit('websocket',ws);}
  if(process.env.SCENARIO==='late_close'){
   const old=ws;ws=makeSocket();page.emit('websocket',ws);
   ws.emit('framereceived',{payload:Buffer.from(process.env.EMPTY_FRAME,'base64')});
   old.emit('close');old.emit('socketerror');old.emit('framereceived',{payload:Buffer.from(process.env.FRAME,'base64')});
  }
  if(process.env.SCENARIO==='invalid'){for(let i=0;i<10;i++)ws.emit('framereceived',{payload:Buffer.from([0])});}
  else if(process.env.SCENARIO==='close')context.emit('close');
  else {ws.emit('framereceived',{payload:Buffer.from(process.env.FRAME,'base64')});if(process.env.SCENARIO==='disconnect_budget')ws.emit('close');}
 });
};
module.exports={devices:{'Desktop Chrome':{userAgent:'synthetic desktop agent'}},chromium:{launchPersistentContext:async()=>context}};
`);
async function run(scenario,buffer){
 const child=spawn(process.execPath,['live_runner.cjs'],{cwd:__dirname,env:{...process.env,CLUBOPS_PLAYWRIGHT:modulePath,SCENARIO:scenario,FRAME:buffer.toString('base64'),POLL_PAYLOAD:buffer.toString('base64'),EMPTY_FRAME:frame([]).toString('base64')},stdio:['pipe','pipe','pipe']});
 let output='',error='',sentStop=false;child.stdout.on('data',x=>{output+=x;if(!sentStop&&(scenario==='stop'&&output.includes('"type":"message"')||scenario==='http_late'&&output.includes('test_body_pending')||scenario==='http_empty'&&output.includes('test_empty_observed'))){sentStop=true;child.stdin.write('{"command":"stop"}\n');}});child.stderr.on('data',x=>error+=x);
 const timer=setTimeout(()=>child.kill(),6000);
 child.stdin.write(JSON.stringify({room_url:'https://live.douyin.com/12345',profile_dir:directory,duration_seconds:10,max_messages:scenario==='stop'?10:1,interactive:false})+'\n');
 const code=await new Promise(resolve=>child.on('exit',resolve));clearTimeout(timer);
 assert.equal(code,0,error);const rows=output.trim().split('\n').map(JSON.parse);assert.equal(rows.at(-1).type,'final');return rows;
}
(async()=>{
 let rows=await run('normal',frame([message()]));assert.equal(rows.filter(r=>r.type==='message').length,1);assert.equal(rows.at(-1).status,'completed');
 const diagnostic=rows.find(r=>r.type==='diagnostic');assert.equal(diagnostic.page.navigation,'loaded');assert.equal(diagnostic.sockets.length,1);assert.equal(diagnostic.sockets[0].path,'/webcast/im/push/v2/');assert.ok(!JSON.stringify(diagnostic).includes('secret'));assert.ok(!JSON.stringify(diagnostic).includes('room_id'));
 assert.deepEqual(diagnostic.network,{requests:1,failures:{ERR_CONNECTION_CLOSED:1},script_errors:{TypeError:1},live_responses:{200:1},live_routes:{'/webcast/im/fetch/':1}});assert.ok(!JSON.stringify(diagnostic).includes('private test content'));
 rows=await run('ended',frame([message({method:'WebcastControlMessage'})]));assert.equal(rows.at(-1).status,'ended');assert.ok(!rows.some(r=>r.type==='message'));
 rows=await run('invalid',frame([]));assert.equal(rows.at(-1).status,'schema_changed');
 rows=await run('close',frame([]));assert.equal(rows.at(-1).status,'interrupted');
 rows=await run('reconnect',frame([message()]));assert.ok(rows.some(r=>r.status==='disconnected'));assert.ok(rows.some(r=>r.status==='reconnected'));assert.equal(rows.filter(r=>r.type==='message').length,1);
 rows=await run('late_close',frame([message()]));assert.ok(!rows.some(r=>r.status==='disconnected'));assert.equal(rows.filter(r=>r.type==='message').length,1);assert.equal(rows.at(-1).frames,2);
 rows=await run('stop',frame([message()]));assert.equal(rows.at(-1).status,'cancelled');assert.equal(rows.filter(r=>r.type==='message').length,1);assert.equal(rows.find(r=>r.type==='diagnostic').timestamps.seconds,1);
 rows=await run('http',response([message()]));assert.equal(rows.at(-1).status,'completed');assert.equal(rows.at(-1).frames,0);assert.equal(rows.filter(r=>r.type==='message').length,1);assert.equal(rows.find(r=>r.type==='stream').transport,'browser_http_poll');assert.equal(rows.find(r=>r.type==='diagnostic').poll.decoded,1);
 rows=await run('http_end',response([message({method:'WebcastControlMessage'})]));assert.equal(rows.at(-1).status,'ended');
 rows=await run('http_late',response([message()]));assert.equal(rows.at(-1).status,'cancelled');assert.equal(rows.filter(r=>r.type==='message').length,0,'A late HTTP body after stop is discarded');
 rows=await run('http_big',response([message()]));assert.equal(rows.at(-1).status,'resource_limited');assert.equal(rows.filter(r=>r.type==='message').length,0);
 rows=await run('http_rejected',response([message()]));assert.equal(rows.at(-1).status,'access_denied');assert.equal(rows.filter(r=>r.type==='message').length,0);
 rows=await run('http_idle',response([message({method:'WebcastMemberMessage'})]));assert.equal(rows.at(-1).status,'completed');assert.equal(rows.filter(r=>r.type==='message').length,0,'Healthy non-chat events keep a quiet room trackable');
 rows=await run('http_empty_budget',Buffer.alloc(0));assert.equal(rows.at(-1).status,'no_data','An empty body still cannot prove a healthy room');
 rows=await run('disconnect_budget',frame([message({method:'WebcastMemberMessage'})]));assert.equal(rows.at(-1).status,'disconnected','A connection that did not recover must not schedule another normal batch');
 for(const empty of [Buffer.alloc(0),require('node:zlib').gzipSync(Buffer.alloc(0))]){
   rows=await run('http_empty',empty);assert.equal(rows.at(-1).status,'cancelled');assert.ok(!rows.some(r=>r.type==='stream'||r.status==='running'),'Empty HTTP 200 is not a working live connection');assert.equal(rows.find(r=>r.type==='diagnostic').poll.empty_bodies,1);assert.equal(rows.find(r=>r.type==='diagnostic').poll.decoded,0);
 }
 console.log('PASS: live child-process budget, end-of-stream, parse failure, disconnect/reconnect, HTTP polling, late-body cancellation, response size/access limits and terminal settlement. Synthetic only.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{
 const resolved=path.resolve(directory),root=path.resolve(os.tmpdir())+path.sep;
 if(!resolved.startsWith(root)||!path.basename(resolved).startsWith('clubops-live-worker-'))throw Error('Unexpected cleanup path');
 fs.rmSync(resolved,{recursive:true,force:true});
});
