'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const client=require('./integrations/iphone/forwarder-core.cjs');
const NOW=1789045000000,ID='a'.repeat(32),AUTH='Bearer '+ 'fixture_phone_'.padEnd(43,'p');
const SETTINGS={origin:'https://relay.example.com',account:'1267597446',authorization:AUTH};
const SMS='【抖音】验证码 012345，5分钟内有效。';
const JOB={id:ID,account:SETTINGS.account,created_at:NOW-1000,expires_at:NOW+179000};
function fixture(overrides={}){
  const calls=[],attempts=new Set();
  const io={now:()=>NOW,admit:async id=>{if(attempts.has(id))return false;attempts.add(id);return true;},
    request:async(settings,path,body)=>{calls.push({settings,path,body});return path==='/v1/pending'?{status:200,data:{...JOB}}:{status:200,data:{status:'received'}};},...overrides};
  return {io,calls,attempts};
}
test('only the code and current task metadata leave the phone; leading zero preserved',async()=>{
  const f=fixture(),result=await client.forward(SMS,SETTINGS,f.io);
  assert.equal(result.status,'received');assert.equal(f.calls.length,2);
  assert.deepEqual(f.calls[1].body,{id:ID,code:'012345',received_at:NOW});
  assert.ok(!JSON.stringify(f.calls[1]).includes('分钟'));assert.ok(!JSON.stringify(result).includes('012345'));
});
test('unrelated, ambiguous, malformed and oversized messages perform zero requests',async()=>{
  for(const input of [null,[],{},42,'【其他】验证码 012345','抖音 012345', '抖音验证码 123',
    '抖音验证码 123456789','抖音验证码 012345 或 987654', SMS+'x'.repeat(2000),
    {text:SMS,received_at:'1789045000000'}, {text:SMS,received_at:NOW,extra:'unexpected'}]){
    const f=fixture();assert.equal((await client.forward(input,SETTINGS,f.io)).status,'ignored');assert.equal(f.calls.length,0);
  }
});
test('phone config rejects userinfo, redirects-as-origins, plaintext, local and malformed hosts',()=>{
  for(const origin of ['http://relay.example.com','https://relay.example.com/otp','https://u@relay.example.com',
    'https://relay.example.com?next=elsewhere','https://127.0.0.1','https://localhost','https://relay.local',
    'https://relay.example.com:444','https://relay.example.com#private'])assert.throws(()=>client.configuration({...SETTINGS,origin}));
  assert.throws(()=>client.configuration({...SETTINGS,account:1267597446}));
  assert.throws(()=>client.configuration({...SETTINGS,authorization:'Bearer short'}));
});
test('missing configuration never makes a network request',async()=>{
  const f=fixture();assert.equal((await client.forward(SMS,null,f.io)).status,'not_configured');assert.equal(f.calls.length,0);
});
test('no task, mismatched account and malformed responses never submit',async()=>{
  for(const [data,expected] of [[{id:null},'no_pending'],[{...JOB,account:'other'},'wrong_account'],
      [{...JOB,id:123},'invalid_response'],[{...JOB,expires_at:NOW+999999},'invalid_response'],[[],'invalid_response']]){
    let calls=0;const f=fixture({request:async()=>{calls++;return {status:200,data};}});
    assert.equal((await client.forward(SMS,SETTINGS,f.io)).status,expected);assert.equal(calls,1);assert.equal(f.attempts.size,0);
  }
});
test('old reception, future reception and expired task are rejected',async()=>{
  for(const received_at of [NOW-1001,NOW+30001,NOW-200000]){
    const f=fixture();assert.equal((await client.forward({text:SMS,received_at},SETTINGS,f.io)).status,'expired');assert.equal(f.calls.length,1);
  }
  const f=fixture({now:()=>JOB.expires_at});assert.equal((await client.forward(SMS,SETTINGS,f.io)).status,'expired');
});
test('duplicate invocation and unknown submit result never cause automatic resubmission',async()=>{
  const f=fixture();const request=f.io.request;
  f.io.request=async(...args)=>{const r=await request(...args);if(args[1]==='/v1/otp')throw Error(SMS+AUTH);return r;};
  const first=await client.forward(SMS,SETTINGS,f.io),second=await client.forward(SMS,SETTINGS,f.io);
  assert.equal(first.status,'unknown');assert.equal(second.status,'already_attempted');
  assert.equal(f.calls.filter(x=>x.path==='/v1/otp').length,1);assert.ok(!JSON.stringify(first).includes(AUTH));
});
test('no post is sent when durable admission fails',async()=>{
  const f=fixture({admit:async()=>{throw Error('storage unavailable');}});
  assert.equal((await client.forward(SMS,SETTINGS,f.io)).status,'unavailable');assert.equal(f.calls.length,1);
});
test('200 alone is insufficient; auth and conflict states have separate outcomes',async()=>{
  for(const [response,expected] of [[{status:200,data:{}},'unknown'],[{status:500,data:{status:'received'}},'unknown'],
    [{status:401,data:{}},'unauthorized'],[{status:409,data:{error:'already_received'}},'already_attempted'],
    [{status:409,data:{error:'stale_otp'}},'expired']]){
    const f=fixture();const request=f.io.request;f.io.request=async(...args)=>args[1]==='/v1/otp'?response:request(...args);
    assert.equal((await client.forward(SMS,SETTINGS,f.io)).status,expected);
  }
});
test('connection check is read-only and does not mark a phone OTP test passed',async()=>{
  const f=fixture();const result=await client.check(SETTINGS,f.io);
  assert.equal(result.status,'connected');assert.equal(f.calls.length,1);assert.equal(f.attempts.size,0);
  assert.match(result.message,/尚未完成/);
});

