'use strict';
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const {observe,messageVerdict}=require('./captcha_verdict.cjs');
let passed=0;const pass=name=>{passed++;console.log('PASS '+name);};
(async()=>{
  assert.equal(messageVerdict('验证通过！'),'passed');assert.equal(messageVerdict('验证失败'),'failed');
  for(const message of ['success','200','识别成功','请点击验证通过',''])assert.equal(messageVerdict(message),'unknown');
  pass('only explicit platform-result messages are verdicts');
  const browser=await chromium.launch({...require('./browser_config.cjs')(),headless:true});
  try{
    const context=await browser.newContext({serviceWorkers:'block'});let body={message:'success'},httpStatus=200;
    let held,release,oldRequestSeen;
    await context.route('**/*',async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/frame')return route.fulfill({contentType:'text/html',body:'<p>unrelated frame</p>'});
      if(url.pathname==='/test')return route.fulfill({contentType:'text/html',body:'<meta charset="utf-8"><div id="captcha_container"><img style="width:30px;height:30px"><span class="vc-captcha-verify-button-text" style="display:none">验证成功</span></div><iframe src="/frame"></iframe>'});
      if(url.searchParams.has('old')){oldRequestSeen?.();await new Promise(r=>{release=r;});}
      return route.fulfill({status:httpStatus,contentType:'application/json',body:JSON.stringify(body)});
    });
    let page,watch;
    const fresh=async setup=>{await watch?.close();await page?.close();page=await context.newPage();await page.goto('https://www.douyin.com/test');if(setup)await setup();watch=await observe(page);watch.arm();};
    const post=async(url='/captcha/verify',method='POST',frame=page)=>{await frame.evaluate(async([u,m])=>{await(await fetch(u,{method:m})).text();},[url,method]);};
    const result=async()=>{await page.waitForTimeout(60);return (await watch.read()).status;};
    await fresh();await post();assert.equal(await result(),'unknown');pass('HTTP 200 and generic success are insufficient');
    body={message:'验证通过'};await fresh();await post();assert.equal(await result(),'passed');pass('current same-frame verification request and explicit success');
    body={message:'验证失败'};await fresh();await post();assert.equal(await result(),'failed');pass('explicit rejection stays failed');
    body={message:'验证通过',msg:'验证失败'};await fresh();await post();assert.equal(await result(),'failed');pass('contradictory response never passes');
    body={message:'验证通过'};httpStatus=500;await fresh();await post();assert.equal(await result(),'unknown');httpStatus=200;pass('unsuccessful HTTP status cannot confirm a pass');
    await fresh();await post('/other/verify');await post('/captcha/verify','GET');await post('/captcha/verify','POST',page.frames()[1]);assert.equal(await result(),'unknown');pass('unrelated endpoint, method and frame excluded');
    await watch.close();await page.close();page=await context.newPage();await page.goto('https://www.douyin.com/test');
    const seen=new Promise(r=>{oldRequestSeen=r;});held=post('/captcha/verify?old=1');await seen;
    watch=await observe(page);watch.arm();release();await held;assert.equal(await result(),'unknown');pass('late response from a request preceding this attempt excluded');
    await fresh();assert.equal(await result(),'unknown');
    await page.locator('.vc-captcha-verify-button-text').evaluate(el=>el.style.display='inline');assert.equal(await result(),'passed');pass('new visible platform success label');
    await fresh(async()=>page.locator('.vc-captcha-verify-button-text').evaluate(el=>el.style.display='inline'));
    assert.equal(await result(),'unknown');pass('preexisting success label is not this attempt');
    await fresh(async()=>page.locator('#captcha_container').evaluate(el=>{el.style.opacity='0';el.querySelector('span').style.display='inline';}));
    assert.equal(await result(),'unknown');pass('transparent ancestor cannot expose a success label');
    await fresh();await post();assert.equal(await result(),'passed');body={message:'验证失败'};await post();assert.equal(await result(),'failed');pass('later explicit failure overrides earlier success');
    await watch.close();body={message:'验证通过'};await post();assert.equal(await result(),'failed');pass('closed observer cannot accept later activity');
    await page.close();console.log(JSON.stringify({passed,network:'all requests locally fulfilled; no platform traffic'}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
