'use strict';
const {createHash}=require('node:crypto');
const point=require('./captcha_point_browser.cjs');

// Historical DOM contract observed in OSfigitive/yym main.py at 33cb8a4.
// Strict matching only: this is not evidence that current Douyin uses that DOM.
const selectors={background:'#captcha-verify-image',
  target:'xpath=//*[@id="captcha_container"]/div/div[2]/img[2]',
  handle:'#secsdk-captcha-drag-wrapper > div:nth-child(2)'};
const adapter='legacy_slider_dom';
// Independent adapter for the public Douyin iframe contract documented in
// gbiz123/tiktok-captcha-solver fdda3e2. Real platform acceptance is measured separately.
const frameSelector='#captcha_container > iframe';
const frameAdapter='douyin_iframe_slider';
const frameSelectors={background:'#captcha_verify_image',target:'#captcha-verify_img_slide',handle:'.captcha-slider-btn'};
const fingerprint=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
function onPlatform(page){try{const u=new URL(page.url());return u.origin==='https://www.douyin.com';}catch{return false;}}

async function describe(page){
  // Structural diagnostics only: no image source, URL query, full text, inputs or HTML.
  if(!onPlatform(page)||!page.frames)return [];
  const results=[];
  for(const frame of page.frames().slice(0,6)){
    try{
      const state=await frame.locator('body').evaluate(body=>{
        const visible=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(el).visibility!=='hidden';};
        const marker=el=>({tag:el.tagName.toLowerCase(),id:/captcha|verify|secsdk/i.test(el.id)?el.id.slice(0,80):'',
          classes:String(el.className||'').split(/\s+/).filter(v=>/captcha|verify|secsdk/i.test(v)&&/^[a-zA-Z0-9_-]+$/.test(v)).slice(0,4),visible:visible(el)});
        const text=body.innerText||'';
        return {text_present:!!text.trim(),type:/旋转/.test(text)?'rotate':/点选|依次|点击.{0,12}两个形状相同/.test(text)?'click':/拖动|滑块/.test(text)?'slider':'unknown',
          frames:[...body.querySelectorAll('iframe')].slice(0,4).map(marker),
          markers:[...body.querySelectorAll('[id],[class]')].filter(el=>/captcha|verify|secsdk/i.test(el.id+' '+el.className)).slice(0,18).map(marker),
          selections:[...body.querySelectorAll('.vc-captcha-verify-img-point')].filter(visible).length,
          controls:[...body.querySelectorAll('#captcha_container .vc-captcha-verify-pc-button,#captcha_container button')].slice(0,6).map(el=>({
            ...marker(el),label:/^(?:确认|确定|确认提交|提交|验证|Confirm|Verify|Submit)$/i.test((el.innerText||'').replace(/\s+/g,''))?'confirm':'other',
            enabled:!el.disabled&&el.getAttribute('aria-disabled')!=='true',
            label_length:Math.min((el.innerText||'').length,80),
            label_kind:!(el.innerText||'').length?'empty':!(el.innerText||'').replace(/\s+/g,'')?'whitespace':
              !(el.innerText||'').replace(/[\s\u200b-\u200d\ufeff]+/g,'')?'invisible_characters':'text',
            label_text:/^[\u4e00-\u9fff\s]{1,40}$/.test(el.innerText||'')?el.innerText:'unavailable',
            state_labels:[...el.querySelectorAll('.vc-captcha-verify-button-text')].slice(0,4).map(label=>({
              visible:visible(label),label:/^(?:确认|确定|确认提交|提交|验证|Confirm|Verify|Submit)$/i.test((label.innerText||'').replace(/[\s\u200b-\u200d\ufeff]+/g,''))?'confirm':'other'}))})),
          images:[...body.querySelectorAll('img')].slice(0,6).map(el=>({...marker(el),loaded:el.complete,width:el.naturalWidth,height:el.naturalHeight}))};
      },undefined,{timeout:1000});
      results.push(state);
    }catch{results.push({unreadable:true});}
  }
  return results;
}

async function promptVisible(page){
  for(const selector of [frameSelector,'#captcha-verify-image','.captcha-verify-container']){
    const matches=page.locator(selector),count=await matches.count();
    for(let i=0;i<Math.min(count,10);i++)if(await matches.nth(i).isVisible())return true;
    if(count>10)return true; // An ambiguous prompt is never treated as cleared.
  }
  if(page.frames){
    for(const frame of page.frames().slice(1,7)){
      for(const selector of ['#captcha_container','#captcha_click_image']){
        const node=frame.locator(selector);
        if(await node.count()===1&&await node.isVisible())return true;
      }
    }
  }
  return false;
}

