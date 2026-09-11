import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {Miniflare,convertV4MiniflareOptions,Log,LogLevel} from 'miniflare';
import entry from './pages-entry/public/_worker.js';

test('entry rejects unsupported traffic without forwarding and does not expose upstream errors',async()=>{
  let calls=0;
  const env={RELAY:{fetch(){calls++;throw Error('private upstream detail');}}};
  for (const [url,method,status] of [
    ['http://entry.example/v1/pending','GET',400],
    ['https://entry.example/arbitrary','GET',404],
    ['https://entry.example/v1/pending','POST',404],
    ['https://entry.example/v1/pending?target=https://elsewhere.example','GET',400]
  ]) assert.equal((await entry.fetch(new Request(url,{method}),env)).status,status);
  const check=await entry.fetch(new Request('https://entry.example/connection-check'),env);
  assert.equal(check.headers.get('cache-control'),'no-store');
  assert.equal((await check.json()).service,'clubops-login-entry');
  assert.equal(calls,0);
  const unavailable=await entry.fetch(new Request('https://entry.example/v1/pending'),env);
  assert.equal(unavailable.status,503);
  assert.deepEqual(await unavailable.json(),{error:'relay_unavailable'});
  assert.equal(calls,1);
});

test('workerd: Pages binding reaches the original relay, preserves roles, and takes a code once',
  {timeout:45000},async t=>{
    const backend='pages_test_backend_'.padEnd(43,'b'),phone='pages_test_phone_'.padEnd(43,'p');
    const root=fileURLToPath(new URL('./',import.meta.url));
    const options=convertV4MiniflareOptions({workers:[
      {name:'pages-entry-test',modules:true,scriptPath:fileURLToPath(new URL('./pages-entry/public/_worker.js',import.meta.url)),
        compatibilityDate:'2026-09-10',serviceBindings:{RELAY:'relay-test'}},
      {name:'relay-test',modulesRoot:root,
        modules:['relay.mjs','iphone-client.mjs'].map(name=>({type:'ESModule',path:fileURLToPath(new URL(name,import.meta.url))})),
        compatibilityDate:'2026-09-10',bindings:{BACKEND_TOKEN:backend,PHONE_TOKEN:phone},
        durableObjects:{LOGIN_RELAY:{className:'LoginRelay',useSQLite:true}}}
    ],host:'127.0.0.1',port:0,cf:false,log:new Log(LogLevel.ERROR)});
    options.telemetry={enabled:false};
    const mf=new Miniflare(options);t.after(()=>mf.dispose());
    async function call(path,token,data){
      const response=await mf.dispatchFetch('https://entry.example'+path,{
        method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
        body:data===undefined?undefined:JSON.stringify(data)});
      return {status:response.status,data:await response.json(),cache:response.headers.get('cache-control')};
    }
    assert.equal((await call('/v1/pending','invalid')).status,401);
    assert.equal((await call('/v1/health',phone)).status,404);
    assert.equal((await call('/v1/pending',backend)).status,404);
    const health=await call('/v1/health',backend);
    assert.equal(health.status,200);assert.equal(health.cache,'no-store');
    const id='f'.repeat(32),code='012345';
    assert.equal((await call('/v1/login',backend,{id,account:'1267597446'})).status,201);
    assert.equal((await call('/v1/pending',phone)).data.id,id);
    assert.equal((await call('/v1/otp',phone,{id,code,received_at:Date.now()})).status,200);
    assert.equal((await call('/v1/otp',phone,{id,code,received_at:Date.now()})).status,409);
    const original=await mf.getWorker('relay-test');
    const response=await original.fetch('https://original.example/v1/take',{method:'POST',
      headers:{Authorization:'Bearer '+backend,'Content-Type':'application/json'},body:JSON.stringify({id})});
    assert.equal((await response.json()).code,code);
    assert.deepEqual((await call('/v1/take',backend,{id})).data,{status:'taken'});
    assert.equal((await call('/v1/cancel',backend,{id})).status,200);
    assert.equal((await call('/v1/pending',phone)).data.id,null);
    const installer=await mf.dispatchFetch('https://entry.example/iphone-script');
    assert.equal(installer.status,200);
    const text=await installer.text();
    assert.ok(text.includes('Script.setShortcutOutput'));
    assert.ok(!text.includes(backend)&&!text.includes(phone));
  });
