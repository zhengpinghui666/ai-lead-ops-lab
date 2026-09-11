'use strict';
const assert=require('node:assert/strict');
const {Commands,recover}=require('./scripts/login-recovery.cjs');
function fixture({loggedIn=false,account='1267597446',inputs=1,manualValue='',submit=true,origin='https://www.douyin.com',cancelDuring=''}={}){
  const commands=new Commands(),trace=[],events=[];let listener,closed=false;
  const profile=()=>listener({url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/',status:()=>200,
    headers:()=>({}),body:async()=>Buffer.from(JSON.stringify({status_code:0,user:{unique_id:account}}))});
  const item=(kind)=>({isVisible:async()=>{if(cancelDuring==='login'&&kind==='saved-login')commands.push({command:'cancel'});return true;},isEnabled:async()=>{if(cancelDuring==='submit')commands.push({command:'cancel'});return true;},
    inputValue:async()=>{if(cancelDuring==='fill')commands.push({command:'cancel'});return manualValue;},
    fill:async code=>{assert.equal(code,'654321');trace.push('filled');},
    click:async()=>{trace.push(kind);if(kind==='submit')await profile();}});
  const list=(kind,count)=>({count:async()=>count,nth:()=>item(kind)});
  const page={on:(_,fn)=>{listener=fn;},url:()=>origin+'/',
    goto:async()=>{if(loggedIn)await profile();},
    getByText:()=>list('saved-login',1),getByRole:()=>list('submit',submit?1:0),locator:()=>list('otp',inputs),
    evaluate:async()=>({user_agent:'fixture',context:{platform:'Win32',width:100,height:100,cores:1,memory:8}})};
  const context={newPage:async()=>page,cookies:async()=>[],close:async()=>{trace.push('closed');closed=true;}};
  const options={commands,timeoutMs:150,initialWaitMs:1,tickMs:1,armWaitMs:50,launchContext:async()=>context,
    emit:event=>{events.push(event);if(event.status==='arming_relay'){trace.push('armed');commands.push({command:'ready'});commands.push({command:'otp',code:'654321'});}},
    verify:async()=>{assert.equal(closed,true);trace.push('http');return {status:'session_ready',credential_file_created:true};}};
  return {commands,trace,events,options};
}
(async()=>{
  const saved=fixture({loggedIn:true});
  assert.equal((await recover('1267597446',saved.options)).status,'completed');
  assert.deepEqual(saved.trace,['closed','http']);
  const failure=fixture({loggedIn:true});
  failure.options.verify=async()=>({status:'identity_failed',identity_status:'http_failed',
    identity_evidence:{transport_error:'connection_failed',transport_phase:'connect',response_bytes:0,
      cookie:'SECRET_FIXTURE',http_status:true,response_sha256:'SECRET_FIXTURE'}});
  const diagnosed=await recover('1267597446',failure.options);
  assert.equal(diagnosed.status,'identity_failed');assert.equal(diagnosed.sms_step_used,false);
  assert.equal(diagnosed.browser_identity_matched,true);assert.equal(diagnosed.code_filled,false);
  assert.deepEqual(diagnosed.identity_evidence,{response_bytes:0,transport_phase:'connect',transport_error:'connection_failed'});
  assert.equal(JSON.stringify(diagnosed).includes('SECRET_FIXTURE'),false);
  const invalid=fixture({loggedIn:true});
  invalid.options.verify=async()=>({status:'invalid_input',bootstrap_phase:'validation',bootstrap_error:'invalid_client_context',cookie:'SECRET_FIXTURE'});
  const prepared=await recover('1267597446',invalid.options);
  assert.equal(prepared.preparation_status,'invalid_input');assert.equal(prepared.bootstrap_phase,'validation');
  assert.equal(prepared.bootstrap_error,'invalid_client_context');assert.equal(JSON.stringify(prepared).includes('SECRET_FIXTURE'),false);
  const sms=fixture();
  assert.equal((await recover('1267597446',sms.options)).status,'completed');
  assert.deepEqual(sms.trace,['armed','saved-login','filled','submit','closed','http']);
  assert.equal(JSON.stringify(sms.events).includes('654321'),false);
  const wrong=fixture({loggedIn:true,account:'different'});
  assert.equal((await recover('1267597446',wrong.options)).status,'account_mismatch');
  assert.deepEqual(wrong.trace,['closed']);
  for(const args of [{inputs:2},{manualValue:'123456'},{origin:'https://unrelated.example'}]){
    const f=fixture(args);
    assert.equal((await recover('1267597446',f.options)).status,'timeout');
    assert.equal(f.trace.includes('filled'),false);assert.equal(f.trace.includes('http'),false);
  }
  const cancel=fixture();cancel.options.emit=e=>{if(e.status==='arming_relay')cancel.commands.push({command:'cancel'});};
  assert.equal((await recover('1267597446',cancel.options)).status,'cancelled');assert.deepEqual(cancel.trace,['closed']);
  const noRelay=fixture();noRelay.options.emit=()=>{};
  assert.equal((await recover('1267597446',noRelay.options)).status,'relay_unavailable');assert.deepEqual(noRelay.trace,['closed']);
  const manual=fixture({inputs:0});
  const original=manual.options.emit;manual.options.emit=e=>{original(e);if(e.status==='manual_required')manual.commands.push({command:'complete'});};
  assert.equal((await recover('1267597446',manual.options)).status,'completed');
  assert.equal(manual.trace.includes('filled'),false);assert.equal(manual.trace.at(-1),'http');
  const duplicate=fixture();const originalEmit=duplicate.options.emit;
  duplicate.options.emit=e=>{originalEmit(e);if(e.status==='code_filled')duplicate.commands.push({command:'otp',code:'654321'});};
  assert.equal((await recover('1267597446',duplicate.options)).status,'completed');
  assert.equal(duplicate.trace.filter(x=>x==='filled').length,1);
  for(const cancelDuring of ['login','fill','submit']){
    const f=fixture({cancelDuring});
    assert.equal((await recover('1267597446',f.options)).status,'cancelled');
    assert.equal(f.trace.includes('submit'),false);
    assert.equal(f.trace.includes('http'),false);
    assert.equal(f.trace.includes('filled'),cancelDuring==='submit');
    if(cancelDuring==='login')assert.equal(f.trace.includes('saved-login'),false);
  }
  const wallNow=Date.now;
  try{
    for(const jump of [-3600000,3600000]){
      const f=fixture();let wall=wallNow();Date.now=()=>wall;
      const emit=f.options.emit;
      f.options.emit=e=>{emit(e);if(e.status==='arming_relay')wall+=jump;};
      assert.equal((await recover('1267597446',f.options)).status,'completed');
      assert.equal(f.trace.filter(x=>x==='filled').length,1);
      const noInput=fixture({inputs:0});wall=wallNow();
      const emitNoInput=noInput.options.emit;
      noInput.options.emit=e=>{emitNoInput(e);if(e.status==='waiting_sms')wall+=jump;};
      const start=performance.now();
      assert.equal((await recover('1267597446',noInput.options)).status,'timeout');
      assert.ok(performance.now()-start<1000);
      assert.equal(noInput.trace.includes('filled'),false);
    }
  }finally{Date.now=wallNow;}
  console.log('PASS: saved session, SMS arming, one fill, account/origin checks, ambiguous/manual input, cancellation, independent identity and wall-clock jumps. Synthetic browser only.');
})().catch(error=>{console.error(error);process.exitCode=1;});
