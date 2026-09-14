'use strict';
// Real Chromium DOM + routed synthetic responses; no platform requests or saved profiles.
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {createReader}=require('./collector_reader.cjs');
const vid='7600000000000000001',url=`https://www.douyin.com/video/${vid}`;
class Stop extends Error{constructor(code){super(code);this.code=code;}}
async function scenario(browser,name){
 const context=await browser.newContext({viewport:{width:1360,height:900},serviceWorkers:'block'});
 const messages=[],requests=[];let failure;
 try{
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.origin!=='https://www.douyin.com'){await route.abort();return;}
   if(u.pathname===`/video/${vid}`){await route.fulfill({status:302,headers:{location:`/note/${vid}`}});return;}
   if(u.pathname===`/note/${vid}`){
    const panel=id=>`<div id="${id}" data-e2e="comment-list" style="height:240px;width:360px;overflow-y:scroll;border:1px solid"><div style="height:1600px">Synthetic comments</div></div>`;
    const hidden=`<div style="display:none">${panel('hidden')}</div>`;
    const lists=name==='hidden-first'?hidden+panel('active'):name==='hidden-last'?panel('active')+hidden:name==='ambiguous'?panel('active')+panel('other'):name==='all-hidden'?hidden:panel('active');
    await route.fulfill({contentType:'text/html; charset=utf-8',body:`<!doctype html><title>Synthetic note fixture</title><button id="tab">评论(3)</button><main style="display:none" id="comments">${lists}</main><script>
      const name=${JSON.stringify(name)};let loaded=false,more=false;
      if(name==='prefetch-hidden'||name==='prefetch-login-gate'){fetch('/aweme/v1/web/comment/list/?aweme_id=${vid}&cursor=0').then(()=>{loaded=true;if(name==='prefetch-login-gate'){const p=document.createElement('p');p.textContent='登录后可查看更多评论';document.body.append(p);}});}
      document.querySelector('#tab').onclick=async()=>{document.querySelector('#comments').style.display='block';await fetch('/aweme/v1/web/comment/list/?aweme_id=${vid}&cursor=0');loaded=true;if(name==='login-gate'){const p=document.createElement('p');p.textContent='登录后可查看更多评论';document.body.append(p);}};
      document.querySelector('#active')?.addEventListener('scroll',async()=>{if(loaded&&!more){more=true;await fetch('/aweme/v1/web/comment/list/?aweme_id=${vid}&cursor=2');}});
      </script>`});return;
   }
   if(u.pathname==='/aweme/v1/web/comment/list/'&&u.searchParams.get('aweme_id')===vid){
    const cursor=u.searchParams.get('cursor');requests.push(cursor);
    const ids=cursor==='0'?['7600000000000000002','7600000000000000003']:['7600000000000000004'];
    await route.fulfill({contentType:'application/json',body:JSON.stringify({status_code:0,total:3,cursor:cursor==='0'?2:3,has_more:cursor==='0'?1:0,comments:ids.map(cid=>({cid,aweme_id:vid,text:'合成评论 '+cid,user:{uid:'123456789012',nickname:'合成用户'}}))})});return;
   }
   await route.abort();
  });
  const page=await context.newPage();
  const shared={config:{kind:'video',target:url,comment_limit:10},emit:async r=>messages.push(r),status:async()=>{},Stop,
   ready:async()=>{},check:()=>{if(failure)throw failure;},fail:e=>{failure=e;},stopping:()=>!!failure,release:()=>{},running:async()=>{},
   pause:async(_reader,code)=>{throw new Stop(code);},reasons:{rate_limited:'limited',access_denied:'denied'}};
  const reader=createReader(page,shared);
  let result,error;
  try{result=await reader.collect({video_id:vid,video_url:url,video_title:'合成无畏契约图文'});}catch(e){error=e;}
  await reader.settle();
  if(['hidden-first','hidden-last','single','prefetch-hidden'].includes(name)){
   assert.equal(error,undefined,name);assert.equal(result,true,name);
   assert.deepEqual(requests,name==='prefetch-hidden'?['0','0','2']:['0','2'],name);assert.equal(messages.filter(r=>r.type==='comment').length,3,name);
   assert.equal(messages.filter(r=>r.type==='checkpoint').at(-1).status,'done',name);
   assert.ok(messages.some(r=>r.stage==='comment-read'&&r.snapshot.processing.has_more===false),name);
  }else if(['login-gate','prefetch-login-gate'].includes(name)){
   assert.equal(error?.code,'needs_login');assert.deepEqual(requests,['0']);
  }else{
   assert.equal(error,undefined,name);assert.equal(result,false,name);assert.deepEqual(requests,['0'],name);
   assert.equal(messages.filter(r=>r.type==='checkpoint').at(-1).status,'partial',name);
  }
  return name;
 }finally{await context.close();}
}
(async()=>{
 const browser=await chromium.launch({...require('./browser_config.cjs')(),headless:true});
 try{
  const results=await Promise.allSettled(['hidden-first','hidden-last','single','ambiguous','all-hidden','login-gate','prefetch-hidden','prefetch-login-gate'].map(name=>scenario(browser,name)));
  for(const r of results)if(r.status==='rejected')throw r.reason;
  console.log('PASS: 8 real Chromium synthetic note cases (including hidden prefetch and prefetch login gate): hidden duplicates in either order, single panel, ambiguous panels, no visible panel and login guard. No live access.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
