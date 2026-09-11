'use strict';
const parser=require('./collector_parser.cjs');
const {OrderedResponseQueue}=require('./collector_queue.cjs');
const {promptVisible,describe}=require('./captcha_browser.cjs');

function retryAfterSeconds(response){
  const raw=String(response.headers?.()['retry-after']||'').trim();
  if(!raw||raw.length>100)return null;
  const seconds=/^\d+$/.test(raw)?Number(raw):Math.ceil((Date.parse(raw)-Date.now())/1000);
  return Number.isFinite(seconds)?Math.min(86401,Math.max(0,seconds)):null;
}

// Every page owns its URL scope, parsing state, budget and queue. No shared current-video state.
function createReader(page,shared){
  const {config,emit,status,Stop}=shared;
  let phase='idle',current=null,networkBlock='',recognized=false,hasMore=null;
  let requestSerial=0,lastValidRequest=0;
  const requestNumbers=new WeakMap();
  let readErrors=0,schemaErrors=0,commentResponses=0,invalidComments=0,processingLimit=false,navigationError='',navigationStatus=null,navigationRetryAfter=null;
  const searchResults=new Map(),commentIds=new Set(),videoIds=new Set(),perVideo=new Map(),responseMeta=[];
  const queue=new OrderedResponseQueue({concurrency:2,capacity:8,onError:()=>{readErrors++;}});
  function check(){shared.check();if(page.isClosed())throw new Stop('interrupted','采集页面已关闭；本批停止，已入库数据保留。');if(processingLimit)throw new Stop('resource_limited','单页待处理响应超过 8 条，本批停止；已入库数据保留。');}
  async function ready(){await shared.ready(reader);check();}
  async function wait(ms){const end=Date.now()+ms;while(Date.now()<end){check();await new Promise(r=>setTimeout(r,Math.min(250,end-Date.now())));}}
  async function bounded(promise,ms){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('timeout')),ms);})]);}finally{clearTimeout(timer);}}
  async function visibleText(){return (await page.locator('body').innerText({timeout:3000}).catch(()=>'')).slice(0,100000);}
  async function diagnose(stage){
    if(page.isClosed())return;
    if(stage==='needs_verification'){
      await emit({type:'diagnostic',stage:'captcha_dom',snapshot:{title:'验证码页面结构（不含图片与凭证）',visible_text:JSON.stringify(await describe(page)),responses:[]}});
    }
    let url='';try{const u=new URL(page.url());url=u.protocol==='https:'&&u.hostname==='www.douyin.com'?u.origin+u.pathname:'非抖音内容页';}catch{}
    await emit({type:'diagnostic',stage,snapshot:{title:await page.title().catch(()=>''),page_url:url,visible_text:(await visibleText()).slice(0,2500),video_links:await page.locator('a[href*="/video/"]').count().catch(()=>0),responses:responseMeta.slice(-15),navigation_error:navigationError,navigation_http_status:navigationStatus,navigation_retry_after_seconds:navigationRetryAfter,processing:{pending:queue.pending,high_water:queue.highWater,capacity:8,concurrency:2}}});
  }
  async function pause(code,detail){await shared.pause(reader,code,detail);networkBlock='';}
  async function guard(){
    for(let attempt=0;attempt<3;attempt++){
      await ready();
      const code=networkBlock||parser.blockFromText((await page.title().catch(()=>''))+'\n'+await visibleText())||
        (await promptVisible(page)?'needs_verification':'');
      if(!code){shared.release(reader);return;}
      if(code==='rate_limited'||code==='access_denied')throw new Stop(code,shared.reasons[code]);
      await pause(code);
    }
    throw new Stop('no_data','页面仍未开放数据，结束本批；请处理页面提示后新建任务。');
  }
  async function observeVideo(){if(current&&!videoIds.has(current.video_id)){videoIds.add(current.video_id);await emit({type:'video',record:current});}}
  function onResponse(response){
    if(shared.stopping()||phase==='idle')return;
    const kind=parser.responseKind(response.url(),current?.video_id||'');
    if(!kind||(phase==='search'&&kind!=='search')||(phase==='comment'&&kind!=='comment'))return;
    const requestNumber=response.request?requestNumbers.get(response.request())||0:0;
    if(kind==='comment')commentResponses++;
    const http=response.status(),meta={kind,status:http};responseMeta.push(meta);if(responseMeta.length>30)responseMeta.shift();
    if(http===429||http===403){networkBlock=http===429?'rate_limited':'access_denied';shared.fail(new Stop(networkBlock,shared.reasons[networkBlock]));return;}
    if(http===401){networkBlock='needs_login';return;}
    if(http>=500&&http<=599){meta.retry_after_seconds=retryAfterSeconds(response);shared.fail(new Stop('network_error',`数据接口暂不可用（HTTP ${http}），本批停止并保留已读取内容。`));return;}
    if(http!==200)return;
    const expected=current;
    if(!queue.submit(async()=>{
      const headers=response.headers(),contentType=String(headers['content-type']||'').toLowerCase();
      meta.content_kind=contentType.includes('json')?'json':contentType.includes('html')?'html':contentType?'other':'missing';
      if(Number(headers['content-length']||0)>8_000_000){meta.body_error='body_too_large';throw Error('Response exceeds byte budget');}
      let raw;
      try{raw=await bounded(response.body(),7000);}
      catch(error){meta.body_error=error.message==='timeout'?'body_timeout':'body_unavailable';throw Error('Response body unavailable');}
      meta.body_bytes=Math.min(raw.length,8_000_001);
      if(raw.length>8_000_000){meta.body_error='body_too_large';throw Error('Response exceeds byte budget');}
      const text=raw.toString('utf8');
      if(!text.trim()){meta.body_error='empty_body';throw Error('Empty response body');}
      try{return JSON.parse(text);}
      catch{meta.body_error='invalid_json';throw Error('Invalid response JSON');}
    },async body=>{
      if(shared.stopping()||page.isClosed()||processingLimit)return;
      meta.keys=Object.keys(body||{}).slice(0,25);meta.data_type=Array.isArray(body?.data)?'array':typeof body?.data;
      const verification=parser.blockFromBody(body);
      if(verification){networkBlock=verification;return;}
      if(kind==='search'){
        const rows=parser.searchVideos(body);
        for(const row of rows)if(searchResults.size<50)searchResults.set(row.video_id,row);
        if(body?.status_code===0&&rows.length){lastValidRequest=requestNumber;if(networkBlock==='needs_verification')networkBlock='';}
        return;
      }
      meta.comments_type=Array.isArray(body?.comments)?'array':body?.comments===null?'null':typeof body?.comments;
      if(Array.isArray(body?.comments))meta.comments_count=body.comments.length;
      for(const key of ['status_code','has_more','total'])if(Number.isSafeInteger(body?.[key]))meta[key]=body[key];
      if(!expected)return;
      if(!expected.video_title||expected.video_title===expected.video_id){
        const title=parser.pageVideoTitle(await page.title().catch(()=>''),page.url(),expected.video_id);if(title)expected.video_title=title;
      }
      const parsed=parser.comments(body,expected.video_id,expected);
      if(body?.status_code===0&&parsed.recognized&&!parsed.skipped&&!parsed.truncated){lastValidRequest=requestNumber;if(networkBlock==='needs_verification')networkBlock='';}
      if(!parsed.recognized)schemaErrors++;
      if(parsed.truncated)schemaErrors++;
      invalidComments+=parsed.skipped;recognized ||= parsed.recognized;hasMore=parsed.hasMore;
      if(parsed.recognized)await observeVideo();
      if(parsed.skipped)await emit({type:'skipped',count:parsed.skipped});
      for(const row of parsed.rows){
        if(commentIds.has(row.comment_id)||(perVideo.get(row.video_id)||0)>=config.comment_limit)continue;
        commentIds.add(row.comment_id);perVideo.set(row.video_id,(perVideo.get(row.video_id)||0)+1);
        await emit({type:'comment',record:row});
      }
    })) {processingLimit=true;shared.fail(new Stop('resource_limited','单页待处理响应超过 8 条，本批停止；已入库数据保留。'));}
  }
  async function drain(){check();await queue.drain();check();}
  async function goto(url){
    await ready();networkBlock='';navigationError='';navigationStatus=null;navigationRetryAfter=null;
    try{const r=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
      const http=r?.status();
      navigationStatus=Number.isInteger(http)?http:null;
      if(http>=500&&http<=599){
        navigationRetryAfter=retryAfterSeconds(r);
        const error=new Stop('network_error',`页面暂不可用（HTTP ${http}），本批停止并保留已读取内容。`);
        shared.fail(error);throw error;
      }
      if(http===429||http===403){
        networkBlock=http===429?'rate_limited':'access_denied';
        const error=new Stop(networkBlock,shared.reasons[networkBlock]);shared.fail(error);throw error;
      }
      if(http===401)networkBlock='needs_login';
    }catch(e){check();navigationError=(e.message.match(/net::ERR_[A-Z_]+/)||[])[0]||(e.name==='TimeoutError'?'navigation_timeout':'navigation_failed');}
    // Known authentication requirements are checked before the normal page-settle delay.
    if(networkBlock)await guard();
    await wait(5000);await drain();await guard();
    if(navigationError.startsWith('net::ERR_')){await diagnose('navigation');throw new Stop('network_error','浏览器页面未能连接：'+navigationError+'；本批已停止。');}
  }
  async function discover(){
    phase='search';await goto('https://www.douyin.com/search/'+encodeURIComponent(config.target)+'?type=video');
    async function readAnchors(){
      const anchors=await page.locator('a[href*="/video/"]').evaluateAll(nodes=>nodes.slice(0,250).map(n=>({href:n.href,label:n.innerText?.slice(0,5000)}))).catch(()=>[]);
      for(const a of anchors){let u;try{u=new URL(a.href);}catch{continue;}
        const m=u.pathname.match(/^\/video\/(\d{5,30})\/?$/);if(searchResults.size>=50)break;
        if(u.protocol==='https:'&&u.hostname==='www.douyin.com'&&m&&!searchResults.has(m[1]))searchResults.set(m[1],{video_id:m[1],video_title:a.label?.trim().slice(0,5000)||m[1],video_url:`https://www.douyin.com/video/${m[1]}`});
      }
    }
    for(let i=0;i<4&&searchResults.size<config.video_limit;i++){
      await readAnchors();if(searchResults.size>=config.video_limit)break;
      await guard();await page.mouse.wheel(0,700);await wait(2000);await drain();
    }
    if(!searchResults.size&&config.interactive){await pause('needs_interaction','搜索页没有可识别的视频。请在专用浏览器中完成登录或正常搜索，再点击“继续读取”。');await drain();await guard();await readAnchors();}
    phase='idle';await drain();
    if(!searchResults.size){
      const failures=responseMeta.filter(m=>m.kind==='search'&&m.body_error);
      if(failures.length){
        await diagnose('search-response-unreadable');
        const code=failures.every(m=>m.body_error==='empty_body')?'empty_response':
          failures.some(m=>['body_timeout','body_unavailable'].includes(m.body_error))?'network_error':'schema_changed';
        throw new Stop(code,code==='empty_response'?'搜索接口返回了空响应，未取得视频；本批暂停，不能将其当作没有相关作品。':
          code==='network_error'?'未能完整读取搜索响应，本批暂停；不自动重试。':'搜索响应无法按预期格式解析，本批暂停；不将其当作零结果。');
      }
    }
    const candidates=[...searchResults.values()],selected=require('./candidate_select.cjs').select(candidates,config.video_limit,config.candidate_policy);
    if(candidates.length&&config.candidate_policy){
      check();await emit({type:'candidates',records:candidates.map(row=>({video_id:row.video_id,video_title:row.video_title.slice(0,300)}))});
      await emit({type:'diagnostic',stage:'candidate_selection',snapshot:{title:'搜索候选轮换',
        visible_text:`本次搜索返回 ${candidates.length} 个候选，按读取历史轮换 ${selected.length} 个作品；保留探索位置，其余优先复查出现新近期评论的作品。`,
        responses:[{policy:config.candidate_policy.version,candidate_count:candidates.length,selected:selected.map(r=>r.video_id),scope:'current_search_response',all_douyin:false}]}});
    }
    return selected;
  }
  async function collect(row){
    phase='idle';await drain();await ready();current=row;recognized=false;hasMore=null;commentResponses=0;invalidComments=0;
    const errorsAtStart=readErrors+schemaErrors;
    await emit({type:'checkpoint',video_id:row.video_id,status:'reading',detail:'正在读取页面已加载评论'});
    if(config.kind==='search')await observeVideo();
    phase='comment';await shared.running(`正在读取视频 ${row.video_id} 的已加载评论（最多 ${config.comment_limit} 条）。`);
    await goto(row.video_url);
    if(!recognized){
      await ready();const buttons=page.getByRole('button',{name:/^评论(?:\s|\d|$)/});
      if(await buttons.count()===1&&await buttons.first().isVisible()){await buttons.first().click({timeout:3000}).catch(()=>{});await wait(2500);await drain();}
    }
    if(!recognized&&commentResponses){await diagnose('comment-schema');throw new Stop('schema_changed','收到了评论响应，但结构未能识别；不能将其当作零评论或完成采集。');}
    if(!recognized){await pause('needs_interaction');await drain();await guard();}
    if(invalidComments&&!(perVideo.get(row.video_id)||0)){await diagnose('comment-invalid');throw new Stop('schema_changed','评论缺少有效原始 ID 或内容，未入库；请检查适配器。');}
    if(!recognized){await emit({type:'checkpoint',video_id:row.video_id,status:'partial',detail:'没有可识别的评论响应'});return false;}
    for(let i=0;i<6&&(perVideo.get(row.video_id)||0)<config.comment_limit&&hasMore!==false;i++){
      await guard();await drain();if((perVideo.get(row.video_id)||0)>=config.comment_limit||hasMore===false)break;
      const regions=page.locator('[data-e2e="comment-list"]');if(await regions.count()!==1||!await regions.isVisible())break;
      try{await regions.hover({timeout:3000});}catch{check();await drain();if((perVideo.get(row.video_id)||0)<config.comment_limit)await diagnose('comment-scroll-unavailable');break;}
      await drain();if((perVideo.get(row.video_id)||0)>=config.comment_limit||hasMore===false)break;
      await ready();await page.mouse.wheel(0,650);await wait(2000);await drain();
    }
    await guard();phase='idle';await drain();
    const errors=readErrors+schemaErrors-errorsAtStart,done=!errors&&(hasMore===false||(perVideo.get(row.video_id)||0)>=config.comment_limit);
    await emit({type:'checkpoint',video_id:row.video_id,status:done?'done':'partial',detail:done?'已读完本批预算或页面声明没有更多评论':errors?`${errors} 次响应解析失败，保留已读记录；不能确认本批完整性`:'取得部分评论，但页面未能继续加载'});
    return done;
  }
  page.on('request',request=>requestNumbers.set(request,++requestSerial));
  page.on('response',onResponse);page.on('dialog',d=>d.dismiss().catch(()=>{}));
  const reader={page,discover,collect,diagnose,readVersion:()=>requestSerial,
    settleVerification:drain,
    async readableAfter(version){
      if(lastValidRequest<=version||networkBlock||phase==='idle')return false;
      try{
        const expected=phase==='search'?'https://www.douyin.com/search/'+encodeURIComponent(config.target):current.video_url;
        const actual=new URL(page.url()),wanted=new URL(expected);
        if(actual.origin!==wanted.origin||actual.pathname!==wanted.pathname)return false;
        const title=await page.title(),text=await page.locator('body').innerText({timeout:1000});
        if(!text.trim()||parser.blockFromText(title+'\n'+text))return false;
        if(await promptVisible(page))return false;
        return true;
      }catch{return false;} // An unreadable page is not evidence that the prompt cleared.
    },
    errors:()=>readErrors+schemaErrors,counts:()=>({videos:videoIds.size,comments:commentIds.size}),async settle(){phase='idle';await queue.drain();}};
  return reader;
}
module.exports={createReader};
