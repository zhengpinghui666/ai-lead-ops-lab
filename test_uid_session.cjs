'use strict';
const assert=require('node:assert/strict');
const {bootstrap}=require('./scripts/bootstrap-uid-session.cjs');
(async()=>{
  let closed=false,verified=0;
  const request={url:()=> 'https://imapi.douyin.com/v1/stranger/get_conversation_list',
    method:()=> 'POST',allHeaders:async()=>({':authority':'imapi.douyin.com','cookie':'synthetic-secret','user-agent':'Synthetic browser','content-length':'9'}),
    postDataBuffer:()=>Buffer.from('synthetic')};
  const page={waitForRequest:async predicate=>{assert.ok(predicate(request));return request;},goto:async()=>{}};
  const context={newPage:async()=>page,cookies:async()=>[{name:'sessionid',value:'synthetic-secret'}],close:async()=>{closed=true;}};
  const result=await bootstrap('synthetic_account',{launchContext:async()=>context,verify:async input=>{
    verified++;assert.ok(closed);assert.ok(!(':authority' in input.headers));assert.ok(!('content-length' in input.headers));
    assert.equal(input.identity_cookie,'sessionid=synthetic-secret');return {status:'session_ready',can_send:false,live_verified:false,credential_file_created:true};
  }});
  assert.equal(verified,1);assert.equal(result.browser_used_for_http,false);assert.equal(result.credential_file_created,true);
  assert.ok(!JSON.stringify(result).includes('synthetic-secret'));
  for(const failure of ['open','close','capture']){
    verified=0;
    const fail=()=>{throw Error('synthetic-secret');};
    const r=await bootstrap('synthetic_account',{launchContext:async()=>{
      if(failure==='open')return fail();
      return {...context,newPage:async()=>({...page,waitForRequest:async()=>failure==='capture'?null:request}),close:async()=>failure==='close'?fail():undefined};
    },verify:async()=>{verified++;}});
    assert.equal(verified,0);assert.equal(r.credential_file_created,false);
    assert.ok(!JSON.stringify(r).includes('synthetic-secret'));
  }
  const invalid=await bootstrap('bad\naccount',{launchContext:()=>{throw Error('must not run');}});
  assert.equal(invalid.status,'invalid_account');
  console.log('PASS: account bootstrap, HTTP/2 header removal, browser-close barrier and failure redaction. Synthetic only.');
})().catch(error=>{console.error(error);process.exitCode=1;});
