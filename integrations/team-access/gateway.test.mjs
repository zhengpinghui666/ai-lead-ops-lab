import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {gzipSync} from 'node:zlib';
const require=createRequire(new URL('../login-relay/package.json',import.meta.url));
const {Miniflare,convertV4MiniflareOptions,Log,LogLevel}=require('miniflare');
const origin='https://clubops-team-123abc.pages.dev',secret='synthetic_connector_token_'.padEnd(48,'x');
test('public entry, authenticated outbound connection, unchanged content and bounded forwarding',{timeout:45000},async t=>{
  const options=convertV4MiniflareOptions({name:'team-gateway-test',modulesRoot:fileURLToPath(new URL('./',import.meta.url)),
    modules:[{type:'ESModule',path:fileURLToPath(new URL('gateway.mjs',import.meta.url))}],compatibilityDate:'2026-09-11',
    host:'127.0.0.1',port:0,cf:false,log:new Log(LogLevel.ERROR),bindings:{PUBLIC_ORIGIN:origin,CONNECTOR_TOKEN:secret},
    durableObjects:{TEAM:{className:'TeamGateway',useSQLite:true}}});options.telemetry={enabled:false};
  const mf=new Miniflare(options);t.after(()=>mf.dispose());
  assert.equal((await mf.dispatchFetch('https://wrong.example/')).status,403);
  assert.equal((await mf.dispatchFetch(origin+'/')).status,503);
  assert.equal((await mf.dispatchFetch(origin+'/_access/connect',{headers:{Upgrade:'websocket'}})).status,401);
  assert.equal((await mf.dispatchFetch(origin+'/api/service')).status,404);
  assert.equal((await mf.dispatchFetch(origin+'/api/monitor-stop',{method:'POST'})).status,403);
  const connection=await mf.dispatchFetch(origin+'/_access/connect',{headers:{Upgrade:'websocket',Authorization:'Bearer '+secret}});
  assert.equal(connection.status,101);const ws=connection.webSocket;ws.accept();t.after(()=>ws.close());
  assert.equal((await mf.dispatchFetch(origin+'/_access/status')).status,200);
  assert.equal((await mf.dispatchFetch(origin+'/_access/connect',{headers:{Upgrade:'websocket',Authorization:'Bearer '+secret}})).status,409);
  const requests=[];
  ws.addEventListener('message',event=>{if(event.data==='pong')return;const req=JSON.parse(event.data);requests.push(req);
    if(new URL(req.path,origin).searchParams.has('hang'))return;
    const codec=new URL(req.path,origin).searchParams.get('codec');
    if(codec){const raw=Buffer.from(codec==='large'?'x'.repeat(64*1024*1024+1):codec==='archive'?'x'.repeat(26*1024*1024):'压缩后的相同工作台内容'.repeat(1000));
      ws.send(JSON.stringify({id:req.id,status:200,content_type:'text/plain; charset=utf-8',body_encoding:codec==='unknown'?'br':'gzip',body:(codec==='invalid'?Buffer.from('invalid gzip'):gzipSync(raw)).toString('base64')}));return;}
    ws.send(JSON.stringify({id:req.id,status:200,content_type:req.path==='/'?'text/html; charset=utf-8':'application/json',
      body:Buffer.from(req.path==='/'?'<!doctype html><h1>现有工作台</h1>':JSON.stringify({fixture:true})).toString('base64')}));
  });
  const page=await mf.dispatchFetch(origin+'/');assert.equal(page.status,200);assert.equal(await page.text(),'<!doctype html><h1>现有工作台</h1>');
  assert.equal(page.headers.get('set-cookie'),null);
  const compressed=await mf.dispatchFetch(origin+'/api/state?codec=gzip');assert.equal(compressed.status,200);assert.equal(await compressed.text(),'压缩后的相同工作台内容'.repeat(1000));
  const archive=await mf.dispatchFetch(origin+'/api/state?codec=archive');assert.equal(archive.status,200);assert.equal((await archive.text()).length,26*1024*1024);
  for(const codec of ['large','invalid','unknown'])assert.equal((await mf.dispatchFetch(origin+'/api/state?codec='+codec)).status,502);
  assert.equal((await mf.dispatchFetch(origin+'/api/live-history?limit=25')).status,200);
  assert.equal(requests.at(-1).path,'/api/live-history?limit=25');
  const result=await mf.dispatchFetch(origin+'/api/monitor-stop?mode=live',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','X-ClubOps-Token':'test_csrf'},body:'{}'});
  assert.equal(result.status,200);assert.equal(requests.at(-1).headers['x-clubops-token'],'test_csrf');
  assert.equal(requests.at(-1).path,'/api/monitor-stop?mode=live');
  assert.equal((await mf.dispatchFetch(origin+'/api/service-stop',{method:'POST',headers:{Origin:origin}})).status,404);
  assert.equal((await mf.dispatchFetch(origin+'/api/monitor-stop',{method:'POST',headers:{Origin:origin},body:'x'.repeat(262145)})).status,413);
  assert.ok(!JSON.stringify(requests).includes(secret));
  // A normal browser needs no gateway session or login cookie.
  assert.deepEqual(await (await mf.dispatchFetch(origin+'/_access/status')).json(),{online:true,access:'public',login_required:false});
  // An open but nonresponsive old connection must not hold the only computer
  // slot forever. A timed-out write stays unknown and is never replayed.
  const count=requests.length;
  const hanging=await mf.dispatchFetch(origin+'/api/monitor-stop?hang=1',{method:'POST',headers:{Origin:origin},body:'{}'});
  assert.equal(hanging.status,504);assert.match((await hanging.json()).error,/不要重复提交/);
  assert.equal(requests.length,count+1);
  assert.equal((await (await mf.dispatchFetch(origin+'/_access/status')).json()).online,false);
  const replacement=await mf.dispatchFetch(origin+'/_access/connect',{headers:{Upgrade:'websocket',Authorization:'Bearer '+secret}});
  assert.equal(replacement.status,101);replacement.webSocket.accept();t.after(()=>replacement.webSocket.close());
  assert.equal(requests.length,count+1,'Reconnection must not replay the timed-out write');
});
