'use strict';
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {navigate}=require('./live_navigation.cjs');
const URL='https://live.douyin.com/12345';
async function run({failures=1,code='ERR_FAILED',respond=false,main=true,sameUrl=true,cancel=false,cost=0,plain=false,lateResponse=false}={}){
 const page=new EventEmitter(),frame={},seen=[],delays=[];let clock=0,stopped=false,calls=0;
 page.mainFrame=()=>frame;
 page.waitForLoadState=async()=>{};
 const request={isNavigationRequest:()=>main,frame:()=>frame,url:()=>sameUrl?URL:URL+'/other',failure:()=>({errorText:'net::'+code})};
 page.goto=async(url,options)=>{
  assert.equal(url,URL);assert.ok(options.timeout<=25000);calls++;clock+=cost;
  if(calls<=failures){if(respond)page.emit('response',{request:()=>request});if(!plain)page.emit('requestfailed',request);throw Error('synthetic');}
  return {status:()=>200};
 };
 let result,error;
 try{result=await navigate(page,URL,{stopped:()=>stopped,attempt:n=>seen.push(n),retry:async()=>{},now:()=>clock,pause:async ms=>{delays.push(ms);clock+=ms;if(cancel)stopped=true;if(lateResponse)page.emit('response',{request:()=>request,status:()=>200});}});}catch(e){error=e;}
 assert.equal(page.listenerCount('requestfailed'),0);assert.equal(page.listenerCount('response'),0);
 return {calls,seen,delays,result,error};
}
(async()=>{
 let r=await run({failures:2});assert.equal(r.calls,3);assert.deepEqual(r.delays,[1000,3000]);assert.equal(r.result.status(),200);
 r=await run({failures:10});assert.equal(r.calls,3);assert.ok(r.error);
 for(const opts of [{respond:true},{main:false},{sameUrl:false},{plain:true},{code:'ERR_BLOCKED_BY_CLIENT'},{code:'ERR_CERT_AUTHORITY_INVALID'},{code:'ERR_ABORTED'}]){
  r=await run(opts);assert.equal(r.calls,1,JSON.stringify(opts));assert.ok(r.error);
 }
 r=await run({cancel:true});assert.equal(r.calls,1);assert.ok(r.error);
 r=await run({cost:25000});assert.equal(r.calls,1);assert.ok(r.error);
 r=await run({lateResponse:true});assert.equal(r.calls,1);assert.equal(r.result.status(),200,'A late page response prevents another navigation');
 // Real Chromium, local route stubs only: no request is sent to Douyin.
 const {chromium}=require('playwright');
 const browser=await chromium.launch({...require('./browser_config.cjs')(),headless:true});
 try{
  const page=await browser.newPage();let attempts=0,reported=0;const trace=[];
  await page.route('**/*',async route=>{
   if(!route.request().isNavigationRequest() || route.request().frame()!==page.mainFrame() || route.request().url()!==URL)return route.fulfill({status:204,body:''});
   attempts++;trace.push({call:reported,request:attempts});if(attempts<=2)await route.abort('failed');else await route.fulfill({status:200,contentType:'text/html',body:'<p>local navigation fixture</p>'});
  });
  const response=await navigate(page,URL,{stopped:()=>false,attempt:n=>reported=n,retry:async()=>{}});
  console.log('Chromium navigation trace',JSON.stringify(trace));
  assert.equal(response.status(),200);assert.ok(reported>=2 && reported<=3);assert.equal(attempts,3,'No extra navigation after Chromium itself receives the page');
  await page.close();
 }finally{await browser.close();}
 console.log('PASS: bounded original-page retries; HTTP response, auth/block/certificate, changed scope, cancellation and deadline exclusions; real Chromium routed fixture. No platform requests.');
})().catch(e=>{console.error(e);process.exitCode=1;});
