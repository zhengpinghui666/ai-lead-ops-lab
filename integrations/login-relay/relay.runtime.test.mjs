import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {Miniflare,convertV4MiniflareOptions,Log,LogLevel} from 'miniflare';
import phoneClient from '../iphone/forwarder-core.cjs';

test('workerd SQLite: simultaneous submissions/takes, restart persistence and cancellation',{timeout:45000},async t=>{
  const backend='runtime_backend_test_'.padEnd(43,'b'),phone='runtime_phone_test_'.padEnd(43,'p');
  const id='c'.repeat(32),other='d'.repeat(32),code='234567';
  const options=convertV4MiniflareOptions({
    name:'relay-runtime-test',modulesRoot:fileURLToPath(new URL('./',import.meta.url)),
    modules:['relay.mjs','iphone-client.mjs'].map(name=>({type:'ESModule',path:fileURLToPath(new URL(name,import.meta.url))})),
    compatibilityDate:'2026-09-10',host:'127.0.0.1',port:0,cf:false,
    log:new Log(LogLevel.ERROR),bindings:{BACKEND_TOKEN:backend,PHONE_TOKEN:phone},
    durableObjects:{LOGIN_RELAY:{className:'LoginRelay',useSQLite:true}},
  });
  options.telemetry={enabled:false};
  const mf=new Miniflare(options);t.after(()=>mf.dispose());
  const installer=await mf.dispatchFetch('https://relay.example/iphone-script');
  assert.equal(installer.status,200);
  const installerText=await installer.text();
  assert.ok(installerText.includes('Script.setShortcutOutput'));
  assert.ok(!installerText.includes(backend)&&!installerText.includes(phone));
  async function call(route,token=backend,data){
    const res=await mf.dispatchFetch('https://relay.example/v1/'+route,{
      method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},
      body:data===undefined?undefined:JSON.stringify(data)});
    return {status:res.status,value:await res.json()};
  }
  assert.equal((await call('health','invalid')).status,401);
  assert.equal((await call('health')).status,200);
  assert.equal((await call('login',backend,{id,account:'1267597446'})).status,201);
  assert.equal((await call('login',backend,{id:other,account:'1267597446'})).status,409);
  assert.equal((await call('pending',phone)).value.id,id);
  const submissions=await Promise.all(Array.from({length:6},()=>call('otp',phone,{id,code,received_at:Date.now()})));
  assert.equal(submissions.filter(x=>x.status===200).length,1);
  assert.equal(submissions.filter(x=>x.status===409).length,5);
  // Evict the live object, then prove the next instance reads encrypted storage.
  const ns=await mf.getDurableObjectNamespace('LOGIN_RELAY');
  await mf.unsafeEvictDurableObject('relay-runtime-test','LoginRelay',{id:ns.idFromName('single-account').toString()});
  const takes=await Promise.all(Array.from({length:8},()=>call('take',backend,{id})));
  assert.equal(takes.filter(x=>x.value.code===code).length,1);
  assert.equal(takes.filter(x=>x.value.status==='taken').length,7);
  assert.equal((await call('take',phone,{id})).status,404);
  assert.equal((await call('cancel',backend,{id})).status,200);
  assert.equal((await call('take',backend,{id})).status,409);
  assert.equal((await call('pending',phone)).value.id,null);
  assert.equal((await call('login',backend,{id:other,account:'1267597446'})).status,201);
  const phoneRequests=[];
  const forwarded=await phoneClient.forward('【抖音】验证码 012345，5分钟内有效。',
    {origin:'https://relay.example',account:'1267597446',authorization:'Bearer '+phone},
    {now:()=>Date.now(),admit:async()=>true,request:async(settings,path,body)=>{
      phoneRequests.push({path,body});
      const response=await call(path.replace('/v1/',''),settings.authorization.slice(7),body);
      return {status:response.status,data:response.value};
    }});
  assert.equal(forwarded.status,'received');
  assert.deepEqual(Object.keys(phoneRequests[1].body).sort(),['code','id','received_at']);
  assert.equal((await call('take',backend,{id:other})).value.code,'012345');
  assert.equal((await call('take',backend,{id:other})).value.status,'taken');
  assert.equal((await call('cancel',backend,{id:other})).status,200);
});
