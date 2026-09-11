'use strict';
// Real Chromium + entirely local fulfilled responses. No platform/profile access.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {capture,submit,promptVisible,describe}=require('./captcha_browser.cjs');
const {solve}=require('./captcha_solver.cjs');
const bytes=Object.fromEntries(['background','target'].map(n=>[n,fs.readFileSync(path.join(__dirname,'tests/fixtures/captcha',n+'.png'))]));
const top='https://www.douyin.com/search/local-fixture';
let options={},cases=0,requests=0;
function childHTML(){
  const bg=options.cors?'https://images.fixture.invalid/background.png':'https://captcha.fixture.invalid/background.png';
  return `<meta charset="utf-8">${options.outer?'<div id="captcha_container">':''}${options.click?'<img id="captcha_click_image" class="vc-captcha-verify-img-picture" src="https://captcha.fixture.invalid/background.png">':''}<style>body{margin:0}#wrap{position:relative;width:180px;height:125px}#captcha_verify_image{width:180px;height:95px}#captcha-verify_img_slide{position:absolute;left:0;top:23.5px;width:18px;height:18px}.captcha-slider-btn{position:absolute;left:0;top:100px;width:25px;height:20px}</style>
    <p>${options.type||'拖动滑块完成拼图'}</p><div id="wrap"><img id="captcha_verify_image" src="${bg}">
    ${options.missing?'':'<img id="captcha-verify_img_slide" src="https://captcha.fixture.invalid/target.png">'}
    <div class="captcha-slider-btn">拖动</div></div><script>window.down=0;window.up=0;document.querySelector('.captcha-slider-btn').onpointerdown=()=>window.down++;
    document.onpointerup=()=>{if(window.down)window.up++};</script>`;
}
function passed(name){cases++;console.log('PASS '+name);}
(async()=>{
  const browser=await chromium.launch({...require('./browser_config.cjs')(),headless:true});
  try{
    const context=await browser.newContext({serviceWorkers:'block'});
    await context.route('**/*',async route=>{
      requests++;const u=new URL(route.request().url());
      if(u.href===top)return route.fulfill({contentType:'text/html',body:`<body>本地合成采集页面<div ${options.outer?'':'id="captcha_container"'} style="margin:60px"><iframe style="border:0;width:300px;height:230px" src="https://captcha.fixture.invalid/frame"></iframe>${options.duplicate?'<iframe src="https://captcha.fixture.invalid/frame"></iframe>':''}</div></body>`});
      if(u.hostname==='captcha.fixture.invalid'&&u.pathname.startsWith('/frame'))return route.fulfill({contentType:'text/html',body:childHTML()});
      if(['captcha.fixture.invalid','images.fixture.invalid'].includes(u.hostname)&&['/background.png','/target.png'].includes(u.pathname)){
        if(options.delay&&u.pathname==='/target.png')await new Promise(r=>setTimeout(r,500));
        return route.fulfill({contentType:'image/png',body:bytes[u.pathname.slice(1,-4)]});
      }
      await route.abort();throw Error('Unexpected fixture URL');
    });
    const page=await context.newPage();
    async function load(value={}){options=value;await page.goto(top,{waitUntil:value.delay?'domcontentloaded':'load'});}
    await load();
    assert.equal(await promptVisible(page),true);
    const challenge=await capture(page);
    assert.equal(challenge.adapter,'douyin_iframe_slider');assert.ok(challenge.payload,challenge.reason);
    assert.equal(challenge.boxes.background.width,180);assert.equal(challenge.background_size[0],360);
    assert.ok(challenge.boxes.background.x>=60);passed('cross-origin iframe capture and global scaled geometry');
    const structure=await describe(page);
    assert.equal(structure[1].type,'slider');assert.equal(structure[1].images.length,2);
    assert.ok(!JSON.stringify(structure).includes('fixture.invalid'));passed('structural diagnostics exclude image URLs and page text');
    const python=path.join(__dirname,'.tools/ddddocr-eval/venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
    assert.ok(fs.existsSync(python),'real OCR runtime required for this explicit test');
    const prediction=await solve(challenge.payload,{python,stopping:()=>false});
    assert.equal(prediction.status,'predicted');
    assert.equal((await submit(page,challenge,prediction,()=>{})).submitted,true);
    assert.deepEqual(await page.frameLocator('#captcha_container > iframe').locator('body').evaluate(()=>[window.down,window.up]),[1,1]);
    assert.equal(await promptVisible(page),true,'A submitted but visible iframe must not be called cleared');
    passed('real local OCR and one pointer submission; visible prompt remains blocked');
    await page.locator('#captcha_container').evaluate(el=>el.hidden=true);
    assert.equal(await promptVisible(page),false);passed('hidden iframe prompt is cleared');
    await load({missing:true});assert.equal((await capture(page)).reason,'target_missing');passed('missing piece diagnostic');
    await load({type:'请依次点选图中的物体'});assert.equal((await capture(page)).reason,'unsupported_type');passed('point selection never enters slider prediction');
    await load({duplicate:true});assert.equal((await capture(page)).reason,'frame_ambiguous');passed('ambiguous frames rejected');
    await load({cors:true});assert.equal((await capture(page)).reason,'image_pixels_unavailable');passed('tainted canvas reported without alternate fetch');
    await load({delay:true});assert.ok((await capture(page)).payload);passed('delayed image loading completes inside readiness budget');
    const old=await capture(page);
    await page.locator('#captcha_container > iframe').evaluate(el=>el.src='https://captcha.fixture.invalid/frame-new');
    await page.frameLocator('#captcha_container > iframe').locator('#captcha_verify_image').waitFor();
    assert.deepEqual(await submit(page,old,prediction,()=>{}),{submitted:false,reason:'challenge_changed'});passed('changed frame identity rejected before interaction');
    await load({missing:true});
    let checks=0;await assert.rejects(capture(page,{check:()=>{if(++checks===3)throw Error('cancelled');}}),/cancelled/);passed('DOM readiness wait remains cancellable');
    await load({outer:true,click:true});assert.equal(await promptVisible(page),true);
    assert.deepEqual(await capture(page),{reason:'point_prompt_or_image_unsupported',adapter:'douyin_same_shape_pair'});
    passed('observed outer-frame point-selection structure is identified without slider action');
    await load({outer:true});assert.ok((await capture(page)).payload);
    await page.locator('iframe').evaluate(el=>el.hidden=true);
    assert.equal(await promptVisible(page),false,'Hidden outer frame must not block recovered reads');
    passed('outer iframe slider and hidden outer-frame recovery');
    console.log(JSON.stringify({passed:cases,browser:'real Chromium',images:'synthetic fixture',ocr:'local ddddocr',network:'all requests locally fulfilled; zero live platform traffic',requests}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
