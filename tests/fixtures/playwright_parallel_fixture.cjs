'use strict';
// Synthetic multi-page browser. No network, platform account or persistent profile.
const {EventEmitter}=require('node:events');
const scenario=process.env.CLUBOPS_FIXTURE_SCENARIO;
const ids=['7600000000000000101','7600000000000000102','7600000000000000103'];
let verified=false,ctx;
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const trace=value=>process.stdout.write(JSON.stringify({type:'fixture',...value})+'\n');
process.stdin.on('data',data=>{
  if(String(data).includes('"command":"resume"')&&scenario!=='gate-persistent')verified=true;
  if(String(data).includes('"command":"close_window"'))ctx?.close();
});
const response=(url,body,status=200,lag=0)=>({url:()=>url,status:()=>status,headers:()=>({}),body:async()=>{await delay(lag);return Buffer.from(JSON.stringify(body));}});
class Context extends EventEmitter{
  constructor(){super();this.closed=false;this.list=[new Page(this)];this.timers=[];}
  pages(){return this.list;}
  async newPage(){const p=new Page(this);this.list.push(p);return p;}
  async close(){if(this.closed)return;this.closed=true;for(const timer of this.timers)clearTimeout(timer);this.emit('close');}
}
class Page extends EventEmitter{
  constructor(context){super();this.context=context;this.address='about:blank';this.mouse={wheel:async()=>trace({action:'wheel',video_id:this.vid})};}
  url(){return this.address;}
  isClosed(){return this.context.closed;}
  async bringToFront(){trace({action:'foreground',video_id:this.vid});}
  challenge(){return scenario.startsWith('gate-')&&this.vid===ids[1]&&!verified;}
  async title(){return this.challenge()?'验证码中间页':`合成视频 ${this.vid||''} - 抖音`;}
  async goto(url){
    this.address=url;
    if(url.includes('/search/')){
      this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{data:ids.map(aweme_id=>({aweme_info:{aweme_id,desc:`合成视频 ${aweme_id}`}}))}));
      return {status:()=>200};
    }
    this.vid=url.split('/').pop();trace({action:'goto',video_id:this.vid});
    if(scenario.startsWith('navigation-')&&this.vid===ids[1]){
      const http=scenario==='navigation-rate-limit'?429:scenario==='navigation-upstream'?502:403;
      trace({action:'navigation-denied',video_id:this.vid,http});return {status:()=>http};
    }
    const endpoint=`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${this.vid}`;
    if(scenario==='rate-limit'&&this.vid===ids[1]){this.emit('response',response(endpoint,{},429));return {status:()=>200};}
    if(scenario==='gate-rate-limit'&&this.vid===ids[0])this.context.timers.push(setTimeout(()=>this.emit('response',response(endpoint,{},429)),5500));
    const wrong=scenario==='wrong-video'&&this.vid===ids[1];
    const body={status_code:0,has_more:0,comments:[1,2].map(n=>({cid:`${this.vid.slice(0,-1)}${n+3}`,aweme_id:wrong?ids[0]:this.vid,text:`合成原文来自 ${this.vid} 条目 ${n}`,user:{uid:`${this.vid.slice(0,-1)}${n+6}`,nickname:'合成用户'}}))};
    // Do not reuse comment IDs across the different video fixtures.
    for(let i=0;i<body.comments.length;i++){body.comments[i].cid=`${this.vid}${i}`;body.comments[i].user.uid=`${this.vid}${i+5}`;}
    this.emit('response',response(endpoint,body,200,this.vid===ids[0]?50:5));
    if(scenario.startsWith('gate-')&&this.vid===ids[0])await delay(250);
    return {status:()=>200};
  }
  locator(selector){const self=this;return {innerText:async()=>self.challenge()?'请完成安全验证':'合成公开评论',count:async()=>0,evaluateAll:async()=>[],isVisible:async()=>false,hover:async()=>{},first(){return this;},click:async()=>{}};}
  getByRole(){return this.locator('button');}
  getByText(){return {count:async()=>0,isVisible:async()=>false};}
}
module.exports={devices:{'Desktop Chrome':{userAgent:'fixture Desktop Chrome'}},chromium:{launchPersistentContext:async(profile,options)=>{if(options.userAgent!=='fixture Desktop Chrome')throw Error('Desktop client configuration missing');ctx=new Context();return ctx;}}};
