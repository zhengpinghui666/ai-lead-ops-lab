'use strict';
// In-memory browser double. No browser, cookies, platform requests or live profile.
const {EventEmitter}=require('node:events');
const fs=require('node:fs');
const path=require('node:path');
const scenario=process.env.CLUBOPS_FIXTURE_SCENARIO||'accepted';
const iframeScenario=scenario.startsWith('iframe-');
const samples=path.join(__dirname,'captcha');
const images=Object.fromEntries(['target','background'].map(name=>[name,fs.readFileSync(path.join(samples,`${name}.png`)).toString('base64')]));
const vid='7600000000000000801';
const trace=action=>process.stdout.write(JSON.stringify({type:'fixture',action})+'\n');
const response=(url,body,request)=>({url:()=>url,request:()=>request,status:()=>200,headers:()=>({}),allHeaders:async()=>({'content-type':'application/json'}),body:async()=>Buffer.from(JSON.stringify(body))});
class Context extends EventEmitter{
  constructor(){super();this.closed=false;this.page=new Page(this);}
  pages(){return [this.page];}
  async newPage(){return this.page;}
  async close(){this.closed=true;this.emit('close');}
}
class Page extends EventEmitter{
  constructor(ctx){super();this.ctx=ctx;this.address='about:blank';this.challenge=false;this.captures=0;
    this.mouse={wheel:async()=>trace('wheel'),move:async()=>{},down:async()=>trace('mouse-down'),up:async()=>{
      trace('mouse-up');
      if(scenario==='rejected')return;
      const verify={url:()=> 'https://www.douyin.com/captcha/verify',frame:()=>this,method:()=> 'POST'};
      this.emit('request',verify);this.emit('response',response(verify.url(),{message:'验证通过'},verify));
      this.challenge=scenario==='iframe-stays';
      if(scenario==='no-response')return;
      this.readResponse();
    }};
  }
  isClosed(){return this.ctx.closed;}
  url(){return this.address;}
  frames(){return [this];}
  async title(){return this.challenge?'验证码中间页':'合成采集页面';}
  async bringToFront(){}
  readResponse(){
    const request=scenario==='old-response'?this.oldRequest:{};
    if(scenario!=='old-response')this.emit('request',request);
    if(this.address.includes('/search/'))this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{status_code:0,data:[{aweme_info:{aweme_id:vid,desc:'合成验证码流程'}}]},request));
    else this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,{status_code:0,has_more:0,comments:[{cid:vid+'1',aweme_id:vid,text:'合成测试：找陪练预算100',user:{uid:vid+'2',nickname:'合成样例'}}]},request));
  }
  async goto(url){this.address=url;trace(url.includes('/search/')?'search':'video');
    if(url.includes('/search/')||scenario==='second-challenge'){
      this.challenge=true;
      const endpoint=url.includes('/search/')?'https://www.douyin.com/aweme/v1/web/general/search/single/':`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`;
      const request={};this.emit('request',request);
      this.emit('response',response(endpoint,{status_code:0,search_nil_info:{search_nil_type:'verify_check'},data:[],has_more:0},request));
      if(scenario==='old-response'){this.oldRequest={};this.emit('request',this.oldRequest);}
    }else this.readResponse();
    return {status:()=>200};
  }
  locator(selector){return this.locatorInScope(selector,false);}
  frameLocator(){return {locator:selector=>this.locatorInScope(selector,true)};}
  locatorInScope(selector,inFrame){const page=this;
    if(selector==='#captcha_container')return {count:async()=>page.challenge?1:0,isVisible:async()=>page.challenge,
      innerText:async()=>page.challenge?'拖动滑块完成拼图':'',locator:()=>({count:async()=>2}),evaluate:async()=>[]};
    const image=selector===(inFrame?'#captcha_verify_image':'#captcha-verify-image')?'background':
      (inFrame?selector==='#captcha-verify_img_slide':selector.startsWith('xpath='))?'target':'';
    const captcha=!!image||selector===(inFrame?'.captcha-slider-btn':'#secsdk-captcha-drag-wrapper > div:nth-child(2)');
    const exists=captcha?(inFrame===iframeScenario):selector==='#captcha_container > iframe'?iframeScenario:false;
    return {count:async()=>exists&&page.challenge&&scenario!=='unsupported'?1:0,
      isVisible:async()=>exists&&page.challenge,
      getAttribute:async()=>'/fixture-frame',
      innerText:async()=>page.challenge&&(!iframeScenario||inFrame)?'拖动滑块完成拼图':'合成采集内容',
      evaluate:async()=>{if(image==='background')page.captures++;
        if(scenario==='changed'&&page.captures>1)return {image:images[image]+'AAAA',width:image==='background'?360:36,height:image==='background'?190:36};
        return {image:images[image],width:image==='background'?360:36,height:image==='background'?190:36};},
      boundingBox:async()=>image==='background'?{x:100,y:100,width:360,height:190}:image==='target'?{x:100,y:147,width:36,height:36}:{x:100,y:310,width:30,height:30},
      evaluateAll:async()=>[],first(){return this;},nth(){return this;},hover:async()=>{},click:async()=>{}};
  }
  getByRole(){return this.locator('button');}
  getByText(){return {count:async()=>0,isVisible:async()=>false};}
}
module.exports={devices:{'Desktop Chrome':{userAgent:'fixture Desktop Chrome'}},chromium:{launchPersistentContext:async(profile,options)=>{if(options.userAgent!=='fixture Desktop Chrome')throw Error('Desktop client configuration missing');trace(options.headless?'headless-browser':'visible-browser');return new Context();}}};
