'use strict';
const readline=require('node:readline');
const {createReader}=require('./collector_reader.cjs');
const {runPool}=require('./collector_pool.cjs');
const {createVerification}=require('./collector_verification.cjs');
let outputTail=Promise.resolve();
const emit=value=>{const line=JSON.stringify(value)+'\n';outputTail=outputTail.then(()=>new Promise((resolve,reject)=>process.stdout.write(line,error=>error?reject(error):resolve())));return outputTail;};
const status=(value,detail)=>emit({type:'status',status:value,detail});
const reasons={needs_login:'页面要求登录。请在专用浏览器中登录后，点击工作台“继续读取”。',needs_verification:'页面出现验证，请在专用浏览器中自行完成，再点击“继续读取”。',needs_interaction:'当前页面没有可识别的评论数据。请在专用浏览器中打开评论区并加载评论，再点击“继续读取”。',rate_limited:'页面提示访问频繁或返回 429；本批已停止，不自动重试。',access_denied:'内容页面拒绝访问；本批已停止。'};
class Stop extends Error{constructor(code,detail){super(detail);this.code=code;}}
let config,context,gate,fatal,handleVerification,cancelled=false,timedOut=false,contextClosed=false;
const readers=[];
const input=readline.createInterface({input:process.stdin});
function releaseGate(){if(!gate)return;const old=gate;gate=null;old.resume?.();old.release();}
function check(){if(timedOut)throw new Stop('timeout','采集运行超过 15 分钟，本批停止；已入库数据与断点保留。');if(cancelled)throw new Stop('cancelled','用户已停止；已入库数据保留。');if(fatal)throw fatal;if(contextClosed)throw new Stop('interrupted','专用浏览器窗口已关闭；已入库数据与断点保留。');}
function fail(error){fatal ||= error;releaseGate();}
async function ready(reader){check();while(gate&&gate.owner!==reader){await gate.released;check();}}
async function pause(reader,code,detail=reasons[code]){
  await ready(reader);
  if(!gate){let release;const released=new Promise(r=>release=r);gate={owner:reader,released,release,resume:null};}
  const owned=gate;
  let timer;
  const response=new Promise((resolve,reject)=>{
    owned.resume=()=>{clearTimeout(timer);owned.resume=null;resolve();};
    timer=setTimeout(()=>{owned.resume=null;reject(new Stop('session_expired','等待人工操作超过 10 分钟；会话关闭，已入库数据保留。'));},600000);
  });
  // Attach a rejection handler before asynchronous diagnostics, without swallowing the awaited error.
  response.catch(()=>{});
  try{
    await reader.diagnose(code);
    if(code==='needs_verification'){
      owned.autoProcessing=true;
      try{if(await handleVerification(reader)){owned.resume?.();return;}}
      finally{owned.autoProcessing=false;}
      check();
      await reader.diagnose(code); // Capture the post-attempt state, not only the initial prompt.
    }
    if(!config.interactive)throw new Stop(code,'后台读取遇到需要处理的状态，本批已暂停并关闭后台浏览器；已有数据及已记录的验证码样本保留，请从工作台查看诊断。');
    await reader.page.bringToFront?.().catch(()=>{});
    await status(code,detail+' 其他读取页暂停后续操作；已发出的请求仍可能完成。');
    await response;check();
    await status('running','继续检查当前页面；未确认提示已解除前，其他页面继续等待。');
    // The owner releases the other lanes only after guard confirms its page is readable.
  }finally{clearTimeout(timer);}
}
input.on('line',line=>{
  let value;try{value=JSON.parse(line);}catch{return;}
  if(!config){config=value;main().finally(()=>input.close());return;}
  if(value.command==='cancel'){cancelled=true;releaseGate();context?.close().catch(()=>{});}
  if(value.command==='resume'&&!gate?.autoProcessing)gate?.resume?.();
});
input.on('close',()=>{cancelled=true;releaseGate();context?.close().catch(()=>{});});
async function main(){
  let chromium,devices;
  try{({chromium,devices}=require(process.env.CLUBOPS_PLAYWRIGHT||'playwright'));}catch{await status('dependency_missing','未安装 Playwright 浏览器控制库。');return;}
  const deadline=setTimeout(()=>{timedOut=true;releaseGate();context?.close().catch(()=>{});},900000);
  const shared={config,emit,status,Stop,reasons,check,ready,pause,fail,stopping:()=>cancelled||contextClosed||!!fatal||timedOut,
    release:reader=>{if(gate?.owner===reader&&!gate.resume)releaseGate();},
    running:detail=>gate?Promise.resolve():status('running',detail)};
  handleVerification=createVerification(shared);
  let peak=0;
  try{
    const concurrency=config.page_concurrency??1;
    if(!config.profile_dir||!['search','video'].includes(config.kind)||!Number.isInteger(concurrency)||concurrency<1||concurrency>4||config.comment_limit<1||config.comment_limit>100||config.video_limit<1||config.video_limit>5)throw new Stop('failed','任务参数无效。');
    await status('running',`正在启动${config.interactive?'专用 Chrome 窗口':'后台 Chrome，不显示窗口'}；本批视频读取并发上限 ${concurrency}，不是平台安全频率保证。`);
    context=await chromium.launchPersistentContext(config.profile_dir,{...require('./browser_config.cjs')(),userAgent:devices['Desktop Chrome'].userAgent,headless:!config.interactive,acceptDownloads:false,viewport:{width:1360,height:900},timeout:25000});
    context.on('close',()=>{contextClosed=true;releaseGate();});
    readers.push(createReader(context.pages()[0]||await context.newPage(),shared));
    const rows=config.resume_targets?.length?config.resume_targets:config.kind==='search'?await readers[0].discover():[{video_id:new URL(config.target).pathname.split('/').pop(),video_title:new URL(config.target).pathname.split('/').pop(),video_url:config.target}];
    if(!rows.length){await readers[0].diagnose('search-empty');throw new Stop('no_data','没有从搜索页面取得可识别的视频；不生成占位数据，也不代表搜索结果为空。');}
    if(rows.length>config.video_limit||new Set(rows.map(r=>r.video_id)).size!==rows.length)throw new Stop('failed','视频目标超出范围或重复。');
    await emit({type:'targets',records:rows});
    for(let slot=1;slot<Math.min(concurrency,rows.length);slot++){check();readers.push(createReader(await context.newPage(),shared));}
    const result=await runPool(rows,concurrency,(row,index,slot)=>readers[slot].collect(row),{
      check:()=>ready(null),onError:fail,
      onActivity:async value=>{peak=value.peak;await emit({type:'parallel',active_pages:value.active,peak_pages:peak,page_concurrency:concurrency});}
    });
    check();await Promise.all(readers.map(r=>r.settle()));
    const counts=readers.map(r=>r.counts()),videos=counts.reduce((n,r)=>n+r.videos,0),comments=counts.reduce((n,r)=>n+r.comments,0),errors=readers.reduce((n,r)=>n+r.errors(),0);
    const complete=result.outcomes.every(Boolean)&&!errors;
    const unavailable=result.outcomes.filter(r=>r?.unavailable).length;
    await status(complete?'completed':comments?'partial':errors?'schema_changed':'no_data',`本批观察到 ${videos} 个视频、${comments} 条评论；视频读取并发峰值 ${peak}。只覆盖已加载内容，不代表全部评论。`+(unavailable?` ${unavailable} 个作品明确不存在，已跳过，未读取其评论。`:'')+(errors?` ${errors} 次响应未能解析。`:''));
  }catch(error){
    fail(error);
    if(!cancelled)await Promise.all(readers.map(r=>r.diagnose('finished-error').catch(()=>{})));
    await Promise.all(readers.map(r=>r.settle()));
    const actual=timedOut?new Stop('timeout','采集运行超过 15 分钟，本批停止；已入库数据保留。'):cancelled?new Stop('cancelled','用户已停止；已入库数据保留。'):fatal;
    await status(actual instanceof Stop?actual.code:contextClosed?'interrupted':'failed',actual instanceof Stop?actual.message:`浏览器执行失败或窗口已关闭；已入库数据保留。错误类型：${['TimeoutError','TargetClosedError','Error'].includes(actual?.name)?actual.name:'UnknownError'}`);
  }finally{
    clearTimeout(deadline);releaseGate();await context?.close().catch(()=>{});
    await Promise.all(readers.map(r=>r.settle()));await outputTail;
  }
}
