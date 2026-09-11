'use strict';
const {createHash}=require('node:crypto');
const hash=value=>createHash('sha256').update(value).digest('hex');
const identify=value=>hash(JSON.stringify(value));
const validBox=b=>b&&[b.x,b.y,b.width,b.height].every(Number.isFinite)&&b.width>0&&b.height>0;

function kind(text){
  const first=String(text||'').trim().split(/[\r\n]+/)[0].replace(/\s+/g,'');
  return /^(?:请)?点击(?:图中)?两个形状相同的物体[。！!]?$/u.test(first)?'same_shape_pair':'';
}

async function capture(page,scope,img,{frameSource,check=()=>{}}){
  const adapter='douyin_same_shape_pair';
  const fail=reason=>({adapter,reason});
  const root=scope.locator('#captcha_container');
  const panels=await root.locator('.vc-captcha-verify-click-action').count();
  if(panels>1)return fail('point_prompt_or_image_unsupported');
  async function selectionCount(){
    const markers=root.locator('.vc-captcha-verify-img-point'),count=await markers.count();
    if(count>8)return -1;
    let visible=0;
    for(let i=0;i<count;i++)if(await markers.nth(i).isVisible())visible++;
    return visible;
  }
  if(await selectionCount()!==0)return fail('point_selection_already_present');
  async function state(){
    check();
    if(!await img.isVisible()||await root.count()!==1)return null;
    const prompt=kind(await root.innerText({timeout:750}));
    if(!prompt)return null;
    const info=await img.evaluate(el=>({source:el.currentSrc,loaded:el.complete,
      width:el.naturalWidth,height:el.naturalHeight}),undefined,{timeout:1000});
    if(!info.loaded||!info.width||!info.height)return null;
    const box=await img.boundingBox();
    if(!validBox(box)||info.width*info.height>4000000||Math.max(info.width,info.height)>4096)return null;
    return {prompt,source_hash:hash(info.source),natural_size:[info.width,info.height],box,
      frame_source_hash:hash(await frameSource()),page_url_hash:hash(page.url())};
  }
  const original=await state();
  if(!original)return fail('point_prompt_or_image_unsupported');
  // Capture only the visible challenge image, never the entire page or another
  // transport. Screenshot pixel dimensions are used independently of natural size.
  check();const image=await img.screenshot({timeout:4000,type:'png'});check();
  if(image.length>1024*1024||!image.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))return fail('image_size_unsupported');
  const size=[image.readUInt32BE(16),image.readUInt32BE(20)];
  if(size[0]*size[1]>4000000||Math.max(...size)>4096)return fail('image_size_unsupported');
  const identity=identify(original);
  if(identify(await state())!==identity)return fail('challenge_changed');
  const payload={method:'same_shape_pair',image:image.toString('base64')};
  async function confirmationControl(){
    const control=root.locator('.vc-captcha-verify-pc-button');
    if(await control.count()!==1||!await control.isVisible())return null;
    const isConfirm=text=>/^(?:确认|确定|确认提交|提交|验证|Confirm|Verify|Submit)$/i.test(text.replace(/[\s\u200b-\u200d\ufeff]+/g,''));
    const fullText=await control.innerText({timeout:500});
    let confirmed=isConfirm(fullText);
    if(!confirmed){
      // The live control contains separate state labels. Match the visible label,
      // never hidden success/loading text or an arbitrary button on the page.
      const labels=control.locator('.vc-captcha-verify-button-text'),count=await labels.count();
      if(count>4)return null;
      let matches=0,unknown=0;
      for(let i=0;i<count;i++)if(await labels.nth(i).isVisible()){
        if(isConfirm(await labels.nth(i).innerText({timeout:500})))matches++;else unknown++;
      }
      confirmed=matches===1&&unknown===0;
      if(!confirmed){
        const names=control.getByText(/^(?:确认|确定|确认提交|提交|验证|Confirm|Verify|Submit)$/i,{exact:true});
        const count=await names.count();if(count>4)return null;
        let visible=0;for(let i=0;i<count;i++)if(await names.nth(i).isVisible())visible++;
        confirmed=visible===1;
      }
      // Candidate for a caption rendered outside text nodes: the unique
      // pc-button and single hidden state-label constrain this fallback. Its
      // actual rendering still needs live confirmation; never accept a different
      // explicitly visible caption.
      if(!confirmed&&!fullText.trim()&&count===1&&!await labels.isVisible())confirmed=true;
    }
    if(!confirmed)return null;
    const button=control.locator('button');
    const target=await button.count()===1?button:control;
    if(!await target.isVisible()||!await target.isEnabled()||await target.getAttribute('aria-disabled')==='true')return null;
    return target;
  }
  return {adapter,payload,image_size:size,boxes:{image:original.box},fingerprint:identify({identity,payload,size}),
    unchanged:async()=>identify(await state())===identity,
    ...(panels===1?{selectionCount,confirmationControl,
      promptPresent:async()=>await root.count()===1&&await root.isVisible()}: {})};
}

