'use strict';
const assert=require('node:assert/strict');
const {prepare}=require('./scripts/prepare-collection-session.cjs');
function fixture(body,status=200){
  const trace=[];
  const response={url:()=> 'https://www.douyin.com/aweme/v1/web/user/profile/self/?aid=6383',status:()=>status,headers:()=>({}),body:async()=>Buffer.from(JSON.stringify(body))};
  const page={waitForResponse:async pred=>{assert.equal(pred({url:()=> 'https://elsewhere.test/aweme/v1/web/user/profile/self/'}),false);assert.equal(pred(response),true);return response;},goto:async url=>{trace.push('navigate');assert.equal(url,'https://www.douyin.com/');},evaluate:async()=>({user_agent:'test',context:{platform:'Win32',width:100,height:100,cores:1,memory:8}})};
  const context={newPage:async()=>page,cookies:async()=>[{name:'sessionid',value:'TEST_ONLY_SECRET',domain:'.douyin.com',path:'/',expires:-1}],close:async()=>trace.push('closed')};
  const options={refreshSession:true,launchContext:async()=>context,verify:async input=>{assert.equal(trace.at(-1),'closed');assert.equal(input.account,'1267597446');assert.equal(Object.keys(input.cookies).length,4);trace.push('http');return {status:'session_ready',credential_file_created:true};}};
  return {trace,page,options};
}
(async()=>{
  const ok=fixture({status_code:0,user:{unique_id:'1267597446'}});
  const result=await prepare('1267597446','',ok.options);
  assert.equal(result.status,'session_ready');assert.equal(result.browser_closed_before_http,true);
  assert.equal(result.browser_used_for_collection,false);assert.deepEqual(ok.trace,['navigate','closed','http']);
  assert.ok(!JSON.stringify(result).includes('TEST_ONLY_SECRET'));
  const unnamed=fixture({status_code:0,user:{unique_id:'1267597446'}});
  const saved=[];
  const originalLaunch=unnamed.options.launchContext;
  unnamed.options.launchContext=async()=>{
    const context=await originalLaunch();
    const read=context.cookies;
    context.cookies=async url=>[...await read(url),{name:'',value:'UNNAMED_TEST_ONLY_SECRET',domain:'.douyin.com',path:'/',expires:-1}];
    return context;
  };
  unnamed.options.verify=async input=>{
    assert.equal(unnamed.trace.at(-1),'closed');
    for(const values of Object.values(input.cookies)){
      assert.equal(values.length,1);assert.equal(values[0].name,'sessionid');
      assert.equal(values[0].value,'TEST_ONLY_SECRET');
    }
    saved.push(input);return {status:'session_ready',credential_file_created:true};
  };
  const cleaned=await prepare('1267597446','',unnamed.options);
  assert.equal(cleaned.status,'session_ready');assert.equal(cleaned.ignored_unnamed_cookie_records,4);
  assert.equal(saved.length,1);assert.ok(!JSON.stringify(cleaned).includes('TEST_ONLY_SECRET'));
  for(const [body,status,expected] of [[{status_code:0,user:{unique_id:'different'}},200,'account_mismatch'],[{status_code:8},200,'needs_login_or_verification'],[{},403,'browser_identity_unverified']]){
    const test=fixture(body,status);const value=await prepare('1267597446','',test.options);
    assert.equal(value.status,expected);assert.equal(value.credential_file_created,false);
    assert.deepEqual(test.trace,['navigate','closed']);
  }
  const missing=fixture({});missing.page.waitForResponse=async()=>{throw Error('timeout');};
  assert.equal((await prepare('1267597446','',missing.options)).status,'browser_identity_unverified');
  assert.deepEqual(missing.trace,['navigate','closed']);
  await assert.rejects(prepare('1267597446','keyword',ok.options),/one preparation mode/);
  for(const beforeLogin of [null,{status_code:8}]){
  const sms=fixture(beforeLogin);if(beforeLogin===null)sms.page.waitForResponse=async()=>null;
  sms.page.getByText=(label,options)=>{assert.equal(label,'一键登录');assert.equal(options.exact,true);return {waitFor:async()=>{},count:async()=>1,click:async()=>sms.trace.push('login_clicked')};};
  let completed;
  sms.options.resumeSavedLogin=true;
  sms.options.confirm=()=>new Promise(resolve=>{completed=resolve;sms.trace.push('waiting_for_user');});
  const pending=prepare('1267597446','',sms.options);
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(sms.trace,['navigate','login_clicked','waiting_for_user']);
  completed();
  assert.equal((await pending).status,'session_ready');
  assert.deepEqual(sms.trace,['navigate','login_clicked','waiting_for_user','closed','http']);
  }
  console.log('PASS: refreshed browser identity required, mismatch and missing identity fail without HTTP/save; Chrome closes before independent HTTP check.');
})().catch(error=>{console.error(error);process.exitCode=1;});
