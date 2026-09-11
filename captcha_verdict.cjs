'use strict';
// Observe the platform's result after this attempt. Never infer a successful
// captcha from HTTP 200, an OCR score, a hidden dialog, or a late old response.
function messageVerdict(value){
  const text=typeof value==='string'?value.replace(/[\s\u200b-\u200d\ufeff]+/g,''):'';
  if(/^(?:验证成功|验证通过|VerificationSuccessful|CaptchaVerificationSucceeded)[。.!！]?$/i.test(text))return 'passed';
  if(/^(?:验证失败|验证不通过|验证未通过|验证码错误|验证错误|VerificationFailed|CaptchaVerificationFailed)[。.!！]?$/i.test(text))return 'failed';
  return 'unknown';
}
async function observe(page,{check=()=>{}}={}){
  let active=false,closed=false,pending,verdict={status:'unknown',source:'none'},timer;
  const unknown={arm(){},read:async()=>verdict,close:async()=>{}};
  if(!page.frames||!page.on||!page.off)return unknown;
  const candidates=[];
  try{
    for(const frame of page.frames().slice(0,6)){
      const root=frame.locator('#captcha_container');
      if(await root.count()===1&&await root.isVisible()&&await root.locator('img').count()>0)candidates.push({frame,root});
    }
  }catch{return unknown;}
  if(candidates.length!==1)return unknown;
  const {frame,root}=candidates[0],requests=new Set(),responses=new Set();
  let requestCount=0;
  const accept=(status,source)=>{
    if(!active||closed||status==='unknown')return;
    // Any contradictory result is a failure, never an optimistic success.
    if(verdict.status==='failed')return;
    verdict={status,source};
  };
  async function visibleResults(){
    try{
      return await root.evaluate(el=>{
        const visible=node=>{const r=node.getBoundingClientRect();if(!el.isConnected||!r.width||!r.height)return false;for(let p=node;p;p=p.parentElement){const s=getComputedStyle(p);if(s.visibility==='hidden'||s.display==='none'||Number(s.opacity)===0)return false;}return true;};
        return [...el.querySelectorAll('.vc-captcha-verify-button-text')].slice(0,4).filter(visible).map(node=>{
          const text=(node.innerText||'').replace(/[\s\u200b-\u200d\ufeff]+/g,'');
          return /^(?:验证成功|验证通过)[。！!]?$/u.test(text)?'passed':/^(?:验证失败|验证不通过|验证未通过|验证码错误|验证错误)[。！!]?$/u.test(text)?'failed':'unknown';
        });
      },undefined,{timeout:500});
    }catch{return [];} // A detached frame is unknown, not success.
  }
  // A result already visible before our input is stale. It must disappear and
  // reappear before it can describe this attempt.
  const baseline=await visibleResults(),stale=new Set(baseline.filter(s=>s!=='unknown'));
  async function sample(){
    if(!active||closed)return;
    const result=await visibleResults();
    for(const status of [...stale])if(!result.includes(status))stale.delete(status);
    for(const status of result)if(!stale.has(status))accept(status,'visible_platform_result');
  }
  function onRequest(request){
    if(!active||closed||requestCount>=4)return;
    try{
      const url=new URL(request.url()),owner=new URL(frame.url());
      if(request.frame()===frame&&request.method()==='POST'&&url.origin===owner.origin&&/\/captcha\/verify\/?$/.test(url.pathname)){requests.add(request);requestCount++;}
    }catch{}
  }
  function onResponse(response){
    if(!active||closed||!requests.has(response.request()))return;
    requests.delete(response.request());
    const work=(async()=>{
      try{
        const headers=await response.allHeaders();
        if(!/json/i.test(headers['content-type']||'')||Number(headers['content-length']||0)>65536)return;
        const body=await response.body();if(body.length>65536)return;
        const value=JSON.parse(body.toString('utf8'));if(!value||Array.isArray(value))return;
        const statuses=['message','msg'].filter(k=>typeof value[k]==='string').map(k=>messageVerdict(value[k]));
        if(statuses.includes('failed'))accept('failed','platform_response_message');
        else if(response.status()>=200&&response.status()<300&&statuses.includes('passed'))accept('passed','platform_response_message');
      }catch{}
    })();
    responses.add(work);work.finally(()=>responses.delete(work));
  }
  page.on('request',onRequest);page.on('response',onResponse);
  return {
    arm(){
      if(active||closed)return;active=true;
      // Polling observes vendor labels only; it performs no DOM or network writes.
      const tick=()=>{if(closed)return;pending=sample().finally(()=>{if(!closed)timer=setTimeout(tick,50);});};tick();
    },
    async read(){await sample();return {...verdict};},
    async close(){closed=true;clearTimeout(timer);page.off('request',onRequest);page.off('response',onResponse);await pending?.catch(()=>{});},
  };
}
module.exports={observe,messageVerdict};