function positions(challenge,prediction){
  const points=prediction.result?.points,size=challenge.image_size,box=challenge.boxes?.image;
  if(prediction.coordinate_type!=='xy_in_image_pixels'||JSON.stringify(prediction.image_size)!==JSON.stringify(size)||
     !Array.isArray(size)||size.length!==2||!size.every(n=>Number.isInteger(n)&&n>0)||!validBox(box)||
     !Array.isArray(points)||points.length!==2)return null;
  if(points.some(p=>!Array.isArray(p)||p.length!==2||!p.every(Number.isFinite)||p[0]<1||p[0]>=size[0]-1||p[1]<1||p[1]>=size[1]-1))return null;
  if(Math.hypot(points[0][0]-points[1][0],points[0][1]-points[1][1])<Math.min(...size)*.05)return null;
  if(Math.abs(box.width/size[0]-box.height/size[1])>.02)return null;
  return points.map(([x,y])=>({x:box.x+x*box.width/size[0],y:box.y+y*box.height/size[1]}));
}

async function submit(page,challenge,prediction,check,beforeAction){
  const points=positions(challenge,prediction);
  if(!points)return {submitted:false,reason:'unsupported_geometry'};
  if(!await challenge.unchanged())return {submitted:false,reason:'challenge_changed'};
  check();beforeAction();
  const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function registered(expected){
    for(let i=0;i<10;i++){
      check();
      try{
        if(!await challenge.promptPresent())return 'page_changed';
        if(!await challenge.unchanged()){
          if(!await challenge.promptPresent())return 'page_changed';
          throw Error('point_challenge_changed_during_selection');
        }
        if(await challenge.selectionCount()===expected)return 'registered';
      }catch(error){
        if(error.message==='point_challenge_changed_during_selection')throw error;
        return 'page_changed'; // No extra input; fresh-read verification decides the outcome.
      }
      await delay(50);
    }
    throw Error('point_selection_not_registered');
  }
  for(let i=0;i<2;i++){
    check();
    if(!await challenge.unchanged())throw Error('point_challenge_changed_during_selection');
    await page.mouse.click(points[i].x,points[i].y);
    check();
    if(challenge.selectionCount){
      if(await registered(i+1)==='page_changed')return {submitted:true};
      if(i===0){await delay(150);check();}
    }
  }
  if(challenge.confirmationControl){
    for(let i=0;i<10;i++){
      check();
      if(await registered(2)==='page_changed')return {submitted:true};
      const control=await challenge.confirmationControl();
      if(control){
        check();
        if(!await challenge.unchanged()||await challenge.selectionCount()!==2)throw Error('point_challenge_changed_during_selection');
        await control.click({timeout:1500});check();return {submitted:true};
      }
      await delay(50);
    }
    throw Error('point_confirmation_unavailable');
  }
  // Two selections and an applicable confirmation form one handling attempt;
  // Platform verdict and the reader's subsequent fresh response decide acceptance.
  return {submitted:true};
}
module.exports={kind,capture,positions,submit};
