'use strict';
// ISOLATED SYNTHETIC TEST DOUBLE: no browser, no HTTP, no Douyin access, no live DB.
const {EventEmitter}=require('node:events');
const scenario=process.env.CLUBOPS_FIXTURE_SCENARIO||'success';
const vid='7600000000000000001';
let verified=false;
let searchKeyword='';
process.stdin.on('data',chunk=>{if(String(chunk).includes('"command":"resume"'))verified=true;});
const response=(url,body)=>{
  if(url.includes('/search/')&&!url.includes('?'))url+='?keyword='+encodeURIComponent(searchKeyword);
  if(scenario.startsWith('alternate-'))url=url.replace('www.douyin.com','www-hj.douyin.com');
  if(scenario==='alternate-wrong-video'&&url.includes('/comment/'))url=url.replace(vid,'7600000000000000009');
  const blocked=scenario==='alternate-forbidden'&&url.includes('/comment/');
  const limited=scenario==='alternate-limited'&&url.includes('/comment/');
  if(scenario==='alternate-schema'&&url.includes('/comment/'))body={status_code:0,unknown:[]};
  return {url:()=>url,status:()=>blocked?403:limited?429:200,headers:()=>({'content-type':'application/json'}),body:async()=>Buffer.from(JSON.stringify(body))};
};
class Context extends EventEmitter {
  constructor(){super();this.closed=false;this.page=new Page(this);}
  pages(){return [this.page];}
  async newPage(){return this.page;}
  async close(){if(this.closed)return;this.closed=true;clearTimeout(this.closeTimer);clearTimeout(this.loadingTimer);this.emit('close');}
}
class Page extends EventEmitter {
  constructor(ctx){super();this.ctx=ctx;this.address='about:blank';this.mouse={wheel:async()=>{}};}
  url(){return this.address;}
  isClosed(){return this.ctx.closed;}
  async title(){return scenario==='foreign-video'?'无畏契约港服陪玩 - 抖音':scenario==='mobile-video'?'无畏契约手游陪玩 - 抖音':scenario==='gateway-title-only'?'502 Bad Gateway':scenario==='public-comments-login-to-post'?'合成夹具：无畏契约陪玩 - 抖音':scenario.includes('verification')&&!verified?'验证码中间页':'合成夹具页面';}
  async goto(url){
    this.address=url;
    if((scenario.startsWith('comment-loading-')||scenario.startsWith('panel-loading-'))&&!url.includes('/search/')){
      this.loading=true;
      if(!scenario.endsWith('-timeout')&&!scenario.endsWith('-unrelated'))this.ctx.loadingTimer=setTimeout(()=>{
        this.loading=false;
        const reply=response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,
          {status_code:0,has_more:0,comments:[{cid:'7600000000000000002',aweme_id:vid,text:'迟到但身份匹配的评论',user:{uid:'123456789012'}}]});
        if(scenario.endsWith('-limited'))reply.status=()=>429;
        if(scenario.endsWith('-login'))reply.status=()=>401;
        this.emit('response',reply);
      },6500);
      return {status:()=>200};
    }
    if(url.includes('/search/'))searchKeyword=decodeURIComponent(new URL(url).pathname.slice('/search/'.length));
    if(scenario.startsWith('note-')&&!url.includes('/search/')){
      this.address=`https://${scenario==='note-wrong-origin'?'other.example':'www.douyin.com'}/note/${scenario==='note-wrong-post'?'7600000000000000009':vid}`;
      return {status:()=>200};
    }
    if((scenario==='unavailable-video'||scenario==='unavailable-first'&&url.endsWith(vid))&&!url.includes('/search/'))return {status:()=>200};
    if(scenario==='comment-navigation-timeout'&&!url.includes('/search/')){
      const error=Error('synthetic navigation timeout');error.name='TimeoutError';throw error;
    }
    if(scenario==='navigation-502')return {status:()=>502,headers:()=>({'retry-after':'90','set-cookie':'PRIVATE_RESPONSE_SENTINEL'})};
    if(scenario==='gateway-title-only')return {status:()=>200};
    if(scenario==='search-503'||scenario==='comment-503'&&!url.includes('/search/')){
      const endpoint=scenario==='search-503'?'https://www.douyin.com/aweme/v1/web/general/search/single/?keyword='+encodeURIComponent(searchKeyword):`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`;
      this.emit('response',{url:()=>endpoint,status:()=>503,headers:()=>({'retry-after':'180','set-cookie':'PRIVATE_RESPONSE_SENTINEL'}),body:async()=>{throw Error('Must not parse 503 body');}});
      return {status:()=>200};
    }
    if(scenario==='verification-window-close')this.ctx.closeTimer=setTimeout(()=>this.ctx.close(),6000);
    if(scenario.startsWith('verification')||scenario==='device-challenge')return {status:()=>200};
    if(url.includes('/search/')){
      if(scenario.startsWith('search-json-')){
        const body={status_code:0,data:[],aweme_list:null,has_more:0,cursor:16,
          search_nil_info:{search_nil_type:scenario==='search-json-unknown'?'unknown_empty':'service_empty'},private:'PRIVATE_RESPONSE_SENTINEL'};
        this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',
          body));
        if(scenario==='search-json-mixed')this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{...body,data:[{unknown:'PRIVATE_RESPONSE_SENTINEL'}]}));
        return {status:()=>200};
      }
      if(scenario==='search-scope-dom')return {status:()=>200};
      if(scenario.startsWith('search-scope-')){
        if(scenario==='search-scope-foreign')this.address='https://www.douyin.com/search/another-query';
        this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=another-query',
          {data:[{aweme_info:{aweme_id:'7600000000000000010',desc:'无畏契约：其他搜索请求'}}]}));
        const rows=[{aweme_info:{aweme_id:'7600000000000000009',desc:'合成不相关作品：装修瓷砖瓦片'}}];
        if(scenario!=='search-scope-empty')rows.push({aweme_info:{aweme_id:vid,desc:'無畏契約 #瓦 找队友'}});
        this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{data:rows}));
        return {status:()=>200};
      }
      if(scenario==='candidate-rotation'||scenario==='unavailable-first'){
        this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{data:[vid,'7600000000000000009','7600000000000000010'].map(aweme_id=>({aweme_info:{aweme_id,desc:'合成候选 '+aweme_id}}))}));
        return {status:()=>200};
      }
      if(scenario==='resume-search-dom')return {status:()=>200};
      if(['search-empty-body','search-html-body','search-body-unavailable'].includes(scenario)){
        const reply=response('https://www.douyin.com/aweme/v1/web/general/search/single/',{});
        reply.headers=()=>({'content-type':scenario==='search-html-body'?'text/html':'application/json'});
        reply.body=async()=>{if(scenario==='search-body-unavailable')throw Error('PRIVATE_RESPONSE_SENTINEL');return Buffer.from(scenario==='search-html-body'?'<html>PRIVATE_RESPONSE_SENTINEL</html>':'');};
        this.emit('response',reply);return {status:()=>200};
      }
      if(scenario==='queue-overflow'){
        for(let i=0;i<9;i++)this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{data:[]}));
        return {status:()=>200};
      }
      this.emit('response',response('https://www.douyin.com/aweme/v1/web/general/search/single/',{data:[{aweme_info:{aweme_id:vid,desc:'合成夹具：无畏契约陪练'}}]}));
    }else {
      if(scenario==='candidate-rotation'||scenario==='unavailable-first'){
        const actual=url.split('/').pop();
        this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${actual}`,{status_code:0,has_more:0,comments:[{cid:'7600000000000000999',aweme_id:actual,text:'合成轮换评论',user:{uid:'123456789012'}}]}));
        return {status:()=>200};
      }
      const body=scenario==='schema-error'?{status_code:99,comments:[]}:{status_code:0,comments:[{cid:scenario==='invalid-id'?7600000000000000002:'7600000000000000002',aweme_id:vid,text:'合成夹具评论：找个陪练',user:{uid:'123456789012',nickname:'测试夹具'},create_time:1750000000}],has_more:0};
      if(scenario==='nontext-only'||scenario==='nontext-reply'){
        body.comments[0].text='';
        if(scenario==='nontext-reply')body.comments[0].reply_comment=[{cid:'7600000000000000003',text:'合成图片回复：陪练多少钱',user:{uid:'123456789013'}}];
      }
      if(scenario==='mixed-invalid')body.comments.push({cid:7600000000000000003,text:'不安全数字 ID 的合成记录'});
      if(scenario.startsWith('scroll-'))body.has_more=1;
      if(scenario==='empty-null'||scenario==='ambiguous-null'){
        body.comments=null;body.total=scenario==='empty-null'?0:1;
      }
      if(scenario==='embedded-replies')body.comments[0].reply_comment=[
        {cid:'7600000000000000003',text:'合成二级评论：多少钱',reply_id:'7600000000000000002',user:{uid:'123456789013'}},
        {cid:'7600000000000000004',text:'超出本批总评论上限，不应输出'}
      ];
      this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,body));
      if(scenario==='mixed-body-empty'){
        const empty=response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,{});
        empty.body=async()=>Buffer.alloc(0);this.emit('response',empty);
      }
      if(scenario==='mixed-schema')this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,{status_code:99,comments:[]}));
    }
    return {status:()=>200};
  }
  locator(selector){return {innerText:async()=>this.loading?(scenario==='panel-loading-unrelated'?'推荐视频加载中':scenario.startsWith('panel-loading-')?'全部评论\n留下你的精彩评论吧\n大家都在搜：\n无畏契约\n加载中':'视频数据加载中'):scenario==='device-challenge'?'登录后即可搜索更多精彩视频\n使用原设备扫码\n为保障账号安全，请使用「抖音 APP」扫码验证':scenario==='public-comments-login-to-post'?'全部评论\n请先登录后发表评论\n已加载的合成评论\n登录后即可参与互动讨论':scenario==='success'?'合成夹具正文':'',count:async()=>selector==='[data-e2e="comment-list"]'&&scenario.startsWith('scroll-')?1:0,evaluateAll:async()=>scenario==='search-scope-dom'&&selector.startsWith('a[')?[{href:'https://www.douyin.com/video/7600000000000000009',label:'合成不相关装修作品'},{href:`https://www.douyin.com/video/${vid}`,label:'VALORANT 陪玩'}]:scenario==='resume-search-dom'&&verified&&selector.startsWith('a[')?[{href:`https://www.douyin.com/video/${vid}`,label:'人工继续后的合成视频'}]:[],isVisible:async()=>scenario.startsWith('scroll-'),hover:async()=>{if(scenario==='scroll-late-budget')this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,{status_code:0,comments:[{cid:'7600000000000000005',aweme_id:vid,text:'迟到的合成评论',user:{uid:'123456789015'}}],has_more:1}));if(scenario.startsWith('scroll-')){const e=Error('synthetic hover obstruction');e.name='TimeoutError';throw e;}},first(){return this;},click:async()=>{},all:async()=>[]};}
  getByRole(){return this.locator('button');}
  getByText(text){
    if(scenario.startsWith('note-')&&text instanceof RegExp&&text.test('评论(8)')){
      return {count:async()=>['note-hidden-duplicate','note-ambiguous'].includes(scenario)?2:1,
        nth:index=>({isVisible:async()=>scenario!=='note-hidden-duplicate'||index===1,click:async()=>{
          if(['note-ambiguous','note-wrong-post','note-wrong-origin'].includes(scenario))throw Error('Must not click an ambiguous or unrelated page');
          if(scenario!=='note-no-response')this.emit('response',response(`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}`,
            {status_code:0,has_more:0,comments:[{cid:'7600000000000000002',aweme_id:vid,text:'合成图文评论：找陪练',user:{uid:'123456789012'}}]}));
        }})};
    }
    const missing=scenario==='unavailable-video'||scenario==='unavailable-first'&&this.address.endsWith(vid);
    return {count:async()=>missing&&text==='你要观看的视频不存在'?1:0,isVisible:async()=>missing};
  }
}
module.exports={devices:{'Desktop Chrome':{userAgent:'fixture Desktop Chrome'}},chromium:{launchPersistentContext:async(profile,options)=>{if(options.userAgent!=='fixture Desktop Chrome')throw Error('Desktop client configuration missing');return new Context();}}};
