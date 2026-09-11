import test from 'node:test';
import assert from 'node:assert/strict';
import worker, {LoginRelay} from './relay.mjs';

const BACKEND='backend_test_'.padEnd(43,'b'), PHONE='phone_test_'.padEnd(43,'p');
const ID='a'.repeat(32), OTHER='b'.repeat(32), ACCOUNT='1267597446', CODE='135790';

class Storage {
  map=new Map(); alarm=null; tail=Promise.resolve();
  async get(key){return structuredClone(this.map.get(key));}
  async put(key,value){this.map.set(key,structuredClone(value));}
  async delete(key){return this.map.delete(key);}
  async setAlarm(value){this.alarm=value;}
  async deleteAlarm(){this.alarm=null;}
  transaction(fn){
    // Deliberately provide only transaction methods documented by Cloudflare.
    const tx={get:this.get.bind(this),put:this.put.bind(this),delete:this.delete.bind(this)};
    const next=this.tail.then(()=>fn(tx));this.tail=next.catch(()=>{});return next;
  }
}
function fixture(){
  const storage=new Storage(),env={BACKEND_TOKEN:BACKEND,PHONE_TOKEN:PHONE};
  const object=new LoginRelay({storage},env);let calls=0;
  env.LOGIN_RELAY={idFromName:name=>{assert.equal(name,'single-account');return 'one';},get:()=>({fetch:req=>{calls++;return object.fetch(req);}})};
  async function call(route,token=BACKEND,data,options={}){
    const headers={'Authorization':'Bearer '+token,...options.headers};
    const body=options.raw??(data===undefined?undefined:JSON.stringify(data));
    if(body!==undefined)headers['Content-Type']??='application/json';
    const request=new Request((options.origin||'https://relay.example')+'/v1/'+route,
      {method:options.method||(body===undefined?'GET':'POST'),headers,body});
    const response=await worker.fetch(request,env);
    assert.equal(response.headers.get('cache-control'),'no-store');
    return {status:response.status,value:await response.json()};
  }
  return {storage,env,object,call,calls:()=>calls};
}
async function create(f,id=ID){return f.call('login',BACKEND,{id,account:ACCOUNT});}
async function submit(f,code=CODE){return f.call('otp',PHONE,{id:ID,code,received_at:Date.now()});}

test('HTTPS, configuration and strict bearer auth reject before storage',async()=>{
  const f=fixture();
  assert.equal((await f.call('health',BACKEND,undefined,{origin:'http://relay.example'})).status,400);
  assert.equal((await f.call('health','wrong')).status,401);
  assert.equal((await f.call('health',BACKEND,undefined,{headers:{Authorization:BACKEND}})).status,401);
  assert.equal(f.calls(),0);
  f.env.PHONE_TOKEN=BACKEND;
  assert.equal((await f.call('health')).status,503);
  assert.equal(f.calls(),0);
});

test('separate credentials expose only the intended routes',async()=>{
  const f=fixture();
  assert.equal((await f.call('health')).value.service,'clubops-login-relay');
  assert.equal((await f.call('health',PHONE)).status,404);
  assert.equal((await f.call('login',PHONE,{id:ID,account:ACCOUNT})).status,404);
  await create(f);
  assert.equal((await f.call('pending',BACKEND)).status,404);
  assert.equal((await f.call('take',PHONE,{id:ID})).status,404);
  assert.equal((await f.call('cancel',PHONE,{id:ID})).status,404);
  assert.equal((await f.call('otp',BACKEND,{id:ID,code:CODE,received_at:Date.now()})).status,404);
  const pending=(await f.call('pending',PHONE)).value;
  assert.deepEqual(Object.keys(pending).sort(),['account','created_at','expires_at','id']);
  assert.equal(pending.account,ACCOUNT);
});

