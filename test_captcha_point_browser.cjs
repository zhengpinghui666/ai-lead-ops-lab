'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright');
const {capture,submit,describe}=require('./captcha_browser.cjs');
const {positions,kind}=require('./captcha_point_browser.cjs');
const {solve}=require('./captcha_solver.cjs');
const {createReader}=require('./collector_reader.cjs');
const {createVerification}=require('./collector_verification.cjs');
const raw=fs.readFileSync(path.join(__dirname,'tests/fixtures/captcha/point-pair.png'));
const python=path.join(__dirname,'.tools/ddddocr-eval/venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const top='https://www.douyin.com/search/local-point?type=video';
const endpoint='/aweme/v1/web/general/search/single/';
let scenario='accepted',clicks=[],validRequests=0,confirmations=0,cases=0;
function passed(name){cases++;console.log('PASS '+name);}
(async()=>{
  assert.equal(kind('点击两个形状相同的物体\u00a0\n刷新'), 'same_shape_pair');
  assert.equal(kind('请依次点击两个形状相同的物体'), '');
  assert.equal(kind('点击两个颜色相同的物体'), '');
  const browser=await chromium.launch({...require('./browser_config.cjs')(),headless:true});
  try{
    const context=await browser.newContext({serviceWorkers:'block'});
    await context.route('**/*',async route=>{
      const u=new URL(route.request().url());
      if(u.hostname==='point.fixture.invalid'&&u.pathname==='/captcha/verify')return route.fulfill({json:{message:scenario==='platform-failed'?'验证失败':scenario==='platform-unknown'?'success':'验证通过'}});
      if(u.pathname.startsWith('/search/'))return route.fulfill({contentType:'text/html',body:`<meta charset="utf-8"><body>本地测试页面<div style="margin:50px"><iframe style="border:0;width:390px;height:320px" src="https://point.fixture.invalid/frame"></iframe></div><script>
        fetch('${endpoint}');addEventListener('message',async event=>{if(event.data!=='selected')return;${scenario==='stays'?'':"document.querySelector('iframe').hidden=true;"}await fetch('${endpoint}?selected=1');});</script>`});
      if(u.pathname===endpoint){
        const selected=u.searchParams.has('selected');if(selected)validRequests++;
        return route.fulfill({json:selected?{status_code:0,data:[{aweme_info:{aweme_id:'7600000000000000111',desc:'合成作品'}}]}:
          {status_code:0,search_nil_info:{search_nil_type:'verify_check'},data:[]}});
      }
      if(u.hostname==='point.fixture.invalid'&&u.pathname==='/frame')return route.fulfill({contentType:'text/html',body:`<meta charset="utf-8"><div id="captcha_container"><p>点击两个形状相同的物体</p><div style="position:relative"><img id="captcha_click_image" style="width:360px;height:225px" src="https://point.fixture.invalid/point.png">${scenario==='modern-existing'?'<span class="vc-captcha-verify-img-point" style="position:absolute;top:10px;left:10px">1</span>':''}</div><span>刷新</span>${scenario.startsWith('modern')?'<div class="vc-captcha-verify-click-action">'+(scenario==='modern-missing'?'':'<div class="vc-captcha-verify-pc-button"><button disabled>确认</button></div>')+'</div>':''}</div><script>
        const modern=${scenario.startsWith('modern')};
        async function acceptSelection(){await fetch('/captcha/verify',{method:'POST'});parent.postMessage('selected','*');}
        const button=document.querySelector('button');if(button)button.onclick=()=>{console.log('CONFIRM');acceptSelection();};
        let selected=[];document.querySelector('img').onclick=event=>{let b=event.target.getBoundingClientRect();selected.push([(event.clientX-b.x)*480/b.width,(event.clientY-b.y)*300/b.height]);
        console.log('POINT:'+JSON.stringify(selected));
        if(modern&&${scenario!=='modern-unregistered'}){let marker=document.createElement('span');marker.className='vc-captcha-verify-img-point';marker.textContent=selected.length;Object.assign(marker.style,{position:'absolute',left:(event.clientX-b.x)+'px',top:(event.clientY-b.y)+'px',pointerEvents:'none'});event.target.parentNode.append(marker);}
        ${scenario==='changes-after-first'?"document.querySelector('p').innerText='请依次点击指定物体';":''}
        if(selected.length===2){const one=p=>p[0]>20&&p[0]<95&&p[1]>15&&p[1]<100;const two=p=>p[0]>300&&p[0]<400&&p[1]>150&&p[1]<255;
          if((one(selected[0])&&two(selected[1]))||(one(selected[1])&&two(selected[0]))){
            if(!modern||${scenario==='modern-auto'})acceptSelection();
            else if(button&&${scenario!=='modern-disabled'})setTimeout(()=>{button.disabled=false;},100);
          }}};</script>`});
      if(u.hostname==='point.fixture.invalid'&&u.pathname==='/point.png')return route.fulfill({contentType:'image/png',body:raw});
      await route.abort();throw Error('Unexpected fixture request');
    });
    async function pageFor(value){scenario=value;clicks=[];validRequests=0;confirmations=0;const page=await context.newPage();page.on('console',m=>{if(m.text().startsWith('POINT:'))clicks=JSON.parse(m.text().slice(6));if(m.text()==='CONFIRM')confirmations++;});return page;}
    let page=await pageFor('accepted');await page.goto(top,{waitUntil:'load'});
    const challenge=await capture(page);assert.equal(challenge.payload?.method,'same_shape_pair',challenge.reason);
    assert.deepEqual(challenge.image_size,[360,225]);assert.equal(challenge.boxes.image.width,360);
    const prediction=await solve(challenge.payload,{python});assert.equal(prediction.status,'predicted',prediction.reason);
    assert.equal(positions(challenge,{...prediction,result:{points:[[0,2],[3,4]]}}),null);
    assert.equal(positions(challenge,{...prediction,image_size:[480,300]}),null);
    passed('image-only capture, actual screenshot size and local worker prediction');
    await page.frames()[1].locator('img').evaluate(el=>el.src+='?changed=1');
    assert.deepEqual(await submit(page,challenge,prediction,()=>{}),{submitted:false,reason:'challenge_changed'});
    assert.equal(clicks.length,0);passed('changed image identity causes zero clicks');await page.close();
    page=await pageFor('changes-after-first');await page.goto(top,{waitUntil:'load'});
    const before=await capture(page),answer=await solve(before.payload,{python});
    await assert.rejects(submit(page,before,answer,()=>{}),/point_challenge_changed/);
    assert.equal(clicks.length,1);passed('changed instruction after first selection prevents second click');await page.close();
    page=await pageFor('accepted');await page.goto(top,{waitUntil:'load'});
    const cancel=await capture(page);let stopped=false;
    await assert.rejects(submit(page,cancel,await solve(cancel.payload,{python}),()=>{if(stopped)throw Error('cancelled');},()=>{stopped=true;}),/cancelled/);
    assert.equal(clicks.length,0);passed('cancellation before first selection');await page.close();
    page=await pageFor('modern-existing');await page.goto(top,{waitUntil:'load'});
    assert.equal((await capture(page)).reason,'point_selection_already_present');
    assert.equal(clicks.length,0);assert.equal(confirmations,0);
    passed('existing selections decline before adding input');await page.close();
    page=await pageFor('modern-confirm');await page.goto(top,{waitUntil:'load'});
    await page.frames()[1].locator('button').evaluate(el=>{el.innerHTML='<span class="vc-captcha-verify-button-text" style="display:inline-block;width:0;height:0;overflow:hidden">验证成功</span><span>确认</span>';});
    const withStates=await capture(page);
    assert.equal((await submit(page,withStates,await solve(withStates.payload,{python}),()=>{})).submitted,true);
    assert.equal(clicks.length,2);assert.equal(confirmations,1);
    passed('visible confirmation label despite zero-size alternate state text');await page.close();
    page=await pageFor('modern-confirm');await page.goto(top,{waitUntil:'load'});
    await page.frames()[1].locator('.vc-captcha-verify-pc-button').evaluate(el=>{
      const original=el.querySelector('button'),label=document.createElement('div');label.style.display='none';label.className='vc-captcha-verify-button-text';original.replaceWith(label);
      Object.assign(el.style,{width:'70px',height:'30px',background:'blue'});
      el.addEventListener('click',event=>{if(event.target!==el)return;console.log('CONFIRM');parent.postMessage('selected','*');});
    });
    const drawnCaption=await capture(page);
    const drawnStructure=(await describe(page)).find(frame=>frame.controls?.length);
    assert.equal(drawnStructure.controls[0].label_kind,'empty');
    assert.equal(drawnStructure.controls[0].label_length,0);
    assert.equal((await submit(page,drawnCaption,await solve(drawnCaption.payload,{python}),()=>{})).submitted,true);
    assert.equal(confirmations,1);passed('unique visible div control with hidden state label');await page.close();
    for(const value of ['accepted','stays','modern-confirm','modern-auto','modern-missing','modern-disabled','modern-unregistered','platform-failed','platform-unknown']){
      page=await pageFor(value);
      const events=[];class Stop extends Error {constructor(code){super(code);this.code=code;}}
      const config={target:'local-point',kind:'search',video_limit:1,comment_limit:1,interactive:false,captcha:{mode:'auto',python}};
      let verifier;
      const shared={config,emit:async row=>events.push(row),status:async()=>{},Stop,reasons:{},check:()=>{},ready:async()=>{},release:()=>{},fail:e=>{throw e;},stopping:()=>false,
        pause:async(reader,code)=>{assert.equal(code,'needs_verification');if(!await verifier(reader))throw new Stop(code);}};
      verifier=createVerification(shared,['stays','platform-unknown'].includes(value)?{delay:async()=>new Promise(r=>setTimeout(r,50))}:{});
      const reader=createReader(page,shared);
      const succeeds=['accepted','modern-confirm','modern-auto'].includes(value);
      if(succeeds){try{assert.equal((await reader.discover()).length,1);}catch(error){console.log(JSON.stringify({scenario:value,verification:events.filter(r=>r.type==='verification'),clicks,validRequests,confirmations}));throw error;}}
      else await assert.rejects(reader.discover(),/needs_verification/);
      const phases=events.filter(row=>row.type==='verification').map(row=>row.event);
      assert.equal(clicks.length,value==='modern-unregistered'?1:2,value);
      assert.equal(validRequests,succeeds||['stays','platform-failed','platform-unknown'].includes(value)?1:0,value);
      assert.equal(confirmations,value==='modern-confirm'?1:0,value);
      assert.equal(phases.at(-1).phase,succeeds?'accepted':'needs_review',value);
      assert.equal(phases.at(-1).submissions,1);
      if(['modern-missing','modern-disabled'].includes(value))assert.equal(phases.at(-1).reason,'point_confirmation_unavailable');
      if(value==='modern-unregistered')assert.equal(phases.at(-1).reason,'point_selection_not_registered');
      if(value==='platform-failed')assert.equal(phases.at(-1).reason,'platform_verification_failed');
      if(value==='platform-unknown')assert.equal(phases.at(-1).reason,'platform_verdict_unobserved');
      if(succeeds)assert.equal(phases.at(-1).platform_verdict,'passed');
      if(value==='modern-disabled'){
        const structure=await describe(page),frame=structure.find(f=>f.selections===2);
        assert.ok(frame,JSON.stringify(structure));assert.ok(frame.controls.some(c=>c.label==='confirm'&&!c.enabled));
      }
      passed(value+': registered selections, bounded confirmation and fresh-response gate');
      await page.close();
    }
    console.log(JSON.stringify({passed:cases,network:'all routes local synthetic; no platform traffic',ocr:'real local ddddocr'}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