async function captureOnce(page,{check=()=>{}}={}){
  if(!onPlatform(page))return {reason:'unsupported_page'};
  let scope=page,activeSelectors=selectors,activeAdapter=adapter,prompt='#captcha_container',frameSource='';
  const frames=page.locator(frameSelector),frameCount=await frames.count();
  if(frameCount>1)return {reason:'frame_ambiguous',adapter:frameAdapter};
  if(frameCount===1&&await frames.isVisible()){
    scope=page.frameLocator(frameSelector);activeSelectors=frameSelectors;activeAdapter=frameAdapter;prompt='body';
    frameSource=await frames.getAttribute('src')||'';
  }else if(page.frames){
    const candidates=[];
    for(const frame of page.frames().slice(1,7)){
      const root=frame.locator('#captcha_container');
      if(await root.count()===1&&await root.isVisible())candidates.push(frame);
    }
    if(candidates.length>1)return {reason:'frame_ambiguous',adapter:'douyin_embedded_dom'};
    if(candidates.length===1){
      scope=candidates[0];frameSource=scope.url();activeAdapter='douyin_embedded_dom';
      if(await scope.locator('#captcha_verify_image').count()===1)activeSelectors=frameSelectors;
    }
  }
  const fail=reason=>({reason,adapter:activeAdapter});
  const clickImage=scope.locator('#captcha_click_image');
  if(await clickImage.count()===1&&await clickImage.isVisible())return point.capture(page,scope,clickImage,{
    check,frameSource:async()=>typeof scope.url==='function'?scope.url():await frames.getAttribute('src')||frameSource});
  let text;
  try{text=await scope.locator(prompt).innerText({timeout:750});}
  catch{return fail(activeAdapter===frameAdapter?'frame_not_ready':'prompt_not_ready');}
  if(/点选|依次|旋转|扫码/.test(text))return fail('unsupported_type');
  const elements={};
  for(const [key,selector] of Object.entries(activeSelectors)){
    const item=scope.locator(selector),count=await item.count();
    if(count===0)return fail(key+'_missing');
    if(count!==1)return fail(key+'_ambiguous');
    if(!await item.isVisible())return fail(key+'_hidden');
    elements[key]=item;
  }
  if(!/拖动.{0,12}滑块|拖动.{0,12}拼图|向右拖动/.test(text))return fail('unsupported_type');
  const readImage=async item=>item.evaluate(img=>{
    if(img.tagName!=='IMG')return {reason:'image_element_unsupported'};
    if(!img.complete||!img.naturalWidth||!img.naturalHeight)return {reason:'image_not_ready'};
    if(img.naturalWidth*img.naturalHeight>4000000||Math.max(img.naturalWidth,img.naturalHeight)>4096)return {reason:'image_size_unsupported'};
    try{
      const canvas=document.createElement('canvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
      canvas.getContext('2d').drawImage(img,0,0);
      const encoded=canvas.toDataURL('image/png').split(',')[1];
      if(encoded.length>1398104)return {reason:'image_size_unsupported'};
      return {image:encoded,width:img.naturalWidth,height:img.naturalHeight};
    }catch{return {reason:'image_pixels_unavailable'};} // No cross-transport image fetch.
  },undefined,{timeout:3000});
  const background=await readImage(elements.background),target=await readImage(elements.target);
  if(!background?.image||!target?.image)return fail(background?.reason||target?.reason||'image_pixels_unavailable');
  const boxes={};
  for(const [key,item] of Object.entries(elements))boxes[key]=await item.boundingBox({timeout:2000});
  if(Object.values(boxes).some(b=>!b||![b.x,b.y,b.width,b.height].every(Number.isFinite)||b.width<=0||b.height<=0))return fail('invalid_geometry');
  const payload={method:'slide_match',background_image:background.image,target_image:target.image};
  const identity={payload,boxes,url:page.url(),adapter:activeAdapter,frameSource};
  return {adapter:activeAdapter,payload,boxes,background_size:[background.width,background.height],
    target_size:[target.width,target.height],fingerprint:fingerprint(identity)};
}

async function observedImages(page,check){
  // Evidence for an unsupported image challenge, never input to the slider or
  // point solver. Only visible images inside one known captcha root are captured.
  if(!onPlatform(page)||!page.frames)return null;
  const candidates=[];
  for(const scope of page.frames().slice(0,6)){
    check();const root=scope.locator('#captcha_container');
    if(await root.count()!==1||!await root.isVisible())continue;
    const text=await root.innerText({timeout:750});
    if(/扫码|短信|手机|登录/.test(text)||!/验证|点击|旋转|拖动/.test(text))continue;
    const images=root.locator('img'),count=await images.count();if(count<1||count>4)continue;
    const visible=[];for(let i=0;i<count;i++)if(await images.nth(i).isVisible())visible.push(images.nth(i));
    if(visible.length<1||visible.length>2)continue;
    candidates.push(visible);
  }
  if(candidates.length!==1)return null;
  const images=[];
  for(const node of candidates[0]){
    check();const ready=await node.evaluate(el=>el.complete&&el.naturalWidth>0&&el.naturalHeight>0,undefined,{timeout:750});
    if(!ready)return null;
    const raw=await node.screenshot({timeout:2000,type:'png'});check();
    if(raw.length>1024*1024)return null;images.push(raw.toString('base64'));
  }
  return {method:'observed_images',images};
}

async function capture(page,{check=()=>{}}={}){
  const started=Date.now();
  for(let i=0;i<49;i++){
    check();const value=await captureOnce(page,{check});check();
    if(value.payload)return value;
    // The interstitial can precede its cross-origin frame by several seconds.
    // Wait for that frame without refreshing, submitting or replacing a challenge.
    const budget=/^(?:frame|prompt)_not_ready$/.test(value.reason)?12000:2000;
    if(!/_missing$|_hidden$|_not_ready$/.test(value.reason)||Date.now()-started>=budget||i===48){
      try{const evidence=await observedImages(page,check);if(evidence)value.evidence_payload=evidence;}catch{check();}
      return value;
    }
    // Wait only for DOM/image loading; never refresh or submit during this wait.
    await new Promise(resolve=>setTimeout(resolve,250));
  }
}

function movement(challenge,prediction){
  if(prediction.coordinate_type!=='center_xy_in_image_pixels'||
    JSON.stringify(prediction.background_size)!==JSON.stringify(challenge.background_size)||
    JSON.stringify(prediction.target_size)!==JSON.stringify(challenge.target_size))return null;
  const point=prediction.result?.target;
  if(!Array.isArray(point)||point.length!==2||!point.every(Number.isFinite))return null;
  const [width,height]=challenge.background_size,[tw,th]=challenge.target_size;
  const [cx,cy]=point,b=challenge.boxes.background,t=challenge.boxes.target,h=challenge.boxes.handle;
  const scale=b.width/width;
  if(cx<tw/2||cx>width-tw/2||cy<th/2||cy>height-th/2||
    Math.abs(b.height/height-scale)>0.01||Math.abs(t.width/tw-scale)>0.01||Math.abs(t.height/th-scale)>0.01)return null;
  const destination=b.x+(cx-Math.floor(tw/2))*scale;
  if(Math.abs((b.y+(cy-Math.floor(th/2))*scale)-t.y)>3)return null;
  const dx=destination-t.x;
  if(dx<=0||dx>b.width-t.width||t.x<b.x-1||t.x+t.width>b.x+b.width+1)return null;
  return {x:h.x+h.width/2,y:h.y+h.height/2,dx};
}

async function submit(page,challenge,prediction,check,beforeAction=()=>{}){
  check();
  const fresh=await capture(page,{check});
  check();
  if(fresh.fingerprint!==challenge.fingerprint)return {submitted:false,reason:'challenge_changed'};
  if(challenge.payload?.method==='same_shape_pair')return point.submit(page,fresh,prediction,check,beforeAction);
  const move=movement(challenge,prediction);
  if(!move)return {submitted:false,reason:'unsupported_geometry'};
  await page.mouse.move(move.x,move.y);check();
  beforeAction();
  await page.mouse.down();
  try{check();await page.mouse.move(move.x+move.dx,move.y,{steps:12});}
  finally{await page.mouse.up();}
  return {submitted:true};
}
module.exports={capture,submit,movement,adapter,promptVisible,describe};