async function runBundle({input=SMS,inApp=false,redirect=false,networkError=false,initialConfig=SETTINGS}={}){
  const source=fs.readFileSync('static/clubops-iphone.js','utf8');
  const store=new Map(initialConfig?[['clubops.iphone.login.config.v1',JSON.stringify(initialConfig)]]:[]);
  const calls=[],outputs=[];let completes=0,alerts=0;
  class Request{
    constructor(url){this.url=url;}
    async loadString(){
      calls.push(this);assert.equal(this.onRedirect(),null);assert.equal(this.allowInsecureRequest,false);assert.equal(this.timeoutInterval,8);
      if(networkError)throw Error(SMS+AUTH);
      this.response={url:redirect?'https://elsewhere.example.com':this.url,statusCode:200};
      return JSON.stringify(this.url.endsWith('/pending')?{...JOB,created_at:Date.now()-1000,expires_at:Date.now()+179000}:{status:'received'});
    }
  }
  const Keychain={contains:k=>store.has(k),get:k=>{if(!store.has(k))throw Error('missing');return store.get(k);},set:(k,v)=>store.set(k,v),remove:k=>store.delete(k)};
  class Alert{constructor(){alerts++;throw Error('Unexpected UI during automation');}}
  const Script={complete:()=>completes++,setShortcutOutput:v=>outputs.push(v)};
  const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
  await new AsyncFunction('args','config','Request','Keychain','Alert','Script',source)({shortcutParameter:input},{runsInApp:inApp},Request,Keychain,Alert,Script);
  return {store,calls,outputs,completes,alerts};
}
test('generated Scriptable bundle runs with shortcut input, safe output and credential-free code',async()=>{
  const r=await runBundle();assert.equal(r.outputs[0].status,'received');assert.equal(r.completes,1);assert.equal(r.alerts,0);
  const ledger=r.store.get('clubops.iphone.login.attempts.v1');assert.ok(ledger.includes(ID));assert.ok(!ledger.includes('012345'));
  assert.equal(r.calls[1].headers.Authorization,AUTH);
  assert.ok(!fs.readFileSync('static/clubops-iphone.js','utf8').includes(AUTH));
});
test('Scriptable background without input or config never opens an alert',async()=>{
  for(const options of [{input:null},{initialConfig:null},{input:SMS,inApp:true,initialConfig:null}]){
    const r=await runBundle(options);assert.equal(r.calls.length,0);assert.equal(r.alerts,0);assert.equal(r.completes,1);
  }
});
test('Scriptable rejects changed response URL and redacts low-level network errors',async()=>{
  for(const options of [{redirect:true},{networkError:true}]){
    const r=await runBundle(options);assert.equal(r.calls.length,1);assert.equal(r.outputs[0].status,'unavailable');
    assert.ok(!JSON.stringify(r.outputs).includes('012345'));assert.ok(!JSON.stringify(r.outputs).includes(AUTH));
  }
});