test('one task, idempotent creation and explicit bounded body validation',async()=>{
  const f=fixture();
  for(const data of [{id:ID,account:1267597446},{id:12,account:ACCOUNT},{id:ID,account:ACCOUNT,secret:CODE},[]])
    assert.equal((await f.call('login',BACKEND,data)).status,400);
  for(const raw of ['{','x'.repeat(3000),'null'])
    assert.equal((await f.call('login',BACKEND,undefined,{raw})).status,400);
  assert.equal((await f.call('login',BACKEND,{id:ID,account:ACCOUNT},{headers:{'Content-Type':'text/plain'}})).status,400);
  assert.equal((await f.call('health?token=x')).status,400);
  const created=await create(f),repeat=await create(f);
  assert.equal(created.status,201);assert.equal(repeat.status,200);
  assert.deepEqual(created.value,repeat.value);
  const job=await f.storage.get('job');assert.equal(job.expires_at-job.created_at,180000);
  assert.equal(f.storage.alarm,job.expires_at);
  assert.equal((await create(f,OTHER)).status,409);
});

test('code is encrypted while waiting and delivered at most once',async()=>{
  const f=fixture();await create(f);
  assert.deepEqual((await f.call('take',BACKEND,{id:ID})).value,{status:'waiting'});
  assert.equal((await submit(f)).status,200);
  const job=await f.storage.get('job');
  assert.ok(job.cipher.data.length>CODE.length);
  assert.equal(JSON.stringify(job).includes(CODE),false);
  assert.equal((await submit(f,'246802')).status,409);
  assert.deepEqual((await f.call('pending',PHONE)).value,{id:null});
  const responses=await Promise.all(Array.from({length:8},()=>f.call('take',BACKEND,{id:ID})));
  assert.equal(responses.filter(r=>r.value.code===CODE).length,1);
  assert.equal(responses.filter(r=>r.value.status==='taken').length,7);
  assert.equal((await f.storage.get('job')).cipher,undefined);
});

test('wrong task, old timestamp, future timestamp and malformed code are rejected',async()=>{
  const f=fixture();await create(f);const job=await f.storage.get('job');
  const send=changes=>f.call('otp',PHONE,{id:ID,code:CODE,received_at:Date.now(),...changes});
  assert.equal((await send({id:OTHER})).status,409);
  assert.equal((await send({received_at:job.created_at-1})).status,409);
  assert.equal((await send({received_at:Date.now()+60000})).status,409);
  for(const code of ['123','123456789','abc123',135790])assert.equal((await send({code})).status,400);
  assert.equal((await send({received_at:'2026-09-10'})).status,400);
  assert.equal((await send({message:'unneeded full SMS'})).status,400);
  assert.equal((await f.storage.get('job')).status,'waiting');
});

test('expired codes cannot be taken or replayed into another login',async t=>{
  const f=fixture();await create(f);await submit(f);const job=await f.storage.get('job');
  t.mock.method(Date,'now',()=>job.expires_at);
  assert.equal((await f.call('take',BACKEND,{id:ID})).status,409);
  assert.equal(await f.storage.get('job'),undefined);
  assert.equal((await create(f,OTHER)).status,201);
  assert.equal((await submit(f)).status,409);
});

test('cancel checks task identity and removes data and alarm',async()=>{
  const f=fixture();await create(f);await submit(f);
  assert.equal((await f.call('cancel',BACKEND,{id:OTHER})).status,409);
  assert.ok(await f.storage.get('job'));
  assert.equal((await f.call('cancel',BACKEND,{id:ID})).status,200);
  assert.equal(await f.storage.get('job'),undefined);assert.equal(f.storage.alarm,null);
  assert.equal((await f.call('take',BACKEND,{id:ID})).status,409);
});

test('a delayed alarm preserves the new task; due alarm clears expired data',async t=>{
  const f=fixture();await create(f);const job=await f.storage.get('job');
  await f.object.alarm();assert.ok(await f.storage.get('job'));
  assert.equal(f.storage.alarm,job.expires_at);
  t.mock.method(Date,'now',()=>job.expires_at+1);
  await f.object.alarm();assert.equal(await f.storage.get('job'),undefined);
  assert.equal(f.storage.alarm,null);
});

test('storage errors are reported without leaking diagnostic data',async()=>{
  const f=fixture();f.storage.transaction=async()=>{throw Error('sensitive internal diagnostic');};
  const result=await create(f);assert.equal(result.status,503);
  assert.deepEqual(result.value,{error:'relay_unavailable'});
});
