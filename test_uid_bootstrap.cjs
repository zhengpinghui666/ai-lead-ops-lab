'use strict';
// Synthetic contexts and injected HTTP callback, never launches real Chrome.
const assert=require('node:assert/strict');
const {sessionIdentityProbe}=require('./scripts/probe-uid-session.cjs');
(async()=>{
  let closed=false,calls=0;
  const newPage=async()=>({evaluate:async()=> 'Synthetic browser'});
  const context={newPage,cookies:async url=>{assert.equal(url,'https://www.douyin.com/aweme/v1/web/user/profile/self/');return [{name:'sessionid',value:'synthetic-secret'}];},close:async()=>{closed=true;}};
  const result=await sessionIdentityProbe('test_account',{launchContext:async()=>context,httpProbe:async input=>{
    assert.ok(closed);calls++;assert.equal(input.cookie,'sessionid=synthetic-secret');
    assert.equal(input.user_agent,'Synthetic browser');
    return {status:'identity_verified',sender_uid:'9007199254740993123',can_send:false,live_verified:false};
  }});
  assert.equal(calls,1);assert.equal(result.sender_uid,'9007199254740993123');assert.equal(result.browser_used_for_http,false);
  assert.ok(!JSON.stringify(result).includes('synthetic-secret'));
  for(const failingStage of ['open','cookies','close']){
    calls=0;
    const fail=()=>{throw Error('synthetic-secret');};
    const r=await sessionIdentityProbe('test_account',{launchContext:async()=>{
      if(failingStage==='open')return fail();
      return {newPage,cookies:async()=>failingStage==='cookies'?fail():[{name:'x',value:'synthetic-secret'}],close:async()=>failingStage==='close'?fail():undefined};
    },httpProbe:async()=>{calls++;}});
    assert.equal(calls,0);assert.equal(r.http_attempts,0);assert.ok(!JSON.stringify(r).includes('synthetic-secret'));
  }
  const none=await sessionIdentityProbe('test_account',{launchContext:async()=>({newPage,cookies:async()=>[],close:async()=>{}}),httpProbe:()=>{throw Error('must not call');}});
  assert.equal(none.status,'needs_login');
  const invalid=await sessionIdentityProbe('bad\naccount',{launchContext:()=>{throw Error('must not launch');}});
  assert.equal(invalid.status,'invalid_input');
  console.log('PASS: identity-only bootstrap, browser-close barrier, missing session, failures and credential redaction. Synthetic only.');
})().catch(error=>{console.error(error);process.exitCode=1;});
