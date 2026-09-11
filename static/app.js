'use strict';
const $ = (s, root=document) => root.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<i data-lucide="${name}"></i>`;
const pages = {overview:['工作总览','layout-dashboard','OVERVIEW'],monitor:['监控中心','radar','DISCOVERY & MONITORING'],leads:['需求筛选','scan-text','DEMAND INTELLIGENCE'],recruit:['陪玩招募','users-round','TALENT DISCOVERY'],roster:['俱乐部资源','contact-round','CLUB ROSTER'],inbox:['私信导流','messages-square','CONVERSATIONS'],analytics:['导流统计','chart-no-axes-combined','PERFORMANCE'],settings:['数据与设置','sliders-horizontal','DATA & SETTINGS']};
// Legacy personnel pages stay reachable by their old URL; they are not primary workflows.
pages['monitor-settings']=['监控设置','sliders-horizontal','MONITOR SETTINGS'];
pages.live=['直播弹幕','radio','LIVE CHAT'];
const navPages=['overview','monitor','live','leads','inbox','analytics','settings'];
const labels={buyer:'客户需求',seller:'陪玩接单',recruit:'招募需求',social:'免费组队',noise:'无关讨论',uncertain:'待判断'};
const evidenceName=c=>c.evidence_type==='live'?'弹幕':'评论';
const evidenceCount=l=>[l.comment_count?`${l.comment_count} 条评论`:'',l.live_count?`${l.live_count} 条弹幕`:''].filter(Boolean).join(' · ')||'暂无来源记录';
const stages={new:'待联系',reviewed:'已核对',following:'沟通中',referred:'已导流',won:'已成交（历史）',lost:'已结束'};
const jobLabels={draft:'草稿 · 未发送',blocked:'拦截 · 未发送',not_connected:'通道未接 · 未发送',demo_sent:'演示已执行',submitting:'提交中',unknown:'提交结果未知',failed:'失败 · 不自动重发',accepted:'服务端接受 · 未确认送达',delivered:'已送达',replied:'已回复'};
const TARGET_GAME='无畏契约';
const games=[TARGET_GAME];
const serviceTypes=['娱乐开黑','排位组队','新手陪练','对局复盘'];
const reviewFieldLabels={game:'游戏',service_type:'服务方向',region:'区服',rank_label:'段位原文',time:'时间原文',budget:'预算原文',party_size:'人数原文'};
let serviceFilter='';
let monitorDraftDirty=false,semanticDraftDirty=false;
let liveDraftDirty=false,liveSearch='',liveFilter='valuable',livePage=1;
let liveScope='current',liveArchive=null,liveArchiveKey='',liveArchiveSequence=0,liveArchiveError='',liveArchiveAnchor=null,liveArchiveModelKey='';
let monitorResultFilter='valuable',monitorResultQuery='',monitorResultPage=1;
let workFilter='all',workPage=1,workQuery='',monitorResultVideo='';
let monitorHistory=null,monitorHistoryKey='',monitorHistorySequence=0,monitorHistoryError='';
let monitorHistoryErrorKey='';
const monitorHistoryCache=new Map(),monitorHistoryCounts=new Map();
let publishedFilter='all',publishedFrom='',publishedUntil='',leadSort='published';
const publicationWindows={all:'全部发布时间',hour:'近 1 小时',day:'近 24 小时',week:'近 7 天',month:'近 30 天',custom:'指定日期（北京时间）',unknown:'发布时间未知',future:'未来时间 · 待核对'};
let S, mode='live', page=location.hash.slice(1) || 'overview', selected=null, conversation=null, tab='buyer', query='', gameFilter='', draftText='', draftKey='', toastTimer, busy=false;
const draftCache=new Map();
const badge=(text, cls='')=>`<span class="badge ${cls}">${esc(text)}</span>`;
const category=c=>badge(labels[c]||'待判断', c||'uncertain');
const button=(text, action, cls='', attrs='')=>`<button class="button ${cls}" data-action="${action}" ${attrs}>${text}</button>`;
const opts=(obj,value)=>Object.entries(obj).map(([k,v])=>`<option value="${esc(k)}" ${String(value)===k?'selected':''}>${esc(v)}</option>`).join('');
const gameOpts=(value, empty='本方向 / 待识别')=>opts({'':empty,[TARGET_GAME]:'仅无畏契约',other:'其他游戏历史'},value);
const focused=game=>!game||game===TARGET_GAME;
const field=(name,label,value='',type='text',extra='')=>`<label class="field"><span>${label}</span><input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
const select=(name,label,options)=>`<label class="field"><span>${label}</span><select name="${name}">${options}</select></label>`;
const area=(name,label,value='',extra='')=>`<label class="field"><span>${label}</span><textarea name="${name}" ${extra}>${esc(value)}</textarea></label>`;
const fmt=v=>Number(v||0).toLocaleString('zh-CN');
const timeValue=v=>v==null||v===''?null:Number.isFinite(Date.parse(v))?Date.parse(v):null;
const date=v=>timeValue(v)===null?'未提供':new Date(v).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
const commentDate=v=>timeValue(v)===null?'未提供':new Date(v).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
const age=v=>{const stamp=timeValue(v);if(stamp===null)return '发布时间未知';const minutes=Math.floor((Date.now()-stamp)/60000);return minutes<0?'未来时间 · 请核对':minutes<1?'刚刚':minutes<60?`${minutes} 分钟前`:minutes<1440?`${Math.floor(minutes/60)} 小时前`:date(v);};
function dateBound(value){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(value))return null;
  const stamp=Date.parse(value+'T00:00:00+08:00');
  return Number.isFinite(stamp)&&new Date(stamp+8*3600000).toISOString().slice(0,10)===value?stamp:null;
}
function publicationError(){
  if(publishedFilter!=='custom')return '';
  if(!publishedFrom&&!publishedUntil)return '请至少选择开始或结束日期。';
  if((publishedFrom&&dateBound(publishedFrom)===null)||(publishedUntil&&dateBound(publishedUntil)===null))return '日期无效，请重新选择。';
  return publishedFrom&&publishedUntil&&dateBound(publishedFrom)>dateBound(publishedUntil)?'开始日期不能晚于结束日期。':'';
}
function publicationMatches(comment,reference=Date.now()){
  if(publishedFilter==='all')return true;
  const stamp=timeValue(comment?.published_at);
  if(publishedFilter==='unknown')return stamp===null;
  if(stamp===null)return false;
  if(publishedFilter==='future')return stamp>reference;
  if(publishedFilter==='custom')return !publicationError()&&(!publishedFrom||stamp>=dateBound(publishedFrom))&&(!publishedUntil||stamp<dateBound(publishedUntil)+86400000);
  const hours={hour:1,day:24,week:168,month:720}[publishedFilter];
  return !!hours&&stamp<=reference&&stamp>=reference-hours*3600000;
}
function compareLeadTime(a,b,key=leadSort){
  const column=key==='discovered'?'discovered_at':'published_at';
  const at=timeValue(a.latest?.[column]),bt=timeValue(b.latest?.[column]);
  if(at===null&&bt!==null)return 1;if(bt===null&&at!==null)return -1;
  return (bt??0)-(at??0)||b.id-a.id;
}
function publicationSummary(reference=Date.now()){
  const rows=S.comments.filter(c=>focused(c.game));let day=0,week=0,unknown=0,future=0;
  for(const c of rows){const stamp=timeValue(c.published_at);if(stamp===null){unknown++;continue;}const elapsed=reference-stamp;if(elapsed<0){future++;continue;}if(elapsed<=86400000)day++;if(elapsed<=604800000)week++;}
  return `<div class="publication-summary">本方向 / 待识别评论：近 24 小时发布 <b>${day}</b> 条 · 近 7 天 <b>${week}</b> 条 · 时间未知 <b>${unknown}</b> 条${future?` · 未来时间待核对 <b>${future}</b> 条`:''}<small>按原始发布时间计算，不是采集时间；不代表平台全部近期评论。时间显示为北京时间。</small></div>`;
}
function resetLeadFilters(){query='';gameFilter='';serviceFilter='';publishedFilter='all';publishedFrom='';publishedUntil='';leadSort='published';selected=null;}
const avatarInitials=name=>{const text=String(name||'?');const parts=typeof Intl.Segmenter==='function'?[...new Intl.Segmenter('zh',{granularity:'grapheme'}).segment(text)].map(p=>p.segment):Array.from(text);return parts.slice(-2).join('');};
const avatar=(name,small=false)=>`<span class="avatar ${small?'small':''}">${esc(avatarInitials(name))}</span>`;
const sourceLink=c=>(c.source_url||c.video_url)?`<a href="${esc(c.source_url||c.video_url)}" target="_blank" rel="noopener noreferrer">${icon('arrow-up-right')} ${c.evidence_type==='live'?'查看直播间':'查看原视频'}</a>`:'<span class="muted">未提供来源链接</span>';
const empty=(title,text,action='go-monitor',cta='前往视频与采集',ico='inbox')=>`<div class="empty">${icon(ico)}<h3>${title}</h3><p>${text}</p>${cta?button(cta,action,'small'):''}</div>`;
const notice=(text,warn=false)=>`<div class="notice ${warn?'warn':''}">${icon(warn?'circle-alert':'info')}<p>${text}</p></div>`;
const head=(title,desc,actions='')=>`<div class='page-head'><h1>${esc(pages[page][0])}</h1><div class='actions'>${actions}</div></div>`;
const folded=(id,title,content,opened=false)=>`<details id='${id}' class='folded section-gap' ${opened?'open':''}><summary>${title}</summary><div class='folded-body'>${content}</div></details>`;
const openPanelIds=()=>[...(document.querySelectorAll?.('details[id][open]')||[])].map(e=>e.id);
const restorePanelIds=ids=>{for(const id of ids){const panel=document.getElementById?.(id);if(panel?.tagName==='DETAILS')panel.open=true;}};
const metric=(label,note,value,ico,featured=false)=>`<div class="metric ${featured?'featured':''}"><div class="metric-label">${label}<span class="metric-icon">${icon(ico)}</span></div><strong>${fmt(value)}</strong><small>${note}</small></div>`;
function toast(text,error=false){const el=$('#toast');el.textContent=text;el.className='show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.className='',5000);}
function icons(){window.lucide?.createIcons();}
let loadSequence=0,collectionPollTimer,collectionPolling=false,collectionReloadPending=false,collectionPanelPending=false;
async function load(){const requestedMode=mode,sequence=++loadSequence;const r=await fetch(`/api/state?mode=${requestedMode}`);const v=await r.json();if(!r.ok)throw Error(v.error||'读取失败');if(requestedMode!==mode||sequence!==loadSequence)return;S=v;render();queueCollectionPoll();}
async function api(action,body={}){const r=await fetch(`/api/${action}?mode=${mode}`,{method:'POST',headers:{'Content-Type':'application/json','X-ClubOps-Token':S.csrf},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw Error(v.error||'操作失败');return v.result;}
async function save(action,body,message='已保存'){const result=await api(action,body);await load();if(message)toast(message);return result;}
function navigate(next){location.hash=next;if(page===next)render();}
function showModal(title,content,form='',footer=''){const d=$('#modal');d.classList.remove('settings-modal');d.classList.toggle('collection-modal',['collector-form','plan-form'].includes(form));$('#modal-content').innerHTML=`${form?`<form id="${form}">`:''}<div class="modal-head"><h2 id="modal-title">${title}</h2><button type="button" class="icon-button" data-action="close" aria-label="关闭">${icon('x')}</button></div><div class="modal-body">${content}${form?'<p class="form-error notice warn" role="alert" hidden></p>':''}</div>${footer?`<div class="modal-actions">${button('取消','close','', 'type="button"')}${footer}</div>`:''}${form?'</form>':''}`;syncCollectionForms();d.showModal();icons();}
const submit=text=>`<button class="button primary" type="submit">${text}</button>`;
function servicePanel(){return `<div class='service-shortcuts section-gap' aria-label='按服务查看需求'>${serviceTypes.map(s=>`<button class='button' data-action='service-leads' data-service='${s}'>${s}<span>${S.leads.filter(l=>l.game===TARGET_GAME&&l.category==='buyer'&&l.latest.facts?.service_type===s).length}</span></button>`).join('')}</div>`;}
function replyTemplate(l){if(l.category!=='buyer')return '你好，方便说明一下你想沟通的具体内容吗？';const f=l.latest.facts||{};const service=f.service_type||'陪玩';const ask={'娱乐开黑':'想玩什么模式、几个人一起、预计玩多久', '排位组队':'当前段位、预计几人组队，以及想玩的时间', '新手陪练':'主要想练枪、熟悉地图，还是学习对局配合', '对局复盘':'希望复盘的内容，以及是否有可供讲解的对局录像'}[service]||'想要娱乐开黑、排位组队、新手陪练，还是对局复盘';return `你好，关于你提到的${[l.game,f.service_type].filter(Boolean).join(' · ')||'服务'}需求。方便确认${ask}吗？也请补充区服${service==='排位组队'?'':'、方便的时间'}和预算，确认后可为你对接俱乐部进一步咨询。`;}
function closeModal(){monitorDraftDirty=false;liveDraftDirty=false;semanticDraftDirty=false;$('#modal').close();$('#modal-content').innerHTML='';if(S)render();}
function httpMessagingPanel(){
  const t=S.messaging_test;
  if(!t)return '';
  const names={not_configured:'等待授权配置',configured_unverified:'配置已齐 · 平台未验证',sending:'正在提交 · 请勿重发',api_accepted:'接口已受理 · 收件待确认',rejected:'平台拒绝 · 已停止',unknown:'结果不确定 · 请核对收件方',storage_error:'账本异常 · 已停止'};
  const details=t.attempt?esc(t.attempt.detail):t.issues.length?esc(t.issues.join('；')):'本地参数检查通过，不代表平台已授权。此适配器仅支持已有私信或进私事件。';
  return `<section class="card section-gap dm-test-panel" aria-label="HTTP 单条私信测试"><div class="card-head"><h2>HTTP 单条测试</h2>${badge(names[t.status]||'状态待核对',t.status==='api_accepted'?'good':'warn')}</div><div class="card-body"><p>发送方 <b>${esc(t.sender)}</b> → 接收方 <b>${esc(t.recipient)}</b></p><p class="dm-test-detail">${details}</p><details><summary>查看测试消息</summary><p class="dm-test-detail">${esc(t.text)}</p></details><div class="actions dm-test-actions">${button('检查发送条件','dm-test-check')}${button('发送 1 条测试消息','dm-test-send','primary',t.can_send?'':'disabled')}</div><small>不启动浏览器，不读取评论名单；一次发送尝试，失败或结果不确定也不会自动重试。测试不计入客户获客统计。</small></div></section>`;
}
function setMobileNavigation(open,restoreFocus=true){
  if(open)document.body.classList.add('menu-open');else document.body.classList.remove('menu-open');
  $('.mobile-menu').setAttribute('aria-expanded',String(open));
  const shell=$('.shell');if(shell)shell.inert=open;
  if(open)$('.mobile-nav-close').focus();else if(restoreFocus)$('.mobile-menu').focus();
}
document.addEventListener('keydown',event=>{
  if(event.key!=='Tab'||!document.body.classList.contains('menu-open'))return;
  const controls=[...document.querySelectorAll('#sidebar a,#sidebar button')].filter(el=>el.getClientRects().length);
  const first=controls[0],last=controls[controls.length-1];
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
});
window.matchMedia?.('(max-width:700px)').addEventListener('change',event=>{if(!event.matches)setMobileNavigation(false,false);});

function render(){
  if(!S)return;const openMonitorSettings=page==='monitor-settings';if(openMonitorSettings){page='monitor';history.replaceState(null,'','#monitor');}const opened=openPanelIds();if(!pages[page])page='overview';
  $('#nav').innerHTML=navPages.map(k=>[k,pages[k]]).map(([k,[name,ico]])=>`<a href="#${k}" class="${(page===k||k==='monitor'&&page==='monitor-settings')?'active':''}" ${(page===k||k==='monitor'&&page==='monitor-settings')?'aria-current="page"':''}>${icon(ico)}${name}</a>`).join('')+(mode==='live'?`<a href="/login">${icon('key-round')}账号登录</a>`:'');
  $('#demo-banner').hidden=mode!=='demo';document.title=`${pages[page][0]} · ClubOps`;
  $('#main').innerHTML=({overview,monitor,'monitor-settings':monitorSettingsPage,live:liveMonitor,leads,recruit,roster,inbox,analytics,settings}[page])();
  const login=S.collector?.login_recovery,loginAttention=login?.active||(login?.available&&!login.session?.ready);
  if(mode==='live'&&(page==='settings'||(page==='monitor'&&loginAttention)))$('.page-head').insertAdjacentHTML('afterend',loginSummary());
  if(mode==='live'&&page==='monitor')$('.page-head .actions').insertAdjacentHTML('beforeend',`<a class="button small" href="/login">${icon('key-round')}账号登录</a>`);
  restorePanelIds(opened);syncCollectionForms();icons();if(page==='monitor')void refreshMonitorHistory();if(page==='live')void refreshLiveArchive();if(openMonitorSettings)monitorSettingsDialog();
}

function loginSummary(){const value=S.collector?.login_recovery;const title=value?.active?'账号正在恢复登录':value?.session?.ready?'账号会话已保存':'账号登录与手机连接';const hint=value?.active?'查看当前步骤，处理登录页提示。':value?.relay?.ready?'中转已绑定；手机测试与自动恢复在账号页设置。':'手机验证码自动转发尚未连接。';return `<section class="card login-summary" aria-label="账号连接状态"><div><strong>${title}</strong><small>${hint}</small></div><a class="button small" href="/login">管理账号登录</a></section>`;}

function overview(){const s=S.stats;const leads=S.leads.filter(l=>l.category==='buyer'&&l.game===TARGET_GAME).sort((a,b)=>compareLeadTime(a,b,'published')).slice(0,5);return head('','',button(icon('scan-text')+' 运行初筛','analyze','primary'))+`<div class='metrics'>${metric('已入库评论','',s.comments,'message-square-text')}${metric('潜在需求用户','需人工核对',s.buyers,'scan-text',true)}${metric('待初筛评论','',s.pending,'list-filter')}${metric('待发送草稿','尚未发送',s.drafts,'file-pen-line')}</div>${servicePanel()}<div class="card"><div class="card-head"><div><h2>最近客户需求</h2><small>先看原文，再决定是否跟进</small></div><a href="#leads" class="button subtle small">全部线索 ${icon('arrow-right')}</a></div>${leads.length?leadTable(leads,true):empty('从第一条真实评论开始','在视频与采集页开启监控，读到的评论会自动入库并初筛。')}</div>`+folded('overview-records','工作记录',eventList(5));}
function connectionRows(){const c=collectionState(),h=c.http||{};return [['radar','自建抖音采集','按任务选择 HTTP 或本机浏览器 · 有限批次',c.active?'任务进行中':c.last_received?'已有采集数据':'待实测',c.last_received?'good':'warn'],['network','纯 HTTP 采集','指定视频、搜索与回复分别记录结果；正常读取不打开浏览器',h.session?.status==='identity_failed'?'身份核对失败，需准备新会话':h.live_verified?'本工作区已读到评论':h.session?.ready?'会话已准备':h.installed?'待准备会话':'待安装依赖',h.live_verified?'good':'warn'],['sparkles','需求识别',S.semantic?.can_analyze?(S.semantic.config?.auto_analyze?'规则初筛后自动调用所选模型':'规则自动初筛 · 可单条调用所选模型'):'关键词规则初筛 · 非语义模型',S.semantic?.can_analyze?(S.semantic.config?.backend==='openai_compatible'?'远程 API 已配置':'本机模型已配置'):'规则模式',''],['send','个人号 HTTP 私信','独立消息通道；状态以发送记录为准',S.uid_messaging?.can_attempt?'配置就绪':'待配置','warn']].map(x=>`<div class="source-row"><div class="row"><span class="source-icon">${icon(x[0])}</span><div><h3>${x[1]}</h3><small>${x[2]}</small></div></div>${badge(x[3],x[4])}</div>`).join('');}
function eventList(n=10){return S.events.length?`<ul class="events">${S.events.slice(0,n).map(e=>`<li>${esc(e.detail)}<small>${date(e.created_at)}</small></li>`).join('')}</ul>`:'<p class="muted">暂时没有操作记录。</p>';}

function monitor(){return head('监控中心','左侧选择作品，右侧查看采集评论。',button(icon('sliders-horizontal')+' 监控设置','monitor-settings','small')+button(icon('play')+' 单次采集','collector-new','small',mode==='demo'?'disabled':''))+monitorOverview()+discoveryOverview()+`<div class="monitor-workspace">${workPool()}<div class="monitor-comment-column">${monitorResultsPanel()}</div></div>`+folded('monitor-run-details','运行日志与历史',collectorPanel(),false);}
function monitorSettingsPage(){return monitor();}
function monitorSettingsDialog(){showSettingsModal('评论监控设置',monitorSettingsPanel()+schedulePanel());}


function leadTable(items,compact=false){return `<div class="table-scroll"><table class="lead-table"><thead><tr><th>用户 / 需求原文</th><th>游戏</th>${compact?'': '<th>分类 / 跟进</th>'}<th>发布 / 首次入库</th></tr></thead><tbody>${items.map(l=>`<tr data-action="select-lead" data-id="${l.id}" tabindex="0" aria-label="查看 ${esc(l.nickname)} 的需求" class="${selected===l.id?'selected':''}"><td><div class="row">${avatar(l.nickname,true)}<div><strong>${esc(l.nickname)}</strong><span class="cell-sub">${evidenceCount(l)}${l.latest.analysis_method==='pending'?' · 尚未初筛':''}</span></div></div><span class="cell-title" style="margin-top:10px;font-size:.875rem">${esc(l.latest.raw_text||'暂无来源原文')}</span></td><td class="nowrap">${esc(l.game||'待识别')}</td>${compact?'':`<td>${category(l.category)}<span class="cell-sub">${stages[l.stage]}</span></td>`}<td class="comment-times"><span title="${esc(l.latest.published_at||'未提供')}">发布 ${age(l.latest.published_at)}</span><small>首次入库 ${date(l.latest.discovered_at)}</small></td></tr>`).join('')}</tbody></table></div>`;}
function filteredLeads(cats){const reference=Date.now();return S.leads.filter(l=>(!cats||cats.includes(l.category))&&(gameFilter==='other'?!!l.game&&l.game!==TARGET_GAME:gameFilter?l.game===gameFilter:focused(l.game))&&(!serviceFilter||l.latest.facts?.service_type===serviceFilter)&&publicationMatches(l.latest,reference)&&(!query||`${l.nickname} ${l.latest.raw_text||''} ${l.external_id}`.toLowerCase().includes(query.toLowerCase()))).sort(compareLeadTime);}
function filterBar(){return `<div class="lead-filters"><label class="search">${icon('search')}<input id="lead-search" aria-label="搜索昵称、ID 或评论" placeholder="搜索昵称、ID、评论" value="${esc(query)}"></label><select id="game-filter" class="control" aria-label="按游戏筛选">${gameOpts(gameFilter)}</select><select id="service-filter" class="control" aria-label="按服务类型筛选">${opts({'':'全部服务',...Object.fromEntries(serviceTypes.map(s=>[s,s]))},serviceFilter)}</select><select id="published-filter" class="control" aria-label="按发布时间筛选">${opts(publicationWindows,publishedFilter)}</select><select id="lead-sort" class="control" aria-label="线索排序">${opts({published:'发布时间新到旧（未知置后）',discovered:'首次入库时间新到旧'},leadSort)}</select>${button('清除筛选','clear-lead-filters','small')}${publishedFilter==='custom'?`<div class="publication-dates"><label>开始日期（含）<input id="published-from" type="date" value="${esc(publishedFrom)}" aria-label="开始日期（北京时间）"></label><label>结束日期（含）<input id="published-until" type="date" value="${esc(publishedUntil)}" aria-label="结束日期（北京时间）"></label>${button('应用日期','apply-published-range','small')}<small>选择日期后点击“应用日期”，更新结果。</small></div>`:''}<p class="filter-scope">筛选每位用户当前展示评论的发布时间，不按入库时间判断新需求；其余评论仍保留在历史中。</p>${publicationError()?`<p class="filter-error" role="alert">${esc(publicationError())}</p>`:''}</div>`;}
function leads(){const cats=tab==='all'?null:[tab];const items=filteredLeads(cats);let l=items.find(x=>x.id===selected)||items[0];if(l)selected=l.id;const withoutUser=S.comments.filter(c=>!c.person_id).length;return head('识别无畏契约的真实付费需求。','付费陪玩、陪练、接单与免费组队分别判断。普通“找搭子”保留为待判断，不默认视为客户。',button(icon('scan-text')+(S.stats.pending?` 初筛待处理 (${S.stats.pending})`:'规则初筛已完成'),'analyze',S.stats.pending?'primary':'small',S.stats.pending?'':'disabled'))+(withoutUser?notice(`另有 ${withoutUser} 条评论缺少用户标识，保留原文但不会仅按昵称合并成客户。`)+button('查看未关联评论','unlinked-comments','small'):'')+publicationSummary()+`<div class="split"><div class="card"><div class="toolbar"><div class="tabs">${[['buyer','客户需求'],['uncertain','待判断'],['all','全部']].map(([v,t])=>`<button class="tab ${tab===v?'active':''}" data-action="lead-tab" data-tab="${v}">${t}</button>`).join('')}</div>${filterBar()}</div>${items.length?leadTable(items):empty('当前筛选下没有线索',S.comments.length?'当前发布时间、分类或搜索条件下没有已入库记录。可清除筛选查看历史；不代表平台没有相关需求。':'开启评论监控后，系统自动记录原文并筛选需求；有明确用户标识才建立用户线索。')}<div class="card-foot">${items.length} 位用户 · 分类取自每位用户当前展示的来源记录，评论与弹幕历史可展开核对。</div></div>${l?leadDetail(l):`<div class="card">${empty('需求详情','选择一条线索，查看原文、识别依据与沟通记录。','','','scan-text')}</div>`}</div>`;}
function matching(l){const f=l.latest.facts||{};return S.members.filter(m=>m.available&&l.game&&m.game===l.game&&(!f.region||!m.region||f.region===m.region)&&(!f.service_type||!m.service_types?.length||m.service_types.includes(f.service_type)));}
function parentEvidence(c){
  const p=c.parent_context||{status:c.parent_external_id?'missing':'none',external_id:c.parent_external_id};
  if(p.status==='none')return '';
  const available=p.status==='available';
  return `<section class="parent-evidence" aria-label="回复上下文"><strong>${available?'已读到上级评论原文':p.status==='conflict'?'回复关系冲突 · 未用于初筛':'未读到上级评论原文'}</strong><small>上级评论 ID：${esc(p.external_id||'未知')}</small>${available?`<blockquote>${esc(p.raw_text)}</blockquote><small>作者：${esc(p.nickname||'未提供昵称')} · 发布 ${date(p.published_at)}</small><small>来源：${esc(p.source_name||'未提供')} · 入库 ${date(p.discovered_at)}</small><p>上下文辅助辨认游戏和询价对象，不把上级的预算、区服、服务方向或联系意愿转给回复者。</p>`:'<p>仅保留回复 ID，不补写缺失内容；请结合原始页面核对。</p>'}</section>`;
}
function humanEvidence(c){
  const entries=Object.entries(c.manual_fields||{}),history=c.review_history||[];
  const fields=values=>Object.entries(values||{}).map(([key,value])=>`<span>${esc(reviewFieldLabels[key]||key)}：${esc(value||'人工标记为未知')}</span>`).join('');
  return (entries.length?`<section class="manual-evidence"><strong>当前人工核对字段</strong><div class="manual-values">${fields(c.manual_fields)}</div><small>筛选与模板使用这些人工值；其余字段仍来自规则，排期和报价另行确认。</small></section>`:'')+(history.length?`<details class="rule-evidence"><summary>人工核对记录 · 最近 ${history.length} 次</summary>${history.map(r=>`<article class="history-item"><div class="row spread">${category(r.category)}<small>${date(r.created_at)}</small></div><p>${esc(r.reason)}</p>${r.raw_text!==c.raw_text?`<small>此前核对的原文（现已修订）</small><blockquote class="quote">${esc(r.raw_text)}</blockquote>`:''}<small>保存时的人工字段</small><div class="manual-values">${fields(r.manual_fields)||'未设置字段覆盖；分类判断另行保留'}</div></article>`).join('')}</details>`:'');
}
function semanticPanel(){
  const m=S.semantic||{},c={backend:'ollama',enabled:false,host:'127.0.0.1',port:11434,model:'',timeout_seconds:30,max_concurrency:1,live_model_enabled:true,...m.config};
  return `<section class="card"><div class="card-head"><h2>语义模型</h2>${badge(m.can_analyze?'配置已启用 · 未验收':'规则模式')}</div><form id="semantic-form" class="card-body"><p>${esc(m.detail||'未启用语义模型，继续使用规则与人工核对。')}</p>${select('enabled','运行方式',opts({false:'仅规则',true:'规则与语义模型'},String(c.enabled)))}${select('auto_analyze','新采集内容的模型分析',opts({false:'手动逐条分析',true:'规则初筛后自动分析'},String(!!c.auto_analyze)))}${select('live_model_enabled','直播弹幕使用模型',opts({false:'关闭，仅规则初筛',true:'开启，沿用上方分析方式'},String(c.live_model_enabled)))}${select('backend','分析通道',opts({openai_compatible:'远程 API',ollama:'本机 Ollama'},c.backend))}${field('api_base_url','API 地址（远程通道）',c.api_base_url||'','url','maxlength="300" placeholder="https://api.example.com/v1"')}${field('api_key','API 密钥（留空保留已存密钥）','','password',`maxlength="4096" autocomplete="new-password" placeholder="${m.api_key_configured?'已加密保存，不回显':'远程通道启用前填写'}"`)}${field('model','模型名称',c.model,'text','maxlength="120" placeholder="填写 API 可用模型或本机模型名称"')}${select('host','Ollama 地址（本机通道）',opts({'127.0.0.1':'127.0.0.1','::1':'::1'},c.host))}<div class="fields-2">${field('port','Ollama 端口（本机通道）',c.port,'number','min="1" max="65535" required')}${field('timeout_seconds','单条时限（秒）',c.timeout_seconds,'number','min="5" max="60" required')}</div>${select('max_concurrency','远程 API 同时分析数',opts({1:'1 条',2:'2 条',3:'3 条',4:'4 条'},String(c.max_concurrency)))}<p class="muted">仅远程 API 使用此设置；本机 Ollama 同时分析 1 条。</p><p class="muted">保存不启动分析或下载模型。自动模式仅处理之后完成初筛的新内容，队列满时保留规则；仅修改直播开关不影响评论队列，其他模型配置变更会停止旧队列。关闭直播模型后，新弹幕仅做规则初筛，单条模型入口也关闭，历史结果保留。远程 API 接收已启用来源的原文及必要上下文；密钥仅加密保存在本机，不回显。本机 Ollama 需关闭云端功能。失败保留规则结果；准确率尚未独立验证。</p>${(m.issues||[]).map(x=>`<p class="notice warn">${esc(x)}</p>`).join('')}<small id="semantic-draft-status">${semanticDraftDirty?'有未保存的修改':'配置已保存'}</small><div class="actions">${submit('保存模型配置')}</div><p class="form-error notice warn" role="alert" hidden></p></form></section>${modelQueuePanel()}`;
}
function modelQueuePanel(){
  const q=S.collector?.model_queue||S.semantic?.queue||{counts:{},rows:[],active:0,capacity:200},c=q.counts;
  const names={queued:'等待分析',running:'正在分析',cancelling:'正在取消',completed:'已完成',failed:'失败 · 保留规则',stale:'版本已变化',skipped:'无需自动分析',cancelled:'已取消',interrupted:'服务中断'};
  return `<section id="model-queue-panel" class="card"><div class="card-head"><h2>自动模型分析</h2>${badge(`${q.active} 条待处理`,q.active?'warn':'')}</div><div class="card-body"><p>等待 ${c.queued||0} · 分析中 ${c.running||0} · 完成 ${c.completed||0} · 失败 ${c.failed||0}</p><p class="muted">同时最多分析 ${q.concurrency_limit||S.semantic?.concurrency_limit||1} 条，等待与分析中合计最多 ${q.capacity} 条。失败保留规则结果；已核对的人工结果优先。</p>${button('停止当前队列','semantic-queue-cancel','small',`type="button" ${q.active?'':'disabled'}`)}<p class="muted">停止只取消当前队列；后续新内容是否入队由模型配置决定。</p>${q.rows.length?`<details><summary>最近任务</summary>${q.rows.map(r=>`<div class="history-item"><strong>${r.evidence_type==='live'?'弹幕':'评论'} #${r.record_id} · ${esc(names[r.status]||r.status)}</strong><small>${esc(r.detail||'等待模型处理')}</small></div>`).join('')}</details>`:'<p class="muted">尚无自动模型任务。</p>'}</div></section>`;
}
function modelEvidence(c,allowAction=true){
  const m=c.model_result,r=m?.result||{},can=mode==='live'&&S.semantic?.can_analyze&&(c.evidence_type!=='live'||S.semantic.config?.live_model_enabled!==false)&&c.analysis_input_hash&&c.analysis_method!=='pending';
  const action=allowAction&&can?button('分析此条原文','semantic-analyze','small',`type="button" data-kind="${esc(c.evidence_type||'comment')}" data-id="${c.id}" data-hash="${esc(c.analysis_input_hash)}" ${(m?.status==='running'||(S.semantic?.at_capacity??S.semantic?.running))?'disabled':''}`):'';
  if(!m)return action?`<div class="actions">${action}</div>`:'';
  const names={running:'模型分析中',completed:'已保存模型结果',failed:'模型失败 · 回退规则',stale:'历史版本 · 不采用',interrupted:'模型中断 · 回退规则',cancelled:'模型已取消 · 保留规则'};
  return `<details class="rule-evidence"><summary>${esc(names[m.status]||'模型状态待核对')}${c.analysis_method==='human'?' · 人工判断优先':''}</summary><small>${esc(m.engine)} · ${date(m.finished_at||m.started_at)}</small><p>${esc(m.detail)}</p>${m.status==='completed'?`<p>模型分类：${esc(labels[r.category]||'待判断')} · ${r.certainty==='clear'?'模型自报明确':'确定性不足'}；尚未测定准确率。</p><p><strong>模型理由：</strong>${esc(r.reason||"这条历史结果未保存判断理由")}</p><ul class="rule-hits">${(r.facts?.evidence||[]).map(e=>`<li><small>${esc(e.kind)} · ${esc({comment:'当前原文',parent:'上级原文',video:'视频标题'}[e.source]||e.source)}</small><q>${esc(e.text)}</q></li>`).join('')}</ul>`:''}${action}</details>`;
}
function ruleEvidence(c,allowModel=true){
  const human=humanEvidence(c)+modelEvidence(c,allowModel)+(c.analysis_method==='human'&&c.rule_category?`<p class="rule-note">保留的规则分类：${esc(labels[c.rule_category]||c.rule_category)}。${esc(c.rule_reason||'')}</p>`:'');
  if(c.analysis_method==='pending')return human;
  const f=c.rule_facts||c.facts||{},items=Array.isArray(f.evidence)?f.evidence.slice(0,40):[],warnings=Array.isArray(f.warnings)?f.warnings:[];
  if(!f.rules_version&&!items.length)return human+(c.analysis_method==='rules'?'<p class="rule-note">此条旧规则结果没有保存命中片段；不能据此核验具体词句。</p>':'');
  const kinds={game:'游戏',request:'服务需求',pricing:'询价',supply:'接单 / 求职',availability_question:'询问接单',recruit:'招募',group:'组队',free:'免费表达',payment:'付费表达',budget:'预算',party_size:'人数',region:'区服',time:'时间',rank_label:'段位',service_type:'服务方向',service_context:'询价语境',product_context:'物品 / 设备语境',reported:'转述 / 举例',hypothetical:'假设 / 将来考虑',live_paid_help:'付费求带',live_help_price:'求带询价',live_group:'求带 / 缺人',live_offer:'提供服务',live_third_party:'他人需求',live_denial:'否定 / 劝阻',live_ambiguity:'玩笑 / 词义讨论'};
  const currentRules=c.evidence_type==='live'?S.collector?.live_monitor?.ruleset_version:S.settings?.ruleset_version;
  const sources={comment:c.evidence_type==='live'?'此条弹幕':'此条评论',parent:'上级原文',video:'视频标题'};
  return human+`<details class="rule-evidence"><summary>${c.analysis_method==='human'?'查看先前规则依据':'查看初筛依据'}${warnings.length?' · 有待核对项':''}</summary><p class="rule-note">${c.analysis_method==='human'?'以下保留先前规则命中，不代表人工已确认这些字段；已纠正字段单独列在上方。':'规则命中仅用于初筛，不是模型概率或联系授权。'}</p><small>规则版本：${esc(f.rules_version||'未记录')}</small>${f.rules_version&&currentRules&&f.rules_version!==currentRules?`<p class="rule-note">当前规则为 ${esc(currentRules)}；此条保留历史判断，升级不会自动改写。可通过人工核对确认当前结论。</p>`:''}${warnings.length?`<ul class="rule-warnings">${warnings.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}${items.length?`<ul class="rule-hits">${items.map(e=>`<li><div><span>${esc(kinds[e.kind]||e.kind||'未标注')}</span><small>${esc(sources[e.source]||'来源未标注')}</small></div><q>${esc(e.text)}</q>${e.negated?'<strong class="negated-hit">含否定 · 不作正向命中</strong>':''}${e.attribution==='unconfirmed'?'<strong class="negated-hit">归属待核对 · 未提取为此用户字段</strong>':''}</li>`).join('')}</ul>`:'<p class="rule-note">未命中明确词句，保留待判断。</p>'}</details>`;
}
function revealLeadDetail(){
  const detail=$('#lead-detail');
  if(!detail)return;
  detail.focus({preventScroll:true});
  if(window.innerWidth<1100)detail.scrollIntoView({block:'start',behavior:'auto'});
}
function leadDetail(l){
  const c=l.latest,f=c.facts||{};
  return `<aside id="lead-detail" class="card detail" tabindex="-1" aria-label="已选需求详情"><div class="card-body">
    <div class="row spread"><div class="row">${avatar(l.nickname)}<div><h2>${esc(l.nickname)}</h2><div class="subline">ID / ${esc(l.external_id)}</div></div></div>${category(l.category)}</div>
    <blockquote class="quote">${esc(c.raw_text||'没有原文')}</blockquote>
    <div class="row spread" style="font-size:.75rem">${sourceLink(c)}<span class="muted">${date(c.published_at)}</span></div>
    <div class="subline">${evidenceName(c)} ID ${esc(c.external_id||'未知')} · 来源：${esc(c.source_name||'未提供')} · 入库 ${date(c.discovered_at)}</div>${parentEvidence(c)}
    <div class="facts">${[['游戏',l.game],['服务方向',f.service_type],['段位原文',f.rank_label],['时间表达',f.time],['人数原文',f.party_size],['预算数值 / 范围',f.budget],['区服',f.region],['识别方式',{pending:'尚未处理',rules:'规则初筛',human:'人工确认',model:'语义模型 · 未测准确率'}[c.analysis_method]]].map(x=>`<div class="fact"><small>${x[0]}</small><b>${esc(x[1]||'未明确')}</b></div>`).join('')}</div>
    <p class="muted" style="font-size:.875rem">${esc(c.reason||'等待初筛；不从昵称推断职业、身份或消费能力。')}</p>
    ${ruleEvidence(c)}
    <div class="actions" style="margin-top:14px">${c.id?button('核对分类',c.evidence_type==='live'?'live-review':'review','small',`data-id="${c.id}"`):''}${button(`查看 ${l.evidence_count??l.comment_count} 条历史`,'history','small',`data-id="${l.id}"`)}</div><hr class="divider">
    <div class="row spread"><span class="muted" style="font-size:.875rem">${stages[l.stage]} · ${esc(l.owner||'未分配负责人')}</span>${button('跟进记录','follow','small',`data-id="${l.id}"`)}</div>${button(icon('messages-square')+' 进入私信工作台','open-chat','primary',`data-id="${l.id}" style="width:100%;margin-top:15px"`)}<small style="display:block;text-align:center;margin-top:9px">进入工作台不等于已发送消息</small>
  </div></aside>`;
}
function recruit(){const items=filteredLeads(['seller','recruit']);return head('发现无畏契约陪玩人才。','从评论中分离陪玩接单与俱乐部招募需求，避免把同行误当成付费客户。',button('添加俱乐部人员','add-member','primary'))+notice('此处仅为候选线索，不会自动把评论者纳入你的俱乐部。合作意向、技能和排期需要另行确认。')+publicationSummary()+`<div class="card"><div class="toolbar"><h2>人才与招募线索</h2>${filterBar()}</div>${items.length?leadTable(items):empty('暂无接单或招募线索','采集结果完成初筛后，这两类需求会在这里单独展示。')}<div class="card-foot">陪玩接单 ${S.stats.category_counts.seller||0} 条评论 · 招募需求 ${S.stats.category_counts.recruit||0} 条评论</div></div>`;}
function roster(){const hasOtherGames=S.members.some(m=>m.game!==TARGET_GAME);const items=S.members.filter(m=>gameFilter==='other'?m.game!==TARGET_GAME:m.game===TARGET_GAME);return head('无畏契约人员与服务能力。','记录服务类型、区服、段位说明与可用时间。价格由俱乐部填写，不预设实际报价。',button(icon('plus')+' 添加人员','add-member','primary'))+`<div class="toolbar section-gap" style="padding:0 0 15px"><div class="tabs">${hasOtherGames?[['','无畏契约人员'],...(S.members.some(m=>m.game!==TARGET_GAME)?[['other','其他游戏历史']]:[])].map(([g,t])=>`<button class="tab ${gameFilter===g?'active':''}" data-action="roster-game" data-game="${esc(g)}">${t}</button>`).join(''):''}</div><small>${items.length} 位人员 · ${items.filter(m=>m.available).length} 位可接单</small></div>${items.length?`<div class="member-grid">${items.map(m=>`<article class="card member-card"><div class="row spread"><div class="row">${avatar(m.name)}<div><h2>${esc(m.name)}</h2><small>${esc(m.game)}</small></div></div>${badge(m.available?'可接单':'暂不可接',m.available?'good':'')}</div><div class="member-info"><div><small>服务类型</small><p>${esc(m.service_types?.join(' / ')||'待确认')}</p></div><div><small>段位 / 可接范围</small><p>${esc(m.rank_label||'待确认')}</p></div><div><small>服务说明</small><p>${esc(m.skill||'未填写')}</p></div><div><small>区服</small><p>${esc(m.region||'未填写')}</p></div><div><small>参考报价</small><div class="price">${m.price==null?'—':`¥${m.price}`}<small> / 小时</small></div></div><div><small>可用时段</small><p>${esc(m.availability||'待确认')}</p></div></div><div class="actions">${button(icon('pencil')+' 编辑','edit-member','small',`data-id="${m.id}"`)}${button(m.available?'设为不可接':'设为可接单','toggle-member','subtle small',`data-id="${m.id}"`)}</div></article>`).join('')}</div>`:`<div class="card">${empty('暂无人员','点击右上角“添加人员”建立人员库。','','','users-round')}</div>`}`;}

function uidHttpPanel(inDialog=false){
  if(mode!=='live')return '';
  const u=S.uid_messaging||{status:'not_configured',can_attempt:false,issues:['等待服务器加载通道状态']};
  const title=`私信通道 · ${u.can_attempt?'配置就绪，发送前仍需核对':'待配置'} <span class="channel-summary-note">查看配置与检查</span>`;
  const content=`<section class="card" aria-label="个人号 HTTP UID 通道"><div class="card-body"><h2>个人号 HTTP · 数字 UID</h2><p>${esc(u.detail||'尚未完成真实发送验证')}</p><p>发送方数字 UID：${esc(u.sender_uid||'未配置')} · 测试接收范围：${u.allowed_recipients||0} 个账号</p>${(u.issues||[]).map(x=>`<p class="muted">${esc(x)}</p>`).join('')}<div class="actions">${button('检查本地配置','uid-http-check')}${button('核对登录身份（不发送）','uid-http-probe','',u.can_attempt?'':'disabled')}${button('登记授权测试对象','uid-http-target-new')}</div><small>登记与检查不发送消息。发送使用已保存草稿；结果未知时保留记录，不自动重发。</small></div></section>`;
  return inDialog?content:`<div class="channel-settings-entry">${button('私信通道设置','uid-settings','small')}</div>`;
}
function uidSettingsDialog(){showSettingsModal('私信通道设置',uidHttpPanel(true));}

function uidTargetDialog(){
  showModal('登记授权测试对象',field('uid','接收方数字 UID','','text','inputmode="numeric" pattern="[1-9][0-9]{0,18}" maxlength="19" required')+field('nickname','备注昵称')+area('contact_note','同意接收测试消息的依据','','required maxlength="1000"')+notice('该记录仅用于已同意的测试对象，不生成评论或付费需求。发送前还需核对登录账号、会话身份与本地测试范围。'),'uid-target-form',submit('登记对象'));
}
function inbox(){
  const list=S.leads.filter(l=>l.category==='buyer'||l.source_kind==='uid_test'||l.id===conversation||S.jobs.some(j=>j.lead_id===l.id)||S.messages.some(m=>m.lead_id===l.id));
  const l=list.find(x=>x.id===conversation)||list[0];
  const top=head('私信沟通与导流','草稿、提交结果与送达证据分别记录。','')+uidHttpPanel();
  if(!l)return top+`<div class="card">${empty('暂无可跟进的会话','在线索页核对真实需求，或登记已同意的测试对象。','go-leads','前往需求线索','messages-square')}</div>`;
  conversation=l.id;
  const msgs=S.messages.filter(m=>m.lead_id===l.id),jobs=S.jobs.filter(j=>j.lead_id===l.id);
  const channel=mode==='demo'?'模拟通道':S.uid_messaging?.can_attempt?'HTTP 配置就绪 · 未验收':'HTTP 待配置';
  return top+`<div class="inbox"><div class="inbox-list"><h3>线索与会话 <span class="muted">${list.length}</span></h3>${list.map(x=>`<button class="conversation ${x.id===l.id?'active':''}" data-action="chat-select" data-id="${x.id}">${avatar(x.nickname,true)}<span class="conversation-content"><b>${esc(x.nickname)}</b><p>${esc(x.source_kind==='uid_test'?'授权测试对象':x.game||'游戏未识别')} · ${stages[x.stage]}</p></span></button>`).join('')}</div>
    <section class="chat"><div class="chat-header"><div><h2>${esc(l.nickname)}</h2><small>${esc(l.external_id)}</small></div>${badge(channel,'warn')}</div>
    <div class="chat-history">${msgs.length?msgs.map(m=>`<div class="bubble-wrap ${m.direction==='outbound'?'out':''}"><div class="bubble">${esc(m.content)}</div><small>${esc(m.status==='demo'?'演示消息':jobLabels[m.status]||m.status)} · ${date(m.created_at)}</small></div>`).join(''):empty('还没有私信记录','保存草稿不代表发送；服务端接受也不代表接收端送达。','','','message-square')}</div>
    <form id="composer" class="composer"><div class="row spread">${button(icon('wand-sparkles')+' 填入回复模板','suggest','subtle small','type="button"')}<span>模板辅助 · 非 AI 自动回复</span></div><textarea id="draft-content" name="content" placeholder="填写内容并保存为草稿…" maxlength="2000" required aria-label="私信草稿">${esc(draftText)}</textarea><div class="row spread"><span>保存不会自动发送</span><button class="button primary" type="submit">${icon('save')} 保存草稿</button></div></form></section>
    <aside class="inbox-detail"><div><h3>联系与跟进</h3>${badge(l.do_not_contact?'禁止联系':l.contact_basis?'已登记联系依据':'未登记联系依据',l.do_not_contact?'bad':l.contact_basis?'good':'warn')}<p class="muted">${esc(l.contact_note||'尚未记录联系依据')}</p>${button('编辑联系依据','contact','small',`data-id="${l.id}"`)}<hr class="divider">${button('更新跟进结果','follow','small',`data-id="${l.id}"`)}<hr class="divider"><h3>原始需求</h3><p>${esc(l.latest.raw_text||'无来源原文；不得据此认定付费需求')}</p>${sourceLink(l.latest)}</div>
    <div><h3>草稿与发送任务</h3>${jobs.length?jobs.slice(0,8).map(j=>`<div class="job"><div class="row spread">${badge(jobLabels[j.status]||j.status,'warn')}<small>#${j.id}</small></div><p>${esc(j.content)}</p><small>${esc(j.detail||'草稿尚未提交')} · ${date(j.created_at)}</small>${j.uid_http?`<details><summary>查看 HTTP 结果依据</summary><p>阶段：${esc(j.uid_http.phase)}</p><p>消息编号：${esc(j.uid_http.evidence.server_message_id||'尚未确认')}</p></details>`:''}${['draft','blocked','not_connected'].includes(j.status)?(mode==='demo'?button('执行模拟发送','send','small',`data-id="${j.id}"`):button('HTTP 发送这条草稿','uid-http-send','small',`data-id="${j.id}" ${!S.uid_messaging?.can_attempt||!l.contact_basis||l.do_not_contact?'disabled':''}`)):''}</div>`).join(''):'<p class="muted">先保存草稿，再提交。</p>'}</div></aside></div>`;
}

function analytics(){const s=S.stats;const reviewed=S.comments.filter(c=>c.analysis_method==='human').length;const urls=S.comments.filter(c=>c.video_url).length;const lag=S.comments.filter(c=>c.published_at).map(c=>(new Date(c.discovered_at)-new Date(c.published_at))/60000).filter(n=>n>=0).sort((a,b)=>a-b);const median=lag.length?(lag[Math.floor((lag.length-1)/2)]+lag[Math.ceil((lag.length-1)/2)])/2:null;return head('看有效结果，不看虚构增长。','只统计已记录的评论、跟进和消息事件；没有接入的数据不生成曲线或转化率。','')+`<div class="metrics">${metric('需求用户','以最新一条评论分类统计',s.buyers,'users-round',true)}${metric('人工核对评论','不是模型准确率',reviewed,'badge-check')}${metric('已确认导流','人工记录 · 非发送回执',s.referred,'handshake')}${metric('真实私信提交','不包含草稿与演示消息',s.submitted,'send')}</div><div class="grid-2"><div class="stack"><div class="card"><div class="card-head"><h2>评论类型分布</h2><small>总计 ${s.comments} 条评论</small></div><div class="card-body">${Object.entries(labels).map(([k,t])=>`<div class="bar-row"><div class="row spread"><span>${t}</span><span class="mono">${s.category_counts[k]||0}</span></div><div class="bar-track"><div class="bar-fill" style="width:${s.comments?((s.category_counts[k]||0)/s.comments*100):0}%"></div></div></div>`).join('')}</div></div><div class="card"><div class="card-head"><h2>跟进结果</h2><small>用户维度 · 手工记录</small></div><div class="table-scroll"><table><thead><tr><th>阶段</th><th>用户数</th></tr></thead><tbody>${Object.entries(stages).filter(([k])=>k!=='won'||S.leads.some(l=>l.stage==='won')).map(([k,v])=>`<tr><td>${v}</td><td class="mono">${S.leads.filter(l=>l.stage===k).length}</td></tr>`).join('')}</tbody></table></div></div></div><div class="stack"><div class="card"><div class="card-head"><h2>数据质量</h2></div><div class="card-body">${[['原视频链接',`${urls} / ${s.comments}`,'仅检查是否提供链接，未验证内容真实性'],['发布时间',`${lag.length} 条可计算时差`,'缺失或未来时间不参与时差统计'],['入库时差中位数',median===null?'暂无数据':`${Math.round(median)} 分钟`,'从发布时间到首次入库，不等于抓取器响应延迟'],['语义识别模型',S.semantic?.can_analyze?(S.semantic.config?.model||'已配置'):'未启用',`当前有 ${S.comments.filter(c=>c.analysis_method==='model').length} 条评论采用模型结果；人工判断优先，准确率待独立评测`]].map(x=>`<div class="source-row"><div><h3>${x[0]}</h3><small>${x[2]}</small></div><b style="font-size:12px;text-align:right">${x[1]}</b></div>`).join('')}</div></div><div class="card"><div class="card-head"><h2>指标边界</h2></div><div class="card-body"><p class="muted" style="font-size:12px">没有全平台总量，不能计算“全网覆盖率”；没有收件端或通道回执，不能计算真实送达率。已导流来自人工确认记录，不代表支付或履约；旧成交记录单独保留。</p></div></div></div></div>`;}


function showSettingsModal(title,content){showModal(title,content);$('#modal').classList.add('settings-modal');}
function workspaceSettingsDialog(){const form=settingsContent().match(/<form id="settings-form"[\s\S]*?<\/form>/)?.[0];showSettingsModal('工作区设置',form||'设置暂时无法加载');}
function settings(){return head('数据与设置','连接状态和配置入口。')+`<div class="settings-options">${[['工作区设置','发现关键词与规则计算','workspace-settings'],['评论监控','监控目标、时间范围和读取节奏','monitor-settings'],['直播监控','直播间、观察时长与筛选条件','live-settings'],['模型设置','当前分析通道与使用范围','semantic-settings']].map(([title,description,action])=>`<section class="card"><div class="card-body"><h2>${title}</h2><p class="muted">${description}</p>${button('打开设置',action,'small')}</div></section>`).join('')}</div><section class="card section-gap"><div class="card-head"><h2>功能连接情况</h2></div><div class="card-body">${connectionRows()}</div></section>`;}

function settingsContent(){return head('把数据边界，配置清楚。','配置可以保存；连接状态只有在实际接通并验证后才应改变。','')+`<div class="grid-2"><div class="stack"><div class="card"><div class="card-head"><h2>工作区设置</h2></div><form id="settings-form" class="card-body">${field('club_name','俱乐部名称',S.settings.club_name,'text','required maxlength="120"')}${field('focus_game','当前业务方向',TARGET_GAME,'text','readonly')}${area('keywords','无畏契约发现关键词',S.settings.keywords)}${area('excluded','内容排除关键词',S.settings.excluded)}<p class="muted" style="font-size:11px;margin:-4px 0 20px">发现关键词用于新建采集任务的默认值；排除词尚不自动执行，不会静默删除或过滤原文。</p>${field('analysis_workers','本地初筛并发数',S.settings.analysis_workers,'number','min="1" max="16" required')}<p class="muted" style="font-size:11px;margin:-4px 0 20px">仅控制本地规则计算，单批最多 1,000 条；不代表平台请求频率。</p>${submit('保存工作区设置')}</form></div><div class="card"><div class="card-head"><h2>数据来源</h2></div><div class="card-body">${S.sources.map(s=>`<div class="source-row"><div><h3>${esc(s.name)}</h3><small>${esc(s.notes||'未填写说明')}</small><small>最近接收：${date(s.last_received)}</small></div>${badge(s.kind==='import'?'人工导入':s.kind==='browser'?(s.last_received?'已有采集数据':'自建 · 待实测'):'适配器未接',s.kind==='import'||s.kind==='browser'&&s.last_received?'good':'warn')}</div>`).join('')}</div><div class="card-foot">浏览器与 HTTP 通道共享历史内容去重，实际方式以任务记录为准；会话不在表单或日志中展示。历史来源记录保留，来源名称不代表当前已经建立外部连接。</div></div></div><div class="stack">${semanticPanel()}<div class="card"><div class="card-head"><h2>功能连接情况</h2></div><div class="card-body">${connectionRows()}</div></div></div></div>`;}

function unlinkedCommentsDialog(){const rows=S.comments.filter(c=>!c.person_id);showModal('未关联用户的评论',notice('用户标识缺失，暂不建立用户线索或触达任务；原始评论证据仍保留。')+rows.map(c=>`<div class="history-item"><p>${esc(c.raw_text)}</p><small>评论 ID ${esc(c.external_id)} · 发布 ${date(c.published_at)}</small><div>${sourceLink(c)}</div></div>`).join(''));}
function videoDialog(){showModal('添加监控目标',field('url','视频链接','','url','placeholder="https://www.douyin.com/video/…"')+field('external_id','视频 ID（链接中没有 ID 时必填）')+field('title','视频标题（可选）')+`<div class="fields-2">${select('priority','优先级',opts({high:'高优先级',normal:'标准',low:'低优先级'},'normal'))}${field('interval_seconds','期望批次检查间隔 / 秒',300,'number','required min="10" max="86400"')}</div>`+notice('这里只登记目标；保存后可点击“采集一批”。期望间隔暂不触发定时运行。'),'video-form',submit('保存目标'));}
function memberDialog(id){const m=S.members.find(x=>x.id===id)||{};showModal(m.id?'编辑俱乐部人员':'添加俱乐部人员',`<input name="id" type="hidden" value="${m.id||''}">`+field('name','姓名 / 工作昵称',m.name,'text','required maxlength="120"')+field('game','服务游戏',m.game||TARGET_GAME,'text','readonly')+`<div class="fields-2">${field('region','区服',m.region,'text','placeholder="如：国服、亚服；按实际填写"')}${field('price','参考价格 / 元每小时',m.price,'number','min="0" max="100000" step="0.01"')}</div>`+`<fieldset class="service-picker"><legend>服务类型（可多选）</legend>${serviceTypes.map(s=>`<label class="check"><input name="service_types" type="checkbox" value="${s}" ${m.service_types?.includes(s)?'checked':''}>${s}</label>`).join('')}</fieldset>`+field('rank_label','当前段位 / 可接段位范围',m.rank_label,'text','placeholder="按实际填写；用于人工核对组队条件"')+field('skill','服务说明 / 擅长内容',m.skill,'text','placeholder="如：娱乐开黑、地图讲解、练枪复盘"')+field('availability','可用时段',m.availability,'text','placeholder="明确日期与时间，便于人工确认"')+`<label class="check"><input name="available" type="checkbox" ${m.available!==0?'checked':''}>当前可接单</label>`,'member-form',submit('保存人员'));}
function reviewDialog(id,provided=null){
  const c=provided||S.comments.find(x=>x.id===id);if(!c)throw Error('评论不存在');const f=c.facts||{};
  const gameNames=['无畏契约','三角洲行动','王者荣耀','英雄联盟','和平精英'];
  const fields=select('fact_game','游戏',opts({'':'未明确',...Object.fromEntries(gameNames.map(x=>[x,x]))},c.game))+select('fact_service_type','服务方向',opts({'':'未明确',...Object.fromEntries(serviceTypes.map(x=>[x,x]))},f.service_type))+Object.entries(reviewFieldLabels).filter(([key])=>!['game','service_type'].includes(key)).map(([key,label])=>field('fact_'+key,label,f[key]||'','text','maxlength="120" placeholder="原文没有明确表达时留空"')).join('');
  showModal('核对需求与字段',`<input type="hidden" name="id" value="${c.id}"><input type="hidden" name="review_token" value="${esc(c.review_token||'')}"><input type="hidden" name="evidence_type" value="${c.evidence_type||'comment'}"><blockquote class="quote">${esc(c.raw_text)}</blockquote><small>${evidenceName(c)} ID ${esc(c.external_id||'未知')} · ${esc(c.source_name)} · 发布 ${date(c.published_at)}</small>${parentEvidence(c)}${ruleEvidence(c,false)}`+select('category','确认分类',opts(labels,c.category))+select('fields_action','需求字段处理',opts({keep:'仅改分类，保留规则或已核对字段',confirm:'同时核对并保存以下字段',reset:'撤销字段纠正，恢复先前规则值'},'keep'))+`<fieldset id="review-fields" class="review-fields" disabled><legend>需求字段 · 只填写明确依据</legend><div class="fields-2">${fields}</div></fieldset>`+area('reason','人工判断依据',c.analysis_method==='human'?c.reason:'',c.evidence_type==='live'?'required placeholder="请填写本条弹幕的判断依据；无法确认的字段留空"':'placeholder="纠正或撤销字段时必填；写明原文依据，无法确认请留空字段"')+notice('字段纠正单独保存，不改原文或规则证据。留空表示人工标记未知；重复初筛不覆盖人工判断，原文修订后需重新核对。'),'review-form',submit('保存人工判断'));
}
async function historyDialog(id,offset=0){
  const l=S.leads.find(x=>x.id===id);if(!l)throw Error('线索不存在');
  const comments=S.comments.filter(c=>c.person_id===l.person_id);let live={rows:[],total:0,offset:0,limit:50};
  if(l.live_count){const response=await fetch(`/api/lead-live-history?mode=${mode}&lead_id=${id}&offset=${offset}`);live=await response.json();if(!response.ok)throw Error(live.error||'直播历史读取失败');}
  const item=c=>`<div class="history-item"><div class="row spread">${category(c.category)}<small>${date(c.published_at)}</small></div><p>${esc(c.raw_text)}</p><small>${evidenceName(c)}来源：${esc(c.source_title||c.video_title)} · ID ${esc(c.external_id||'未知')}</small><small>入库 ${date(c.discovered_at)} · ${esc(c.source_name)}</small>${parentEvidence(c)}${ruleEvidence(c)}<div class="row spread" style="font-size:.875rem;margin-top:10px">${sourceLink(c)}${button('核对分类',c.evidence_type==='live'?'live-review':'history-review','small',`data-id="${c.id}"`)}</div></div>`;
  showModal(`${esc(l.nickname)} · 来源历史`,(comments.length?`<h3>视频评论 · ${comments.length} 条</h3>`+comments.map(item).join(''):'')+(live.total?`<h3>已存档弹幕 · ${live.total} 条</h3>`+live.rows.map(item).join('')+`<div class="actions">${button('上一页弹幕','live-history-page','small',`data-id="${id}" data-offset="${Math.max(0,live.offset-live.limit)}" ${live.offset===0?'disabled':''}`)}<small>第 ${Math.floor(live.offset/live.limit)+1}/${Math.max(1,Math.ceil(live.total/live.limit))} 页</small>${button('下一页弹幕','live-history-page','small',`data-id="${id}" data-offset="${live.offset+live.limit}" ${live.offset+live.limit>=live.total?'disabled':''}`)}</div>`:'')||'<p>暂无来源记录。</p>');
}
function contactDialog(id){const l=S.leads.find(x=>x.id===id);showModal('登记联系依据',`<input name="lead_id" type="hidden" value="${id}">`+select('contact_basis','联系依据',opts({'':'尚未确认',inbound:'用户主动咨询',opt_in:'用户已同意联系',...(mode==='demo'?{test:'隔离测试收件人'}:{})},l.contact_basis))+area('contact_note','依据与记录',l.contact_note,'placeholder="记录咨询时间、来源或同意联系的说明，不要填写密码或令牌"')+`<label class="check"><input type="checkbox" name="do_not_contact" ${l.do_not_contact?'checked':''}>禁止联系 / 用户已拒绝</label>`+notice('这里是内部记录，不会授予平台权限，也不会绕过通道限制。真实发送仍需实际接入和校验。'),'contact-form',submit('保存联系记录'));}
function followDialog(id){const l=S.leads.find(x=>x.id===id);const availableStages=Object.fromEntries(Object.entries(stages).filter(([key])=>key!=='won'||l.stage==='won'));showModal('更新导流记录',`<input type="hidden" name="id" value="${id}">`+select('stage','联系阶段',opts(availableStages,l.stage))+field('owner','负责人',l.owner)+area('outcome_note','沟通 / 导流记录',l.outcome_note,'placeholder="记录用户反馈、承接方和确认时间；不要把发出消息当作导流成功" maxlength="5000"')+'<p class="muted">选择“已导流”须填写确认记录；保存不会发送消息或改变联系权限。</p>','follow-form',submit('保存导流结果'));}
function stashDraft(){if(conversation)draftCache.set(`${mode}:${conversation}`,{text:draftText,key:draftKey});}
function restoreDraft(id){conversation=id;const cached=draftCache.get(`${mode}:${id}`);draftText=cached?.text||'';draftKey=cached?.key||'';}

async function handleAction(el){const a=el.dataset.action,id=Number(el.dataset.id);switch(a){
case 'menu': setMobileNavigation(true);break;
case 'close-menu': setMobileNavigation(false);break;
case 'close': closeModal();break;
case 'refresh': await load();toast('数据已刷新');break;
case 'uid-http-check': await save('uid-http-check',{},'本地配置已检查，没有发送消息');break;
case 'uid-http-probe': {el.disabled=true;try{const r=await api('uid-http-probe',{});showModal('HTTP 登录身份核对',`<p>${esc(r.detail)}</p><p class="muted">核对时间：${date(r.checked_at)}</p><p>该结果仅用于核对当时的登录身份。发送时仍会重新核对账号、接收方和联系依据。</p>`);}finally{el.disabled=false;}break;}
case 'uid-http-target-new': uidTargetDialog();break;
case 'uid-http-send': {const r=await save('uid-http-send',{id},'');toast(r.detail||'任务已处理',r.status!=='accepted');break;}
case 'dm-test-check': await save('dm-test-check',{},'已重新检查本地配置；没有发送消息');break;
case 'dm-test-send': {const r=await save('dm-test-send',{},'');toast(r.attempt?.detail||'发送条件未满足，没有发出请求',r.status!=='api_accepted');break;}
case 'semantic-analyze': {closeModal();const r=await save('semantic-analyze',{evidence_type:el.dataset.kind,id,input_hash:el.dataset.hash,request_id:crypto.randomUUID()},'');toast(r.detail,r.status!=='completed');break;}
case 'semantic-queue-cancel': {await save('semantic-queue-cancel',{},'当前模型队列已请求停止');break;}
case 'analyze': {const r=await save('analyze',{},'');toast(`已完成 ${r.analyzed} 条规则初筛${S.stats.pending?`，还有 ${S.stats.pending} 条待处理`:''}；未调用语义模型`);break;}
case 'discovery-settings':discoverySettingsDialog();break;
case 'discovery-authors':discoveryAuthorsDialog();break;
case 'author-focus':case 'author-toggle': {const row=discoveryState().authors.find(r=>r.sec_uid===el.dataset.key);if(!row)throw Error('作者已更新，请重新打开作者池');await api('discovery-author',{sec_uid:row.sec_uid,enabled:el.dataset.action==='author-toggle'?!row.enabled:!!row.enabled,priority:el.dataset.action==='author-focus'?(row.priority==='focus'?'auto':'focus'):row.priority});await load();discoveryAuthorsDialog();break;}
case 'monitor-settings':monitorSettingsDialog();break;
case 'live-settings':liveSettingsDialog();break;
case 'workspace-settings':workspaceSettingsDialog();break;
case 'semantic-settings':showSettingsModal('模型设置',semanticPanel());break;
case 'uid-settings':uidSettingsDialog();break;
case 'add-video': videoDialog();break;
case 'collector-new': collectorDialog();break;
case 'work-filter': workFilter=el.dataset.filter;workPage=1;redrawWorkPool();break;
case 'work-page': workPage+=Number(el.dataset.step)||0;redrawWorkPool();break;
case 'work-comments': await changeMonitorSelection(el.dataset.url);break;
case 'work-clear': await changeMonitorSelection('');break;
case 'monitor-history-retry': await refreshMonitorHistory();break;
case 'work-log': {const panel=$('#monitor-run-details');if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'start'});}break;}
case 'monitor-result-filter': monitorResultFilter=['valuable','all','accepted','filtered'].includes(el.dataset.filter)?el.dataset.filter:'valuable';monitorResultPage=1;redrawMonitorResults();await refreshMonitorHistory();break;
case 'monitor-result-page': monitorResultPage+=Number(el.dataset.step)||0;redrawMonitorResults();await refreshMonitorHistory();break;
case 'freshness-preset': {const input=$('[name="freshness_target_seconds"]');if(input){input.value=el.dataset.seconds;markMonitorDraft(input);}break;}
case 'monitor-reset': monitorDraftDirty=false;$('#monitor-settings').outerHTML=monitorSettingsPanel();syncCollectionForms();icons();break;
case 'monitor-start': monitorStartDialog();break;
case 'monitor-stop': await save('monitor-stop',{},'监控已关闭，正在停止关联任务；历史记录保留');break;
case 'collector-video': collectorDialog(S.videos.find(v=>v.id===id));break;
case 'collector-retry': {const t=collectionState().tasks.find(t=>t.id===id);collectorDialog(null,t);break;}
case 'collector-cancel': await save('collector-cancel',{id},'正在停止，已入库数据会保留');break;
case 'collector-resume': await save('collector-resume',{id},'继续检查当前页面');break;
case 'collector-checkpoint': checkpointDialog(id);break;
case 'plan-new': planDialog();break;
case 'plan-edit': planDialog(id);break;
case 'plan-start': await save('collection-plan-start',{id},'有限采集计划已启用');break;
case 'plan-pause': await save('collection-plan-pause',{id},'后续批次已暂停；当前浏览器任务需单独停止');break;
case 'collector-evidence': {const r=await fetch(`/api/collector-evidence?mode=${mode}&id=${id}`);const data=await r.json();if(!r.ok)throw Error(data.error||'证据读取失败');showModal(`采集任务 #${id} · 观察记录`,notice('以下为采集时保存的来源与内容摘要哈希，不含 Cookie 或网络令牌。视频记录不等于已读到评论。')+((data.diagnostics||[]).map(d=>d.snapshot.verification?verificationEvidence(d):`<div class="history-item"><strong>页面诊断 · ${esc(d.stage)}</strong><small>${date(d.created_at)} · ${esc(d.snapshot.title)}</small><small>${esc(d.snapshot.page_url)} · 视频链接 ${d.snapshot.video_links}</small><pre>${esc(d.snapshot.visible_text||d.snapshot.navigation_error||'页面未提供可见文本')}</pre></div>`).join(''))+(data.observations.length?data.observations.map(o=>`<div class="history-item"><strong>${o.kind==='video'?'视频':'评论'} · ${esc(o.external_id)}</strong><small>${date(o.observed_at)}</small><a href="${esc(o.page_url)}" target="_blank" rel="noopener noreferrer">打开原视频 ↗</a><small class="evidence-hash">SHA-256 ${esc(o.payload_hash)}</small></div>`).join(''):'<p>尚无页面数据观察记录。</p>'));break;}
case 'toggle-video': await save('video-toggle',{id});break;
case 'add-member': memberDialog();break;
case 'edit-member': memberDialog(id);break;
case 'toggle-member': await save('member-toggle',{id});break;
case 'go-monitor': navigate('monitor');break;
case 'live-start': {if(liveDraftDirty)throw Error('请先保存修改后的直播配置');await save('live-start',{request_id:crypto.randomUUID()},'直播读取已启动，不发送弹幕或私信');break;}
case 'live-stop': await save('live-stop',{id},'正在停止本次直播会话');break;
case 'live-track-start': if(liveDraftDirty)throw Error('请先保存修改后的直播配置');await save('live-track-start',{request_id:crypto.randomUUID()},'后台持续跟踪已开启');break;
case 'live-track-stop': await save('live-track-stop',{id},'持续跟踪已关闭，正在停止当前批次');break;
case 'live-archive-refresh': liveArchiveAnchor=null;livePage=1;await refreshLiveArchive(true);break;
case 'live-discover': {const value=await api('live-discover');S.collector.live_monitor.discovery=value;const panel=$('#live-discovery'),opened=!!panel?.open;if(panel){panel.outerHTML=liveDiscoveryPanel();$('#live-discovery').open=opened;icons();}toast(value.status==='ready'?'直播候选已更新；选择房间后保存配置':'未更新候选，请查看具体状态',value.status!=='ready');break;}
case 'live-use-room': {const row=liveState().discovery?.rows?.[Number(el.dataset.index)];if(row&&!$('#live-form'))liveSettingsDialog();const form=$('#live-form');if(row&&form){form.elements.namedItem('room_url').value=row.room_url;markMonitorDraft(form.elements.namedItem('room_url'));form.scrollIntoView({behavior:'smooth',block:'start'});toast('直播间已填入；保存配置后再开启读取');}break;}
case 'live-review': {const r=await fetch(`/api/live-message?mode=${mode}&id=${id}`);const row=await r.json();if(!r.ok)throw Error(row.error||'弹幕读取失败');reviewDialog(id,row);break;}
case 'live-open-lead': await load();resetLeadFilters();tab='all';selected=id;navigate('leads');break;
case 'live-history-page': await historyDialog(id,Number(el.dataset.offset)||0);break;
case 'live-prev': livePage=Math.max(1,livePage-1);redrawLiveResults();await refreshLiveArchive();break;
case 'live-next': livePage++;redrawLiveResults();await refreshLiveArchive();break;
case 'unlinked-comments': unlinkedCommentsDialog();break;
case 'go-leads': serviceFilter='';navigate('leads');break;
case 'service-leads': resetLeadFilters();serviceFilter=el.dataset.service;tab='buyer';navigate('leads');break;
case 'apply-published-range': publishedFrom=$('#published-from').value;publishedUntil=$('#published-until').value;selected=null;render();break;
case 'clear-lead-filters': resetLeadFilters();render();break;
case 'lead-tab': tab=el.dataset.tab;selected=null;render();break;
case 'select-lead': selected=id;if(page!=='leads'){resetLeadFilters();selected=id;tab='all';navigate('leads');}else render();revealLeadDetail();break;
case 'review': reviewDialog(id);break;
case 'history': historyDialog(id);break;
case 'history-review': closeModal();reviewDialog(id);break;
case 'follow': followDialog(id);break;
case 'contact': contactDialog(id);break;
case 'roster-game': gameFilter=el.dataset.game;render();break;
case 'open-chat': stashDraft();restoreDraft(id);navigate('inbox');break;
case 'chat-select': stashDraft();restoreDraft(id);render();break;
case 'suggest': {const l=S.leads.find(x=>x.id===conversation);draftText=replyTemplate(l);draftKey='';stashDraft();$('#draft-content').value=draftText;toast('已填入模板，请结合真实需求修改');break;}
case 'send': {const r=await save('send',{id},'');toast(r.detail,r.status==='blocked');break;}
}}
const monitorBrowseActions=new Set(['work-filter','work-page','work-comments','work-clear','monitor-history-retry','monitor-result-filter','monitor-result-page']);
async function handleActionClick(e){
  const el=e.target.closest('[data-action]');if(!el||el.disabled)return;e.preventDefault();
  // Read-only navigation can change while a prior history request is pending.
  // refreshMonitorHistory accepts only the response for the latest selection.
  if(monitorBrowseActions.has(el.dataset.action)){try{await handleAction(el);}catch(err){toast(err.message,true);}return;}
  if(busy)return;busy=true;const isButton=el.tagName==='BUTTON';if(isButton)el.disabled=true;try{await handleAction(el);}catch(err){toast(err.message,true);}finally{busy=false;if(isButton&&el.isConnected)el.disabled=false;}
}
document.addEventListener('click',handleActionClick);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('menu-open')){setMobileNavigation(false);}const row=e.target.closest('tr[data-action]');if(row&&e.target===row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();row.click();}});
document.addEventListener('input',e=>{markMonitorDraft(e.target);if(e.target.id==='work-search'){const pos=e.target.selectionStart;workQuery=e.target.value;workPage=1;redrawWorkPool();const input=$('#work-search');input.focus();input.setSelectionRange(pos,pos);}if(e.target.id==='monitor-result-search'){const pos=e.target.selectionStart;monitorResultQuery=e.target.value;monitorResultPage=1;redrawMonitorResults();void refreshMonitorHistory();const input=$('#monitor-result-search');input.focus();input.setSelectionRange(pos,pos);}if(e.target.id==='lead-search'){const pos=e.target.selectionStart;query=e.target.value;render();const el=$('#lead-search');el.focus();el.setSelectionRange(pos,pos);}if(e.target.id==='draft-content'){draftText=e.target.value;draftKey='';stashDraft();}});
document.addEventListener('change',async e=>{
  markMonitorDraft(e.target);
  if(['kind','transport'].includes(e.target.name))syncCollectionForm(e.target.closest('form'));
  if(e.target.name==='fields_action')$('#review-fields').disabled=e.target.value!=='confirm';
  const controls={'game-filter':v=>gameFilter=v,'service-filter':v=>serviceFilter=v,'published-filter':v=>publishedFilter=v,'lead-sort':v=>leadSort=v};
  if(controls[e.target.id]){const id=e.target.id;controls[id](e.target.value);selected=null;render();$('#'+id)?.focus();}
});
document.addEventListener('submit',async e=>{const form=e.target;const formId=form.getAttribute('id');if(!formId)return;e.preventDefault();if(busy)return;busy=true;const submitButton=$('button[type="submit"]',form);if(submitButton)submitButton.disabled=true;try{const formError=$('.form-error',form);if(formError){formError.hidden=true;formError.textContent='';}const values=Object.fromEntries(new FormData(form));
  if(formId==='composer'){draftText=values.content.trim();if(!draftText)throw Error('请先填写消息内容');draftKey=draftKey||crypto.randomUUID();stashDraft();await api('draft',{lead_id:conversation,content:draftText,request_id:draftKey});draftText='';draftKey='';stashDraft();await load();toast('草稿已保存，尚未发送');}
  else if(formId==='semantic-form'){await api('semantic-save',{backend:values.backend,api_base_url:values.api_base_url.trim(),...(values.api_key?{api_key:values.api_key}:{}),enabled:values.enabled==='true',host:values.host,port:Number(values.port),model:values.model.trim(),timeout_seconds:Number(values.timeout_seconds),max_concurrency:Number(values.max_concurrency||1),auto_analyze:values.auto_analyze==='true',live_model_enabled:values.live_model_enabled!==undefined?values.live_model_enabled==='true':S.semantic?.config?.live_model_enabled!==false});semanticDraftDirty=false;closeModal();await load();toast('模型配置已保存，没有启动分析或下载模型');}
  else if(formId==='settings-form'){await api('settings',values);closeModal();await load();toast('工作区设置已保存');}
  else {let action,body=values,message='已保存';switch(formId){
    case 'discovery-form': action='discovery-save';body={enabled:values.enabled==='true',keywords:values.keywords.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean),seed_videos:values.seed_videos.split(/[,，\n]/).map(v=>v.trim()).filter(Boolean),...Object.fromEntries(['search_interval','author_interval','focus_interval','work_interval','focus_min_related','focus_ratio','initial_author_pages'].map(k=>[k,Number(values[k])]))};message='持续发现配置已保存，运行服从评论监控总开关';break;
    case 'video-form': action='video';break;
    case 'uid-target-form': action='uid-http-target';message='授权测试对象已登记，未发送消息';break;
    case 'live-form': action='live-save';body={...values,interactive:false};message='直播配置已保存，后台运行；正在运行的会话参数保持原样';break;
    case 'collector-form': action='collector-start';body={...values,...(values.video_limit===undefined?{}:{video_limit:Number(values.video_limit)}),comment_limit:Number(values.comment_limit),interactive:true};message=values.transport==='http'?'HTTP 任务已创建；不会自动打开浏览器。':'任务已创建，将打开专用浏览器；需要登录或验证时请在该窗口处理。';break;
    case 'checkpoint-form': action='collector-restart';message='已从保存的视频断点创建新任务；中断视频重新加载并去重。';break;
    case 'plan-form': action='collection-plan-save';message='计划已保存，默认暂停；点击启动才会按批运行';break;
    case 'monitor-form': action='monitor-save';body={...values,lookback_hours:Number(values.window_value)*(values.window_unit==='days'?24:1)};delete body.window_value;delete body.window_unit;message='监控配置已保存，保持关闭；下批使用新的并发、间隔和过滤条件';break;
    case 'monitor-start-form': action='monitor-start';body={};message='评论监控已开启，将按批读取；此操作不会发送私信';break;
    case 'member-form': action='member';if(!values.game)throw Error('请选择游戏');body={...values,available:!!values.available,service_types:new FormData(form).getAll('service_types')};break;
    case 'review-form': action=values.evidence_type==='live'?'live-review':'review';body={id:values.id,category:values.category,reason:values.reason,review_token:values.review_token};if(values.fields_action==='confirm')body.manual_fields=Object.fromEntries(Object.keys(reviewFieldLabels).map(key=>[key,values['fact_'+key]||'']));if(values.fields_action==='reset')body.manual_fields={};break;
    case 'contact-form': action='contact';body={...values,do_not_contact:!!values.do_not_contact};break;
    case 'follow-form': action='lead';break;
    default: throw Error('未识别的表单');
  }await api(action,body);if(action==='monitor-save')monitorDraftDirty=false;if(action==='live-save')liveDraftDirty=false;closeModal();await load();toast(message);}
}catch(err){const formError=$('.form-error',form);if(formError){formError.textContent=err.message;formError.hidden=false;}toast(err.message,true);}finally{busy=false;if(submitButton?.isConnected)submitButton.disabled=false;}});
window.addEventListener('hashchange',()=>{monitorDraftDirty=false;liveDraftDirty=false;semanticDraftDirty=false;stashDraft();page=location.hash.slice(1)||'overview';gameFilter='';query='';setMobileNavigation(false,false);render();window.scrollTo({top:0});});
$('#modal').addEventListener('click',e=>{if(e.target===$('#modal')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeModal();}});
load().catch(err=>{$('#main').innerHTML=empty('暂时无法连接工作台',`请确认电脑已开机、联网并运行 ClubOps。${esc(err.message)}`,'refresh','重试','unplug');icons();});

const collectionLabels={queued:'等待启动',running:'正在读取',cancelling:'正在停止',needs_login:'等待登录',needs_verification:'等待人工验证',needs_interaction:'等待页面操作',completed:'本批读取完成',partial:'部分读取',failed:'执行失败',cancelled:'已停止',interrupted:'会话中断',rate_limited:'访问频繁 · 已停止',access_denied:'访问被拒绝',no_data:'未取得可识别数据',dependency_missing:'缺少运行依赖',session_expired:'会话已过期',network_error:'连接失败',schema_changed:'响应结构需适配',timeout:'运行超时',resource_limited:'读取预算已达上限',empty_response:'空响应 · 未取得数据',identity_failed:'身份核对未通过',upstream_rejected:'平台业务响应未通过'};
function collectorHelp(t){
  if(t.transport==='http'&&['needs_login','needs_verification','session_expired','identity_failed'].includes(t.status))return `<aside class="collector-help"><strong>HTTP 读取已暂停</strong><p>请在项目专用抖音会话完成登录或验证，再重新准备采集会话并新建任务。当前任务不会自动打开浏览器或重试。</p></aside>`;
  if(!t.active||!['needs_login','needs_verification','needs_interaction'].includes(t.status))return '';
  return `<aside class="collector-help" aria-label="浏览器操作指引"><strong>在哪登录或验证？</strong><ol><li>按 Alt + Tab，切到本任务单独打开的 Chrome 抖音窗口，不是在开发者后台或本工作台登录。</li><li>${t.status==='needs_interaction'?'在那个窗口打开视频评论区，加载可见评论。':'在抖音页面扫码登录；出现验证码时自行完成。若提示“使用原设备扫码”，需在原设备上的抖音完成账号验证。无需粘贴 Cookie。'}</li><li>完成后回到此任务，点击“已处理，继续读取”。</li></ol><p>等待最多 10 分钟，超时会关闭窗口；届时可重新采集。不要在未完成页面操作时反复点击继续。</p></aside>`;
}
function transportField(value){return select('transport','读取通道',opts(S.collector?.http?{http:'后端 HTTP',local_browser:'本机浏览器'}:{local_browser:'本机浏览器'},value||(S.collector?.http?.session?.ready?'http':'local_browser')))+(S.collector?.http?.session?.endpoints?.search==='needs_verification'?'<p class="muted">关键词搜索等待抖音验证；当前可使用指定视频的 HTTP 评论读取。</p>':'');}
function collectionState(){const value=S?.collector||{available:false,tasks:[],last_received:null};return {...value,active:value.tasks.some(t=>t.active)};}
function monitorConfig(){return S.collector?.monitor||{enabled:false,active_task_id:null,stopping:false,kind:'search',target:(S.settings?.keywords||'无畏契约陪玩').split(/[,，\n]/)[0],lookback_hours:1,interval_seconds:30,video_limit:3,comment_limit:30,page_concurrency:1,include_keywords:'',exclude_keywords:'',status:'paused',detail:'监控未开启',run_count:0};}
function workBoard(){return S.collector?.board||{rows:[],summary:{tracked:0,reading:0,works:0,fresh_comments:0,model_pending:0,active_plans:0}};}
function monitorOverview(){
  const board=workBoard(),n=board.summary,t=S.collector?.results?.timeliness,latest=collectionState().tasks[0];
  const blocked=latest&&['resource_limited','needs_verification','needs_login','rate_limited','schema_changed','identity_failed','failed','network_error','access_denied'].includes(latest.status);
  const cards=[['跟踪中作品',board.rows.filter(workIsTracked).length,'含持续轮询、待首次采集与当前读取','radar'],['当前读取',n.reading,'此刻正在读取的作品','activity'],['近 1 小时新评论',n.fresh_comments,'发布与首次采集均在近 1 小时','message-circle'],['等待模型处理',n.model_pending,'排队与分析中的评论','brain-circuit'],['采集时效达标率',t?.measured?`${Math.round(t.within_target/t.measured*100)}%`:'待测',`目标 ${durationText(monitorConfig().freshness_target_seconds||60)} · ${t?.measured?`最近批次 ${t.measured} 条样本`:'暂无新样本'}`,'timer']];
  return `<section id="monitor-overview" aria-label="监控全局状态">${monitorControls()}<div class="monitor-metrics">${cards.map(([label,value,note,ico])=>`<article class="monitor-metric"><div>${icon(ico)}<span>${label}</span></div><strong>${typeof value==='number'?fmt(value):value}</strong><small>${note}</small></article>`).join('')}</div>${blocked?`<div class="monitor-attention" role="status">${icon('circle-alert')}<div><strong>最近批次 #${latest.id} · ${esc(collectionLabels[latest.status]||latest.status)}</strong><span>${esc(latest.detail)}</span></div>${button('查看运行记录','work-log','small')}</div>`:''}</section>`;
}
function workMetrics(r){const m=r.metrics;return `<span class="work-platform-metrics">${[['likes','点赞','heart'],['comments','平台评论','message-circle'],['shares','分享','share'],['favorites','收藏','bookmark']].map(([key,label,ico])=>`<span>${icon(ico)}${label}<b>${m&&typeof m[key]==='number'?fmt(m[key]):'未获取'}</b></span>`).join('')}</span><small class="work-metrics-time">${m?.updated_at?`数据更新 ${date(m.updated_at)}`:'平台数据尚未获取'}</small>`;}
const workIsTracked=r=>!!r.continuous_monitoring||['reading','queued','pending_read'].includes(r.state);
function workPool(){
  const all=workBoard().rows,tracked=all.filter(workIsTracked).length,filtered=all.filter(r=>(workFilter==='all'||(workFilter==='tracked'?workIsTracked(r):!workIsTracked(r)))&&(!workQuery||[r.title,r.external_id,r.author_name].join(' ').toLocaleLowerCase().includes(workQuery.toLocaleLowerCase())));
  const pageCount=Math.max(1,Math.ceil(filtered.length/10));workPage=Math.min(pageCount,Math.max(1,workPage));
  const rows=filtered.slice((workPage-1)*10,workPage*10),names={reading:'正在读取',queued:'等待读取',pending_read:'等待首次采集',monitoring:'持续跟踪',attention:'监控待处理',paused:'监控已暂停',history:'未纳入持续监控'};
  const tabs=[['all','全部作品',all.length],['tracked','跟踪中',tracked],['untracked','未跟踪',all.length-tracked]];
  return `<section id="work-pool" class="card work-pool" aria-label="作品监控池"><div class="card-head"><div><h2>${icon('clapperboard')} 作品库 <span class="pool-count">${all.length}</span></h2><small>本地已收录 · 点击作品查看评论</small></div>${button(icon('plus'),'add-video','small',`aria-label="登记作品" ${mode==='demo'?'disabled':''}`)}</div><div class="work-list-tabs">${tabs.map(([key,label,count])=>button(`${label} ${count}`,'work-filter',`result-tab ${workFilter===key?'selected':''}`,`data-filter="${key}" aria-pressed="${workFilter===key}"`)).join('')}</div>${button(`${icon('messages-square')} 全部评论 ${icon('chevron-right')}`,'work-clear',`work-all ${!monitorResultVideo?'selected':''}`,`aria-pressed="${!monitorResultVideo}"`)}<label class="result-search work-search"><input id="work-search" type="search" aria-label="搜索作品、作者或视频 ID" placeholder="搜索作品 / 作者" value="${esc(workQuery)}"></label><div class="work-list">${rows.length?rows.map(r=>{
    const selected=monitorResultVideo===r.url,title=r.title===r.external_id?'标题待补全':r.title;
    return `<article class="work-item ${selected?'work-selected':''}"><button class="work-select" data-action="work-comments" data-url="${esc(r.url)}" aria-pressed="${selected}"><span class="work-item-status">${badge(names[r.state]||'状态未知',['reading','monitoring'].includes(r.state)?'good':r.state==='attention'?'warn':'')}${r.transport?`<small>${r.transport==='http'?'HTTP':'浏览器'}</small>`:''}</span><strong class="work-item-title" title="${esc(title)}">${esc(title)}</strong><span class="cell-sub mono">${esc(r.external_id)}</span>${r.author_name?`<small class="cell-sub">作者：${esc(r.author_name)}</small>`:''}${r.published_at?`<small class="cell-sub">作品发布：${date(r.published_at)}</small>`:''}${workMetrics(r)}<span class="work-item-counts">近 1h <b>${fmt(r.fresh_comments)}</b> · 已存档 <b>${fmt(r.archived_comments)}</b> · 模型待处理 <b>${fmt(r.model_pending)}</b></span><small class="work-item-time">${r.last_checked_at?`最近检查 ${esc(date(r.last_checked_at))}`:'尚未检查'}<br>${r.next_check_at?`下次到期 ${esc(date(r.next_check_at))} · 按队列检查`:r.active_task_id?'本批进行中':r.continuous_monitoring?'等待调度':'未安排下一次检查'}</small></button><div class="work-item-actions"><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">查看原作品 ${icon('arrow-up-right')}</a>${r.id?button('采集一批','collector-video','small',`data-id="${r.id}" ${mode==='demo'||!r.enabled||collectionState().active?'disabled':''}`):''}</div></article>`;
  }).join(''):`<p class="work-empty">${workFilter==='tracked'?'当前没有持续跟踪或正在读取的作品。':'暂无对应作品。'}</p>`}</div><div class="result-footer"><small>${workQuery?`${filtered.length} 条匹配 · `:''}已收录作品长期保留</small><div class="actions">${button('上一页','work-page','small',`data-step="-1" ${workPage===1?'disabled':''}`)}<span>${workPage}/${pageCount}</span>${button('下一页','work-page','small',`data-step="1" ${workPage===pageCount?'disabled':''}`)}</div></div></section>`;
}
function redrawWorkPool(){const panel=$('#work-pool');if(!panel)return;const top=$('.work-list')?.scrollTop||0;panel.outerHTML=workPool();const list=$('.work-list');if(list)list.scrollTop=top;icons();}
function discoveryState(){return S.collector?.discovery||{enabled:false,configured:false,config:{enabled:false,keywords:['无畏契约陪玩','无畏契约陪练','瓦陪玩','无畏契约开黑','无畏契约复盘','VALORANT陪玩'],seed_videos:[],search_interval:300,author_interval:600,focus_interval:90,work_interval:60,focus_min_related:8,focus_ratio:80,initial_author_pages:3},authors:[],author_count:0,focused_count:0,counts:{related:0,awaiting:0,watched:0}};}
function discoveryOverview(){const d=discoveryState();return `<section id="discovery-overview" class="card section-gap" aria-label="作品与作者发现"><div class="card-head"><div><h2>作品与作者持续发现 ${badge(d.enabled?'运行中':d.configured?'等待总监控开启':'未启用',d.enabled?'good':'')}</h2><small>已发现相关作品 ${fmt(d.counts.related)} · 待首次采集 ${fmt(d.counts.awaiting)} · 作者 ${fmt(d.author_count)} · 重点作者 ${fmt(d.focused_count)}</small></div><div class="actions">${button('作者池','discovery-authors','small')}${button(icon('sliders-horizontal')+' 发现设置','discovery-settings','small',mode==='demo'?'disabled':'')}</div></div></section>`;}
function discoverySettingsDialog(){const c=discoveryState().config;showModal('作品与作者发现设置',
  select('enabled','持续作品发现',opts({true:'开启，随评论监控运行',false:'关闭，只运行原监控目标'},String(c.enabled)))+
  area('keywords','发现关键词（每行一个，最多 12 个）',c.keywords.join('\n'))+area('seed_videos','作者种子作品 ID（每行一个，最多 10 个）',c.seed_videos.join('\n'))+
  `<div class="fields-2">${field('search_interval','同一关键词再次发现间隔 / 秒',c.search_interval,'number','min="60" max="86400" required')}${field('author_interval','普通作者检查间隔 / 秒',c.author_interval,'number','min="60" max="86400" required')}${field('focus_interval','重点作者检查间隔 / 秒',c.focus_interval,'number','min="30" max="86400" required')}${field('work_interval','活跃作品评论检查间隔 / 秒',c.work_interval,'number','min="30" max="3600" required')}${field('focus_min_related','自动重点关注：至少相关作品数',c.focus_min_related,'number','min="3" max="100" required')}${field('focus_ratio','自动重点关注：相关占比至少 %',c.focus_ratio,'number','min="50" max="100" required')}${field('initial_author_pages','首次检查作者最多页数',c.initial_author_pages,'number','min="1" max="3" required')}</div>`+
  '<p class="muted">作品按游戏文案初筛入库，不直接判为客户。作者占比只统计实际读到的作者作品，搜索命中不计入样本。每页最多 10 个；后续先检查第一页的新作。评论沿用总监控的时间窗口、筛选及单批预算。安静作品逐步降频，作品和历史持续保留；到期后按队列检查，间隔不代表所有作品都能在该时间内完成。</p>','discovery-form',submit('保存发现设置'));$('#modal').classList.add('settings-modal');}
function discoveryAuthorsDialog(){const d=discoveryState();showSettingsModal('作者池',`<p>已发现 ${fmt(d.author_count)} 位作者，其中 ${fmt(d.focused_count)} 位重点关注。相关率按已检查的作者作品计算；关注调整从下批生效。列表最多展示前 ${d.author_limit||200} 位。</p><div class="result-table-scroll author-table-scroll"><table class="result-table author-table"><thead><tr><th>作者</th><th>相关作品 / 已检查</th><th>关注状态</th><th>最近 / 下次检查</th><th>管理</th></tr></thead><tbody>${d.authors.map(r=>`<tr><td><a href="https://www.douyin.com/user/${esc(r.sec_uid)}" target="_blank" rel="noopener noreferrer">${esc(r.nickname)}</a></td><td>${r.sampled?`${r.related} / ${r.sampled}<small class="cell-sub">相关占比 ${r.ratio}%</small>`:'尚未检查作者作品'}</td><td>${badge(!r.enabled?'已暂停':r.focused?'重点关注':'普通关注',r.enabled&&r.focused?'good':'')}</td><td class="author-check-time">${r.last_checked_at?date(r.last_checked_at):'尚未自动检查'}<small class="cell-sub">${r.enabled&&d.enabled?`下次到期 ${date(r.next_check_at)}`:'尚未安排'}</small></td><td>${button(r.priority==='focus'?'改为自动':r.focused?'固定重点':'设为重点','author-focus','small',`data-key="${esc(r.sec_uid)}" ${mode==='demo'?'disabled':''}`)}${button(r.enabled?'暂停关注':'恢复关注','author-toggle','small',`data-key="${esc(r.sec_uid)}" ${mode==='demo'?'disabled':''}`)}</td></tr>`).join('')||'<tr><td colspan="5" class="result-empty">从相关作品中识别作者后，会在这里持续积累。</td></tr>'}</tbody></table></div>`);}
function historyKey(){return [mode,monitorResultVideo,monitorResultFilter,monitorResultQuery,monitorResultPage].join('\n');}
function historyScopeKey(){return [mode,monitorResultVideo].join('\n');}
function rememberMonitorHistory(cache,key,value){cache.delete(key);cache.set(key,value);if(cache.size>40)cache.delete(cache.keys().next().value);}
async function refreshMonitorHistory(){
  if(page!=='monitor')return;
  const key=historyKey(),sequence=++monitorHistorySequence;
  const wasError=!!monitorHistoryError;monitorHistoryError='';monitorHistoryErrorKey='';
  if(wasError)redrawMonitorResults();
  try{
    const params={mode,video:monitorResultVideo,filter:monitorResultFilter,q:monitorResultQuery,page:monitorResultPage};
    const url='/api/monitor-comments?'+Object.entries(params).map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');
    const response=await fetch(url),value=await response.json();
    if(sequence!==monitorHistorySequence||key!==historyKey()||page!=='monitor')return;
    if(!response.ok||!Array.isArray(value.rows)||!value.counts)throw Error('评论历史暂时无法加载');
    monitorResultPage=value.page;monitorHistory=value;monitorHistoryKey=historyKey();monitorHistoryError='';
    rememberMonitorHistory(monitorHistoryCache,monitorHistoryKey,value);
    rememberMonitorHistory(monitorHistoryCounts,historyScopeKey(),{counts:value.counts,valuable_count:value.valuable_count});
    redrawMonitorResults();
  }catch(error){if(sequence===monitorHistorySequence&&key===historyKey()&&page==='monitor'){monitorHistoryError='评论历史暂时无法加载，请重试';monitorHistoryErrorKey=key;redrawMonitorResults();}}
}
function changeMonitorSelection(url){monitorResultVideo=url;monitorResultPage=1;monitorHistoryError='';redrawMonitorResults();redrawWorkPool();return refreshMonitorHistory();}
function windowLabel(hours){return hours%24===0?`最近 ${hours/24} 天`:`最近 ${hours} 小时`;}
function taskTimeWindow(t){const time=t.comment_since?`时间范围：${esc(date(t.comment_since))} 起，截止各评论观察时刻。过期 ${t.filtered_old||0} · 时间未知 ${t.filtered_unknown||0} · 未来异常 ${t.filtered_future||0}。`:'';const words=t.include_keywords||t.exclude_keywords?`<br>本批评论关键词：${esc(t.include_keywords?.split('\n').join('、')||'不限')}；屏蔽词：${esc(t.exclude_keywords?.split('\n').join('、')||'无')}。关键词未命中 ${t.filtered_keyword||0} · 屏蔽词排除 ${t.filtered_blocked||0}。`:'';return time||words?`<p class="monitor-window">${time}${words}<br>被过滤评论仍计入观察预算，不进入本批入库数；历史记录不删除。</p>`:'';}
// Presentation only: every row comes from a server observation, never sample leads.
const valuableComment=r=>!r.filter_reason&&r.category==='buyer'&&['rules','model','human'].includes(r.analysis_method);
const rejectionLabels={filtered_old:'超出时间范围',filtered_unknown:'发布时间未知',filtered_future:'未来时间异常',filtered_keyword:'未命中关键词',filtered_blocked:'命中屏蔽词'};
function monitorResults(){
  if(monitorHistory&&monitorHistoryKey===historyKey())return monitorHistory;
  const cached=monitorHistoryCache.get(historyKey());if(cached)return {...cached,...monitorHistoryCounts.get(historyScopeKey()),from_cache:true};
  if(monitorHistory||mode==='live')return {rows:[],counts:{observed:null,accepted:null,filtered:null},valuable_count:null,
    ...monitorHistoryCounts.get(historyScopeKey()),scope:'all_local_comment_history',page:monitorResultPage,
    pages:Math.max(1,monitorResultPage),total:null,loading:true};
  const result=S.collector?.results||{task:null,rows:[]};
  const rows=result.rows.filter(r=>String(r.text||'').trim()),accepted=rows.filter(r=>!r.filter_reason).length;
  return {...result,rows,counts:{observed:rows.length,accepted,filtered:rows.length-accepted}};
}
function monitorResultRows(){const result=monitorResults();if(result.scope==='all_local_comment_history')return result.rows;return result.rows.filter(r=>(!monitorResultVideo||r.video_url===monitorResultVideo)&&(monitorResultFilter==='all'||(monitorResultFilter==='valuable'?valuableComment(r):monitorResultFilter==='filtered'?!!r.filter_reason:!r.filter_reason))&&(!monitorResultQuery||[r.text,r.nickname,r.user_identifier,r.external_id].join(' ').toLocaleLowerCase().includes(monitorResultQuery.toLocaleLowerCase()))).sort((a,b)=>(timeValue(b.collected_at||b.first_seen_at||b.observed_at)||0)-(timeValue(a.collected_at||a.first_seen_at||a.observed_at)||0));}
function markedComment(row){
  // Literal highlighting; escape source text and matched terms before HTML insertion.
  const text=String(row.text||''),terms=[...(row.exclude_matches||[]),...(row.include_matches||[])].filter(Boolean).sort((a,b)=>b.length-a.length);
  if(!terms.length)return esc(text);
  const lower=text.toLowerCase();let html='',at=0;
  while(at<text.length){const term=terms.find(t=>lower.startsWith(t.toLowerCase(),at));if(term){html+=`<mark>${esc(text.slice(at,at+term.length))}</mark>`;at+=term.length;}else{html+=esc(text[at]);at++;}}
  return html;
}
function analysisCaption(row){
  const state=row.analysis_state||row.analysis_method||'missing';
  const names={pending:'等待规则初筛',rules:'规则初筛',model:'模型分析完成',human:'人工确认',
    queued:'模型排队中 · 暂用规则',running:'模型分析中 · 暂用规则',cancelling:'模型取消中 · 暂用规则',
    failed:'模型失败 · 保留规则',cancelled:'模型已取消 · 保留规则',interrupted:'模型中断 · 保留规则',
    stale:'版本已变化 · 保留规则',skipped:'未自动分析 · 保留规则',
    snapshot_changed:'原文已变化 · 需核对',missing:'尚未关联判断'};
  const categoryLabel=row.category&&state!=='pending'?`${labels[row.category]||'待判断'} · `:'';
  return categoryLabel+(names[state]||'分析状态待核对');
}
function analysisReason(row){
  if(row.filter_reason||['missing','snapshot_changed','pending'].includes(row.analysis_state))return '';
  const label={model:'模型理由',rules:'规则依据',human:'人工理由'}[row.analysis_method];
  if(!label)return '';
  const reason=row.analysis_reason||'这条历史结果未保存判断理由';
  const quotes=row.analysis_method==='model'?[...new Set((row.analysis_evidence||[]).filter(e=>e.kind==='category'&&e.source==='comment'&&e.text&&String(row.text||'').includes(e.text)).map(e=>e.text))]:[];
  return `<div class="analysis-reason"><p><strong>${label}：</strong>${esc(reason)}</p>${quotes.length?`<p class="analysis-quote"><strong>原文依据：</strong>${quotes.map(q=>`<q>${esc(q)}</q>`).join('、')}</p>`:''}</div>`;
}
const durationText=v=>typeof v!=='number'||!Number.isFinite(v)?'未知':v<60?`${Math.round(v)} 秒`:v<3600?`${Math.round(v/60)} 分钟`:`${Math.round(v/3600)} 小时`;
function commentTiming(row){
  if(row.filter_reason||!row.timing)return '';
  const t=row.timing;
  return `<small>发布至首次采集 ${esc(durationText(t.collection_seconds))}</small><small>模型处理 ${esc(durationText(t.model_seconds))}</small>`;
}
function timelinessSummary(result){
  const t=result.timeliness;if(!t)return '';
  return `<div class="monitor-timeliness" role="status"><strong>采集目标：发布后 ${durationText(t.target_seconds||60)} 内首次入库</strong><span>${t.measured?`${t.within_target} / ${t.measured} 条新评论达标 · P50 ${durationText(t.p50_seconds)} · P95 ${durationText(t.p95_seconds)}`:'暂无可测新评论样本'}</span><small>按当前目标评估最近批次；模型耗时单列。</small></div>`;
}
function monitorResultsPanel(){
  const result=monitorResults(),remote=result.scope==='all_local_comment_history',all=monitorResultRows(),scopeRows=result.rows.filter(r=>!monitorResultVideo||r.video_url===monitorResultVideo),counts=remote?result.counts:{observed:scopeRows.length,accepted:scopeRows.filter(r=>!r.filter_reason).length,filtered:scopeRows.filter(r=>r.filter_reason).length},pages=remote?result.pages:Math.max(1,Math.ceil(all.length/25));
  monitorResultPage=Math.min(pages,Math.max(1,monitorResultPage));
  const rows=remote?all:all.slice((monitorResultPage-1)*25,monitorResultPage*25),legacy=result.rows.some(r=>r.text_origin!=='observation');
  const countLabel=value=>Number.isFinite(value)?fmt(value):'—',error=monitorHistoryErrorKey===historyKey()?monitorHistoryError:'';
  const tabs=[['valuable','有意向',remote?result.valuable_count:scopeRows.filter(valuableComment).length],['accepted','采集通过',counts.accepted],['all','全部观察',counts.observed],['filtered','已过滤',counts.filtered]];
  return `<section id="monitor-result-panel" class="card section-gap comment-results" aria-label="评论监控结果"><div class="card-head"><div><h2>${monitorResultFilter==='valuable'?'有意向的评论':monitorResultFilter==='accepted'?'采集通过的评论':monitorResultFilter==='filtered'?'已过滤评论':monitorResultVideo?'作品评论':'全部评论'}</h2><small>${error?'评论历史加载失败':result.loading?'正在加载评论…':result.from_cache?'正在更新 · 已显示上次结果':'按首次采集时间倒序 · 同一评论合并展示'}</small></div><span class="result-total">${countLabel(counts.observed)} <small>条观察</small></span></div>
    ${error?`<div class="work-selection" role="alert">${esc(error)} ${button('重试','monitor-history-retry','small')}</div>`:''}${monitorResultVideo?`<div class="work-selection">${icon('filter')} <span>${esc(workBoard().rows.find(r=>r.url===monitorResultVideo)?.title||'已选作品')}</span>${button('全部评论','work-clear','small')}</div>`:''}<div class="result-toolbar"><div class="result-tabs" role="group" aria-label="按筛选结果查看">${tabs.map(([key,label,count])=>button(`${label} <b>${countLabel(count)}</b>`,'monitor-result-filter',`result-tab ${monitorResultFilter===key?'selected':''}`,`data-filter="${key}" aria-pressed="${monitorResultFilter===key}"`)).join('')}</div><label class="result-search">${icon('search')}<input id="monitor-result-search" type="search" aria-label="搜索评论、用户或标识" placeholder="搜索评论 / 用户" value="${esc(monitorResultQuery)}"></label></div>
    ${legacy?'<p class="result-legacy">包含已保存的历史评论；采集快照保留当时原文。</p>':''}
    <p class="filter-explanation">有意向：采集通过且被判定有客户需求；采集通过：通过当时的采集筛选，尚不代表有意向。</p><div class="result-table-scroll" tabindex="0" aria-label="评论结果" aria-busy="${!!result.loading&&!error}"><table class="result-table"><thead><tr><th scope="col" title="平台账号 ID，用于区分同名用户并关联评论；与用户设置的抖音号不同">昵称 / 用户 ID</th><th scope="col">评论原文 / 当前判断</th><th scope="col">评论发布时间</th><th scope="col" aria-sort="descending">首次采集时间 ↓</th><th scope="col">筛选结果</th></tr></thead><tbody>${rows.length?rows.map(r=>`<tr class="${r.filter_reason?'result-filtered':''}"><td><strong>${esc(r.nickname||'未提供昵称')}</strong><span class="cell-sub mono">${r.user_identifier?'ID：'+esc(r.user_identifier):'用户 ID 未获取'}</span></td><td><p class="result-comment">${markedComment(r)}</p>${!r.filter_reason?`<p class="result-analysis">${esc(analysisCaption(r))}</p>${analysisReason(r)}`:''}<div class="result-source">${r.text_origin==='archive'?'<span>历史存档</span>':''}<a href="${esc(r.video_url)}" target="_blank" rel="noopener noreferrer">原视频 ↗</a><span>评论 ${esc(r.external_id)}</span></div></td><td class="result-time published-time">${commentDate(r.published_at)}</td><td class="result-time collected-time">${commentDate(r.collected_at||r.first_seen_at||r.observed_at)}${commentTiming(r)}</td><td>${badge(r.filter_reason?(rejectionLabels[r.filter_reason]||'已过滤'):'采集通过',r.filter_reason==='filtered_blocked'?'warn':r.filter_reason?'':'good')}${[...(r.exclude_matches||[]),...(r.include_matches||[])].length?`<small class="cell-sub">命中：${esc([...(r.exclude_matches||[]),...(r.include_matches||[])].join('、'))}</small>`:''}</td></tr>`).join(''):`<tr><td colspan="5" class="result-empty">${error?'评论未能加载，请重试。':result.loading?'<span class="result-loading" role="status"><span class="result-loading-dot" aria-hidden="true"></span>正在加载评论…</span>':monitorResultFilter==='valuable'?'暂无有意向的评论，可切换“采集通过”或“全部观察”查看其他内容。':result.rows.length?'没有符合当前查看条件的评论。':'暂无可展示的评论；采集后会在这里显示。'}</td></tr>`}</tbody></table></div>
    <div class="result-footer"><small>${result.loading?(error?'评论加载失败':'正在加载筛选结果'):`${fmt(remote?result.total:all.length)} 条符合查看条件`} · 北京时间 · 意向评论按当前判断筛选，人工核对优先</small><div class="actions">${button('上一页','monitor-result-page','small',`data-step="-1" ${result.loading||monitorResultPage===1?'disabled':''}`)}<span>${monitorResultPage} / ${result.loading?'—':pages}</span>${button('下一页','monitor-result-page','small',`data-step="1" ${result.loading||monitorResultPage===pages?'disabled':''}`)}</div></div></section>`;
}
function redrawMonitorResults(){const panel=$('#monitor-result-panel');if(!panel)return;const scroll=$('.result-table-scroll'),top=scroll?.scrollTop||0,left=scroll?.scrollLeft||0;panel.outerHTML=monitorResultsPanel();const next=$('.result-table-scroll');if(next){next.scrollTop=top;next.scrollLeft=left;}icons();}
function monitorControls(){
  const m=monitorConfig(),active=m.enabled||m.active_task_id,disabled=mode!=='live',b=m.last_batch;
  const title=m.stopping?'关闭中':m.status==='attention'?'已暂停 · 需要处理':m.enabled?'已开启':m.active_task_id?'等待当前任务关闭':'已关闭';
  return `<section id="monitor-controls" class="card section-gap monitor-controls monitor-compact" aria-label="评论监控开关"><div class="card-head"><div><h2>评论监控</h2><small>${esc(discoveryState().configured?'作品库与作者池':m.target)} · ${windowLabel(m.lookback_hours)} · 采集目标 ${durationText(m.freshness_target_seconds||60)} · 检查间隔 ${m.interval_seconds} 秒</small></div><div class="actions">${badge(title,m.enabled?'good':m.status==='attention'?'warn':'')}${button(m.stopping?'正在关闭…':active?'关闭监控':'开启监控',active?'monitor-stop':'monitor-start',active?'small':'small primary',disabled||m.stopping?'disabled':'')}</div></div><div class="monitor-status-line"><span role="status">${esc(m.detail||'监控未开启')}</span>${m.next_run_at?`<small>下批：${date(m.next_run_at)}</small>`:''}</div></section>`;
}
function markMonitorDraft(target){
  if(target.id==='live-search'){liveSearch=target.value;livePage=1;liveArchiveAnchor=null;redrawLiveResults();void refreshLiveArchive();}
  if(target.id==='live-filter'){liveFilter=target.value;livePage=1;liveArchiveAnchor=null;redrawLiveResults();void refreshLiveArchive();}
  if(target.id==='live-scope'&&liveScope!==target.value){liveScope=target.value;const refresh=$('[data-action="live-archive-refresh"]');if(refresh)refresh.disabled=liveScope==='current';livePage=1;liveArchiveAnchor=null;liveArchiveKey='';liveArchiveSequence++;redrawLiveResults();void refreshLiveArchive();}
  if(target.closest?.('#semantic-form')){semanticDraftDirty=true;const label=$('#semantic-draft-status');if(label)label.textContent='有未保存的修改';}
  if(target.closest?.('#live-form')){liveDraftDirty=true;const label=$('#live-draft-status');if(label)label.textContent='有未保存的修改';document.querySelectorAll('[data-action="live-start"],[data-action="live-track-start"]').forEach(start=>{start.disabled=true;});}
  if(!target.closest?.('#monitor-form'))return;
  monitorDraftDirty=true;syncFreshnessHint();
  const status=$('#monitor-draft-status');if(status)status.textContent='有未保存的修改';
}

function liveState(){return S?.collector?.live_monitor||{config:{room_url:'',duration_seconds:60,max_messages:100,include_keywords:'',exclude_keywords:''},sessions:[],current:null,rows:[],events:[],active_id:null,transport:'browser_websocket',records_limit:500};}
const liveNames={connecting:'连接中',running:'正在观察',reconnected:'已重连 · 有缺口',disconnected:'连接中断',completed:'本次完成',cancelled:'已停止',interrupted:'已中断',needs_login:'等待登录',needs_verification:'等待验证',ended:'已下播',no_data:'未取得弹幕',dependency_missing:'缺少运行依赖',rate_limited:'访问受限',access_denied:'拒绝访问',resource_limited:'资源上限',schema_changed:'解析异常',room_changed:'房间改变',failed:'失败',stopping:'正在停止'};
function liveStatus(){
  const s=liveState(),r=s.current,active=!!s.active_id;
  return `<section id="live-status" class="card section-gap"><div class="card-head"><h2>当前直播会话</h2>${badge(r?liveNames[r.status]||r.status:'未开启',active?'good':'warn')}</div><div class="card-body"><p>${esc(r?.detail||'保存配置后手动开启，只读取指定房间当前可观察的弹幕。')}</p><p>实际方式：${r?.config?.interactive?'可见专用浏览器':'后台浏览器 · 不弹窗'} · 接收直播数据 · 可查看本地存档，不能回看未采集的历史弹幕</p>${r?`<p>房间网页：<a href="${esc(r.room_url)}" target="_blank" rel="noopener noreferrer">${esc(r.room_url)}</a><br>平台房间 ID：${esc(r.room_id||'尚未核对')} · 开始 ${date(r.started_at)} · 结束 ${date(r.finished_at)}</p><div class="collection-counts"><span>观察 <b>${r.observed}</b></span><span>保存 <b>${r.inserted}</b></span><span>过滤 <b>${r.filtered}</b></span><span>重复 <b>${r.duplicate}</b></span><span>无效 <b>${r.invalid}</b></span><span>断线记录 <b>${r.gaps}</b></span></div>`:''}<div class="actions">${active?button('停止直播监控','live-stop','small',`data-id="${s.active_id}" ${r?.status==='stopping'?'disabled':''}`):button('开启本次直播监控','live-start','primary',mode!=='live'||!s.config.room_url||liveDraftDirty||s.tracking?.enabled?'disabled':'')}</div><small>达到读取时长或消息预算会自动结束；关闭只影响本次直播，不停止评论采集。重启服务不自动恢复。</small></div></section>`;
}
function liveTrackingPanel(){
  const s=liveState(),t=s.tracking||{},locked=mode!=='live'||!s.config.room_url||liveDraftDirty;
  return `<section class="card section-gap" id="live-tracking"><div class="card-head"><h2>后台持续跟踪</h2>${badge(t.enabled?'已开启':t.status==='attention'?'需要处理':t.status==='ended'?'直播已结束':'未开启',t.enabled?'good':'warn')}</div><div class="card-body"><p>${esc(t.detail||'按保存配置持续观察一个房间，遇到异常暂停。')}</p><p>已启动 ${fmt(t.run_count||0)} 批${t.next_run_at?` · 下次 ${date(t.next_run_at)}`:''}</p><div class="actions">${t.enabled?button('关闭持续跟踪','live-track-stop','',`data-id="${t.id}"`):button('开启持续跟踪','live-track-start','primary',locked||s.active_id?'disabled':'')}</div><small>后台浏览器 · 不弹窗。正常批次之间按间隔重连，存在观察缺口；下播、未收到有效直播数据或访问异常会停止。服务重启后保持关闭。</small></div></section>`;
}
function liveDiscoveryPanel(){
  const d=liveState().discovery||{},rows=d.rows||[];
  return folded('live-discovery','发现无畏契约直播间',`<p>${esc(d.detail||'从公开分类获取候选房间，填入配置后再开始监控。')}</p><div class="actions">${button('发现直播间','live-discover','small',mode!=='live'?'disabled':'')}<small>HTTPS 读取公开分类 · 一分钟内复用结果</small></div>${d.fetched_at?`<p class="muted">共 ${rows.length} 个候选 · 采集于 ${date(d.fetched_at)}${d.status==='failed'?' · 本次失败，以下为上次结果':''}</p>`:''}${rows.map((r,index)=>{const o=d.observations?.[r.room_url];return `<div class="history-item"><strong>${esc(r.title)}</strong><small>${esc(r.room_url)}${r.popularity_text?' · 平台热度原文 '+esc(r.popularity_text):''}</small><small>${o?`最近测试 #${o.id} · ${esc(liveNames[o.status]||o.status)} · 保存 ${fmt(o.inserted)} 条 · ${date(o.finished_at||o.started_at)}`:'尚未测试'}</small>${button('填入配置','live-use-room','small',`data-index="${index}"`)}</div>`;}).join('')}`);
}
function liveArchiveRequestKey(){return JSON.stringify([mode,liveScope,liveSearch,liveFilter,livePage]);}
async function refreshLiveArchive(force=false,background=false){
  if(page!=='live'||liveScope==='current')return;
  const key=liveArchiveRequestKey();if(!force&&key===liveArchiveKey)return;
  const keepRows=background&&key===liveArchiveKey&&!!liveArchive,modelKey=JSON.stringify(S?.collector?.model_queue||null);
  const sequence=++liveArchiveSequence;liveArchiveKey=key;if(!keepRows)liveArchive=null;liveArchiveError='';if(!keepRows)redrawLiveResults();
  const args=new URLSearchParams({mode,session_id:liveScope,q:liveSearch,filter:liveFilter,offset:String((livePage-1)*25),limit:'25'});
  if(liveArchiveAnchor!==null)args.set('anchor_id',String(liveArchiveAnchor));
  try{const response=await fetch(`/api/live-history?${args}`);const data=await response.json();if(sequence!==liveArchiveSequence||key!==liveArchiveRequestKey()||page!=='live')return;if(!response.ok)throw Error(data.error||'读取失败');liveArchive=data;liveArchiveAnchor=data.anchor_id;liveArchiveModelKey=modelKey;}
  catch{if(sequence!==liveArchiveSequence||key!==liveArchiveRequestKey())return;liveArchiveError=keepRows?'分析状态更新失败，暂时显示上次结果；稍后自动重试':'本地弹幕存档暂时读取失败，请刷新重试';if(!keepRows)liveArchiveKey='';}
  if(sequence===liveArchiveSequence&&page==='live')redrawLiveResults();
}
function liveRows(){return liveState().rows.filter(r=>(liveFilter==='all'||(liveFilter==='valuable'?valuableComment(r):liveFilter==='filtered'?!!r.filter_reason:!r.filter_reason))&&(!liveSearch||[r.raw_text,r.nickname,r.uid,r.message_id].join(' ').toLocaleLowerCase().includes(liveSearch.toLocaleLowerCase())));}
function liveResults(){
  const remote=liveScope!=='current';if(remote&&(!liveArchive||liveArchiveKey!==liveArchiveRequestKey()))return `<div id="live-results" class="card-body" role="status"><p>${esc(liveArchiveError||'正在读取本地弹幕存档…')}</p>${liveArchiveError?button('重试','live-archive-refresh','small'):''}</div>`;
  const rows=remote?liveArchive.rows:liveRows(),total=remote?liveArchive.total:rows.length,pages=Math.max(1,Math.ceil(total/25));if(!remote)livePage=Math.max(1,Math.min(livePage,pages));
  return `<div id="live-results">${remote&&liveArchiveError?notice(esc(liveArchiveError),true):''}${rows.length?`<div class="table-scroll"><table><thead><tr><th>发布时间</th><th>首次采集时间</th><th>用户</th><th>弹幕原文</th><th>筛选结果</th></tr></thead><tbody>${(remote?rows:rows.slice((livePage-1)*25,livePage*25)).map(r=>`<tr><td><span>${commentDate(r.published_at)}</span></td><td>${commentDate(r.observed_at)}</td><td><strong>${esc(r.nickname||'昵称未知')}</strong><small class="cell-sub">UID ${esc(r.uid||'未知')}</small><small class="cell-sub">消息 ${esc(r.message_id||'未知 · 无法按 ID 去重')}</small></td><td class="live-result-text">${esc(r.raw_text)}</td><td>${r.filter_reason?badge(rejectionLabels[r.filter_reason]||r.filter_reason,'warn'):category(r.category)}<small class="cell-sub">${esc(r.reason)}</small>${r.include_matches?.length?`<small class="cell-sub">命中：${esc(r.include_matches.join('、'))}</small>`:''}${r.exclude_matches?.length?`<small class="cell-sub">屏蔽：${esc(r.exclude_matches.join('、'))}</small>`:''}${['rules','human','model'].includes(r.analysis_method)?ruleEvidence(r):''}<div class="actions">${button('核对需求','live-review','small',`data-id="${r.id}"`)}${r.lead_id?button('查看线索','live-open-lead','small',`data-id="${r.lead_id}"`):'<small>尚未关联用户线索</small>'}</div></td></tr>`).join('')}</tbody></table></div>`:empty('尚无符合当前筛选的弹幕',liveFilter==='valuable'?'当前默认显示已接收的潜在客户需求，可切换查看全部已保存弹幕。':'只有实际读到的文字会出现在这里；没有数据不代表直播间没有消息。','','','radio')}<div class="card-foot row spread"><small>${remote?'本地已采集存档 · '+total+' 条':'当前会话最近 '+(liveState().records_limit||500)+' 条以内 · 筛选后 '+rows.length+' 条'} · 第 ${livePage}/${pages} 页</small><div class="actions">${button('上一页','live-prev','small',livePage===1?'disabled':'')}${button('下一页','live-next','small',livePage===pages?'disabled':'')}</div></div></div>`;
}
function redrawLiveResults(){const root=$('#live-results');if(!root)return;const opened=openPanelIds();root.outerHTML=liveResults();restorePanelIds(opened);icons();}
function liveMonitor(){
  const s=liveState(),c=s.config;
  return head('直播弹幕','',button(icon('sliders-horizontal')+' 直播设置','live-settings','small'))+`<div class="live-workspace"><div class="monitor-results"><section class="card"><div class="card-head"><h2>实时弹幕与需求初筛</h2>${badge(S.semantic?.config?.live_model_enabled===false?'仅规则初筛 · 模型已关闭':S.semantic?.can_analyze?(S.semantic.config?.auto_analyze?'规则初筛 · 自动模型分析':'规则自动初筛 · 单条模型可用'):'规则模式 · 非语义模型')}</div><div class="toolbar"><select id="live-scope" aria-label="弹幕查看范围">${opts({current:'当前会话',all:'全部本地存档',...Object.fromEntries(s.sessions.map(r=>[r.id,'会话 #'+r.id+' · '+date(r.started_at)]))},liveScope)}</select>${button('刷新存档','live-archive-refresh','small',liveScope==='current'?'disabled':'')}<input id="live-search" type="search" aria-label="搜索弹幕或用户" placeholder="搜索原文、用户或消息 ID" value="${esc(liveSearch)}"><select id="live-filter" aria-label="弹幕处理状态">${opts({valuable:'有意向',accepted:'采集通过',all:'全部已保存',filtered:'被过滤'},liveFilter)}</select></div>${liveResults()}</section>${folded('live-monitor-controls','监控状态与操作',liveTrackingPanel()+liveStatus()+liveDiscoveryPanel())}${folded('live-events','连接事件与数据缺口',s.events.length?s.events.map(e=>`<p>${date(e.observed_at)} · ${esc(e.detail)}</p>`).join(''):'<p>尚无连接事件</p>')}<p class="muted">弹幕原文与评论分开保存。有 UID 且通过过滤的记录自动进入内部线索；核对不授予联系权限，不创建发送任务。</p></div></div>`;
}
function liveSettingsPanel(){const c=liveState().config;return `<aside class="card monitor-settings"><div class="card-head"><h2>直播监控配置</h2></div><form id="live-form"><fieldset class="monitor-fields" ${mode!=='live'?'disabled':''}>${field('room_url','完整直播间链接 / 网页房间 ID',c.room_url,'text','required maxlength="200" placeholder="https://live.douyin.com/…"')}${field('duration_seconds','本次读取时长 / 秒',c.duration_seconds,'number','required min="10" max="3600"')}${field('max_messages','本次文字消息观察预算',c.max_messages,'number','required min="1" max="5000"')}${field('interval_seconds','持续跟踪批次间隔 / 秒',c.interval_seconds??30,'number','required min="30" max="3600"')}${area('include_keywords','弹幕关键词（任一匹配）',c.include_keywords,'maxlength="4000" placeholder="留空不过滤"')}${area('exclude_keywords','屏蔽词（优先排除）',c.exclude_keywords,'maxlength="4000"')}<p id="live-draft-status" class="muted">保存配置不会开始读取</p><p class="form-error notice warn" role="alert" hidden></p>${submit('保存直播配置')}</fieldset><p class="muted" style="padding:0 20px 20px">本次参数在启动时固定。直播时段与评论回看范围分开，断线不会自动补齐。</p></form></aside>`;}
function liveSettingsDialog(){showSettingsModal('直播监控设置',liveSettingsPanel());}

function monitorSettingsPanel(){
  const m=monitorConfig(),locked=mode!=='live'||m.enabled||!!m.active_task_id,days=m.lookback_hours%24===0;
  return `<aside id="monitor-settings" class="card monitor-settings" aria-label="监控配置"><div class="card-head"><div><h2>监控配置</h2><small>保存后用于下一批监控</small></div>${icon('sliders-horizontal')}</div><form id="monitor-form"><fieldset class="monitor-fields" ${locked?'disabled':''} aria-label="监控参数">
    <div class="freshness-controls"><div><h3>采集时效目标</h3><p class="muted">评论发布时间 → 首次采集入库；模型分析耗时单独计算。</p></div><div class="freshness-presets" role="group" aria-label="采集目标快捷选择">${[30,60,120,300].map(v=>button(durationText(v),'freshness-preset',`small ${Number(m.freshness_target_seconds||60)===v?'selected':''}`,`type="button" data-seconds="${v}"`)).join('')}</div>${field('freshness_target_seconds','目标时间 / 秒（可自定义）',m.freshness_target_seconds||60,'number','required min="10" max="3600" step="1"')}<p id="freshness-hint" class="monitor-hint" role="status"></p></div>
    <div class="fields-2">${field('page_concurrency','采集并发 / 视频页',m.page_concurrency,'number','required min="1" max="4" step="1"')}${field('interval_seconds','批次检查间隔 / 秒',m.interval_seconds,'number','required min="30" max="86400" step="1"')}</div>
    ${transportField(m.transport||'local_browser')}
    <p class="monitor-hint">并发 1–4，不超过本批视频数。最低 30 秒；间隔从上一批结束后计算，分析会在采集中逐条开始。</p>
    ${area('include_keywords','评论关键词 · 命中任一词',m.include_keywords||'','rows="3" maxlength="4000" placeholder="如：找陪玩，陪练，多少钱；留空则不限"')}
    ${area('exclude_keywords','屏蔽词 · 命中即排除',m.exclude_keywords||'','rows="3" maxlength="4000" placeholder="如：接单，招募，免费；留空则不屏蔽"')}
    <p class="monitor-hint">逗号、分号或换行分隔；不区分英文字母大小写。只匹配评论原文，屏蔽词优先，不直接认定付费意向。</p>
    <details class="monitor-more" open><summary>监控对象与时间</summary>
      ${select('kind','监控对象',opts({search:'关键词相关视频',video:'指定视频',author:'从作品作者发现'},m.kind))}
      ${collectionTargetField(m.target,'发现视频的搜索词 / 视频链接')}
      <div class="fields-2">${field('window_value','最近时间范围',days?m.lookback_hours/24:m.lookback_hours,'number','required min="1" max="8760" step="1"')}${select('window_unit','时间单位',opts({days:'天',hours:'小时'},days?'days':'hours'))}</div>
      <small>默认最近 1 小时，最多 365 天；筛选评论发布时间。旧视频也可能产生新评论。</small>
    </details>
    <details class="monitor-more"><summary>每批读取上限</summary><div class="fields-2">${field('video_limit','发现后作品上限',m.video_limit,'number','required min="1" max="5" step="1"')}${field('comment_limit','每视频观察评论数',m.comment_limit,'number','required min="1" max="100" step="1"')}</div><small>被过滤的评论仍消耗观察预算，不保证读完时间范围内的全部评论。</small></details>
  </fieldset><div class="monitor-save"><p id="monitor-draft-status" role="status">${locked?'先关闭监控并等待任务停止，才能修改配置。':'保存不会开启监控；不会改动历史记录。'}</p><p class="form-error" role="alert" hidden></p><div class="actions">${button('还原未保存修改','monitor-reset','small',`type="button" ${locked?'disabled':''}`)}<button class="button primary small" type="submit" ${locked?'disabled':''}>保存配置</button></div></div></form></aside>`;
}
function monitorStartDialog(){if(monitorDraftDirty)throw Error('请先保存监控设置，再开启监控');const m=monitorConfig();if(mode!=='live')throw Error('演示区不能开启真实监控');showModal('开启评论监控',`<p>监控目标：${esc(m.target)}</p><p>评论发布时间：${windowLabel(m.lookback_hours)}</p>`+notice((m.transport==='http'?'通过后端 HTTP':'通过专用 Chrome')+'按批读取，直到关闭监控；验证、限制或失败时暂停，重启后保持关闭。需先完成相同通道的手动评论读取。不会发送私信。'),'monitor-start-form',submit('确认开启监控'));}
const verificationLabels={detected:'检测到验证',capturing:'正在提取图片',recognizing:'正在识别',submitting:'正在提交',verifying:'正在检查结果',accepted:'已确认恢复读取',needs_review:'等待处理'};
const verificationReasons={http_challenge_not_available:'HTTP 响应未提供验证码图片和提交信息',manual_mode:'自动处理未启用',batch_attempt_limit:'本批自动尝试已用完',unsupported_challenge_dom:'验证页面结构尚未适配',unsupported_type:'当前验证码类型不支持',image_pixels_unavailable:'无法读取验证码图片',transparent_template_requires_mask_aware_model:'透明拼图尚不支持',acceptance_not_observed:'尚未确认采集接口恢复',challenge_changed:'验证码已变化',dependency_missing:'识别依赖未就绪',solver_timeout:'识别超时',ambiguous_match:'识别结果存在歧义',weak_match:'匹配不足',unsupported_geometry:'页面坐标不符合适配条件'};
Object.assign(verificationReasons,{frame_ambiguous:'发现多个验证窗口，无法确定当前题目',frame_not_ready:'验证窗口尚未加载完成',prompt_not_ready:'验证说明尚未加载完成',background_missing:'未找到验证码背景图',background_hidden:'验证码背景图尚未显示',background_ambiguous:'验证码背景图不唯一',target_missing:'未找到拼图小块',target_hidden:'拼图小块尚未显示',target_ambiguous:'拼图小块不唯一',handle_missing:'未找到可拖动的滑块',handle_hidden:'滑块尚未显示',handle_ambiguous:'可拖动的滑块不唯一',image_not_ready:'验证码图片尚未加载完成',image_element_unsupported:'验证码图片格式尚未适配',image_size_unsupported:'验证码图片超出处理范围',invalid_geometry:'无法确定验证码在页面中的位置'});
verificationReasons.click_solver_unavailable='已识别为点选验证码，点选识别尚未接入';
Object.assign(verificationReasons,{point_prompt_or_image_unsupported:'点选题目要求或图片暂不支持',point_background_unsupported:'图片背景或颜色超出当前识别范围',point_segmentation_ambiguous:'图片中的物体未能可靠分开',point_object_count_unsupported:'未能识别出适用数量的独立物体',point_weak_shape_match:'相同形状的匹配不足，未自动点击',point_ambiguous_pair:'存在多个相似配对，未自动点击',point_unstable_segmentation:'物体轮廓识别不稳定，未自动点击',point_character_uncertain:'字符识别不确定，未自动点击',point_character_mismatch:'形状与字符识别不一致，未自动点击',point_selection_changed:'点选过程中题目变化，已停止后续点击'});
Object.assign(verificationReasons,{point_selection_already_present:'页面已有选中标记，未追加自动点选',point_selection_not_registered:'页面未确认选中，已停止后续操作',point_confirmation_unavailable:'已选中目标，但未找到可用确认按钮'});
Object.assign(verificationReasons,{platform_verification_failed:'抖音判定验证未通过，已暂停，不连续重试',platform_verdict_unobserved:'尚未取得抖音明确通过判定，已暂停，不计为通过'});
verificationReasons.sample_archive_unavailable='样本归档未完成，已暂停，已有记录保留';
function verificationDetail(v){return (v.phase==='accepted'&&v.platform_verdict==='passed'?'抖音判定通过，读取已恢复':verificationLabels[v.phase]||v.phase)+(v.reason?' · '+(verificationReasons[v.reason]||'自动处理未完成，需检查诊断'):'');}
function verificationPanel(t){const v=t.verification;if(!v)return '';return `<p class="collection-detail" role="status">验证码：${esc(verificationDetail(v))} · 提交尝试 ${t.verification_counts?.attempted||0} · 确认恢复 ${t.verification_counts?.recovered||0}</p>`;}
function verificationEvidence(d){const v=d.snapshot.verification;return `<div class="history-item"><strong>验证码处理 · ${esc(verificationDetail(v))}</strong><small>${date(d.created_at)} · ${v.transport==='http'?'HTTP':'浏览器'} · ${v.elapsed_ms||0} 毫秒</small><small>提交尝试 ${v.submissions||0} · ${esc(v.attempt_id||'')}</small></div>`;}
function collectionTask(t){const c=collectionState();const waiting=['needs_login','needs_verification','needs_interaction'].includes(t.status);return `<article class="collection-task"><div class="row spread"><div><strong>#${t.id} · ${({search:'关键词发现',video:'指定视频',author:'作者作品发现'})[t.kind]||'未知方式'} · ${t.transport==='http'?'HTTP':'浏览器'}</strong><p class="collection-target">${esc(t.target)}</p></div>${badge(collectionLabels[t.status]||t.status,t.status==='completed'?'good':waiting||['failed','rate_limited','access_denied'].includes(t.status)?'warn':'')}</div><p class="collection-detail" role="status">${esc(t.detail)}</p>${verificationPanel(t)}<details id='task-metrics-${t.id}' class='task-metrics'><summary>统计与视频进度</summary><div class="collection-counts"><span>观察视频 <b>${t.videos}</b></span><span>评论 <b>${t.comments}</b></span><span>新增 <b>${t.inserted}</b></span><span>重复 <b>${t.duplicate}</b></span><span>修订 <b>${t.revised}</b></span><span>跳过 <b>${t.skipped}</b></span><span>读取页 <b>${t.active_pages||0} / ${t.page_concurrency||1}</b></span><span>并发峰值 <b>${t.peak_pages||'未记录'}</b></span></div>${taskTimeWindow(t)}${checkpointPanel(t)}</details>${collectorHelp(t)}<div class="row spread"><small>${date(t.updated_at)} · 本批上限 ${t.video_limit} 视频 / 每视频 ${t.comment_limit} 评论${!t.active&&waiting?(t.transport==='http'?' · 请准备会话后新建任务':' · 浏览器会话已关闭'):''}</small><div class="actions">${button('来源证据','collector-evidence','small',`data-id="${t.id}"`)}${t.active?(waiting?button('已处理，继续读取','collector-resume','small primary',`data-id="${t.id}"`):'')+button('停止','collector-cancel','small',`data-id="${t.id}" ${t.status==='cancelling'?'disabled':''}`):(t.resumable?button('从断点继续','collector-checkpoint','small primary',`data-id="${t.id}" ${c.active||mode==='demo'?'disabled':''}`):'')+button('重新采集','collector-retry','small',`data-id="${t.id}" ${c.active||mode==='demo'?'disabled':''}`)}</div></div></article>`;}
function collectorPanel(){
  const c=collectionState(),tasks=c.tasks;const current=tasks.filter(t=>t.active||t===tasks[0]),history=tasks.filter(t=>!current.includes(t));
  const readiness=mode==='demo'?'演示区禁用':c.active?'状态：采集中':c.available?'状态：本地依赖就绪':'状态：缺少运行依赖';
  return `<section id='collection-panel' class='card section-gap collector-panel' aria-label='自建采集任务'><div class='card-head'><h2>${icon('radar')} 采集任务</h2><span class='collector-readiness' role='status'>${readiness}</span></div><div class='card-body'>${notice(mode==='demo'?'演示区不会访问抖音。':'任务按所选通道读取；HTTP 不会自动回退到浏览器。')}${current.length?current.map(collectionTask).join(''):`<div class='collection-empty'><p>暂无任务，请点击上方“新建采集任务”。</p></div>`}${history.length?folded('collection-history',`历史任务（${history.length}）`,history.map(collectionTask).join('')):''}</div></section>`;
}
function collectionTargetField(value,label){return area('target',label,value,'class="collection-target-input" data-collection-target rows="3" required maxlength="2000"')+`<p class="monitor-hint" data-collection-target-hint></p>`;}
function syncCollectionForm(form){
  const formId=form?.getAttribute('id');
  if(!['collector-form','plan-form','monitor-form'].includes(formId))return;
  const target=$('[data-collection-target]',form),hint=$('[data-collection-target-hint]',form);
  if(!target||!hint)return;
  const kind=$('[name="kind"]',form)?.value,video=kind==='video',author=kind==='author',http=$('[name="transport"]',form)?.value==='http';
  target.rows=video||author?3:1;target.maxLength=video||author?2000:80;
  hint.id='collection-target-hint-'+formId;target.setAttribute('aria-describedby',hint.id);
  hint.textContent=author?'仅支持后端 HTTP。每行一个种子作品链接或 ID，最多 3 个；从这些作者返回的各 10 个候选中，按无畏契约文案匹配、作品发布时间优先选取。不代表作者全部作品。':!video?'填写一个搜索词组，最多 80 字。':http?'每行填写一个完整视频链接或数字 ID，最多 5 个；重复目标合并后全部读取。':'填写一个完整视频链接或数字 ID；本机浏览器每批支持一个指定视频。';
  const limit=$('[name="video_limit"]',form);
  if(limit){limit.disabled=video;const label=limit.closest('.field');label.hidden=video;label.classList.add('collection-target-limit');}
}
function syncFreshnessHint(){const form=$('#monitor-form');if(!form)return;const target=Number($('[name="freshness_target_seconds"]',form)?.value),interval=Number($('[name="interval_seconds"]',form)?.value),hint=$('#freshness-hint');if(hint)hint.textContent=interval>=target?'当前检查间隔已达到或超过时效目标，再加读取耗时，可能无法达标。':'目标用于衡量实际采集延迟；检查间隔从上一批结束后计算，仍需实测。';for(const b of document.querySelectorAll('[data-action="freshness-preset"]')){b.classList.toggle('selected',Number(b.dataset.seconds)===target);b.setAttribute('aria-pressed',String(Number(b.dataset.seconds)===target));}}
function syncCollectionForms(){syncFreshnessHint();for(const form of document.querySelectorAll?.('#collector-form,#plan-form,#monitor-form')||[])syncCollectionForm(form);}
function collectorDialog(video=null,retry=null){if(mode!=='live')throw Error('请先切换正式数据');if(collectionState().active)throw Error('已有采集会话，请先完成或停止它');const kind=video?'video':retry?.kind||'search';const target=video?(video.url||video.external_id):retry?.target||(S.settings.keywords||'无畏契约陪玩').split(/[,，\n]/)[0];showModal('新建真实采集任务',notice('HTTP 使用已准备的本机会话；浏览器通道会打开专用窗口。验证码会进入自动处理实验流程；未确认通过时暂停，保留已有数据。')+`<input type="hidden" name="request_id" value="${crypto.randomUUID()}">`+transportField(retry?.transport)+select('kind','发现方式',opts({search:'关键词搜索相关视频',video:'指定抖音完整视频链接 / ID',author:'从作品作者发现'},kind))+collectionTargetField(target,'关键词或完整视频链接')+`<div class="fields-2">${field('video_limit','发现后最多读取视频数',retry?.video_limit||3,'number','required min="1" max="5"')}${field('comment_limit','每视频评论上限',retry?.comment_limit||30,'number','required min="1" max="100"')}</div>`+field('page_concurrency','同时读取的视频数',retry?.page_concurrency||1,'number','required min="1" max="4"')+notice('视频页并发范围 1–4，不超过本批视频数；这不是平台安全频率保证。遇到验证暂停后续页面操作，遇到访问限制停止整批。'),'collector-form',submit('开始本批采集'));}
function queueCollectionPoll(){clearTimeout(collectionPollTimer);if(mode==='live'&&!document.hidden)collectionPollTimer=setTimeout(pollCollection,collectionState().active||liveState().active_id||S.collector?.model_queue?.active||S.collector?.plans?.some(p=>p.status==='running')?2500:15000);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&mode==='live')void pollCollection();else queueCollectionPoll();});
function checkpointPanel(t){const rows=t.checkpoints||[];if(!rows.length)return '';const names={pending:'未开始',reading:'读取中 / 中断点',done:'本批已完成',partial:'部分读取'};return `<details class="collection-checkpoints" data-checkpoint-id="${t.id}"><summary>视频进度 ${rows.filter(r=>r.status==='done').length} / ${rows.length}${t.parent_task_id?` · 接续任务 #${t.parent_task_id}`:''}</summary>${rows.map(r=>`<div class="checkpoint-row"><div><a href="${esc(r.video_url)}" target="_blank" rel="noopener noreferrer">${esc(r.video_title)}</a><small>${esc(r.detail||'等待读取')}${r.status==='done'?' · 不代表全量评论':''}</small></div>${badge(names[r.status]||r.status,r.status==='done'?'good':'')}</div>`).join('')}</details>`;}
function checkpointDialog(id){const t=collectionState().tasks.find(t=>t.id===id);if(!t?.resumable||t.active)throw Error('当前没有可恢复的视频断点');showModal('从视频断点继续',`<input name="id" type="hidden" value="${id}"><input name="request_id" type="hidden" value="${crypto.randomUUID()}">`+notice('沿用原任务通道，跳过已完成的视频。中断视频从第一页重新读取并去重；不会切换通道或绕过验证。')+checkpointPanel(t),'checkpoint-form',submit('从断点继续读取'));}
function schedulePanel(){const plans=(S.collector?.plans||[]).filter(p=>!p.continuous);const names={paused:'已暂停',running:'有限调度已启用',attention:'需要处理',completed:'已达批次上限'};const content=`<section class="card"><div class="card-head"><div><h2>有限采集计划</h2><small>仅在启用后运行，完成指定批次即停止</small></div>${button('新建计划','plan-new','small',mode==='demo'?'disabled':'')}</div><div class="card-body">${plans.length?plans.map(p=>`<article class="collection-task"><div class="row spread"><div><strong>${esc(p.name)}</strong><p class="collection-target">${esc(p.target)} · ${{1:'低',2:'标准',3:'高'}[p.priority]}优先级</p></div>${badge(names[p.status]||p.status,p.status==='running'?'good':p.status==='attention'?'warn':'')}</div><p class="collection-detail">${esc(p.detail)}</p><div class="collection-counts"><span>已启动 <b>${p.run_count}/${p.run_limit}</b> 批</span><span>已结束 <b>${p.settled_count}</b> 批</span><span>批次间隔 ${p.interval_seconds} 秒</span><span>每批 ${p.video_limit} 视频 / 每视频 ${p.comment_limit} 评论</span><span>视频读取并发 ${p.page_concurrency||1}</span></div><div class="row spread"><small>${p.next_run_at?`下次到期 ${date(p.next_run_at)}`:'当前无待执行时刻'}${p.last_task_id?` · 上次任务 #${p.last_task_id}`:''}</small><div class="actions">${p.status==='running'?button('暂停计划','plan-pause','small',`data-id="${p.id}"`):button('启动计划','plan-start','small primary',`data-id="${p.id}" ${p.run_count>=p.run_limit?'disabled':''}`)}${button('修改','plan-edit','small',`data-id="${p.id}" ${p.status==='running'?'disabled':''}`)}</div></div></article>`).join(''):notice('未配置计划，不会定时访问抖音。计划可先保存；至少手动完成一批真实评论读取后才能启用。')}</div><div class="card-foot">遇到验证或失败暂停后续批次；重启服务后计划也保持暂停。间隔是你的调度配置，不是平台安全频率承诺。</div></section>`;const active=plans.filter(p=>p.status==='running'||p.status==='attention').length;return folded('schedule-panel',`采集计划（${plans.length}）${active?' · 有运行或待处理计划':''}`,content,active>0);}
function planDialog(id){if(mode!=='live')throw Error('演示区不创建真实计划');const p=S.collector?.plans?.find(p=>p.id===id)||{};showModal(p.id?'修改有限采集计划':'新建有限采集计划',`<input name="id" type="hidden" value="${p.id||''}">`+notice('保存不会启动。启用后按所选通道读取；验证、限制或失败时暂停，最多执行指定批次数。')+field('name','计划名称',p.name||'无畏契约需求发现','text','required maxlength="100"')+transportField(p.transport)+select('kind','采集方式',opts({search:'关键词搜索',video:'指定视频',author:'从作品作者发现'},p.kind||'search'))+collectionTargetField(p.target||'无畏契约陪玩','关键词或完整视频链接')+`<div class="fields-2">${field('video_limit','发现后作品上限',p.video_limit||1,'number','required min="1" max="5"')}${field('comment_limit','每视频评论上限',p.comment_limit||10,'number','required min="1" max="100"')}${field('interval_seconds','批次结束后的间隔 / 秒',p.interval_seconds||600,'number','required min="300" max="86400"')}${field('run_limit','计划总批次数',p.run_limit||3,'number','required min="1" max="24"')}</div>`+field('page_concurrency','每批视频读取并发',p.page_concurrency||1,'number','required min="1" max="4"')+select('priority','调度优先级',opts({3:'高',2:'标准',1:'低'},p.priority||2)),'plan-form',submit('保存为暂停计划'));}
async function pollCollection(){
  if(collectionPolling||mode!=='live'||document.hidden){queueCollectionPoll();return;}
  collectionPolling=true;const requestedMode=mode,sequence=loadSequence;
  try{
    const r=await fetch(`/api/collector?mode=${requestedMode}`);if(!r.ok)throw Error('采集状态读取失败');
    const value=await r.json();if(mode!==requestedMode||sequence!==loadSequence)return;
    const stable=v=>({...v,board:v?.board?{...v.board,updated_at:null}:undefined});
    const changed=JSON.stringify(stable(S.collector))!==JSON.stringify(stable(value));
    collectionPanelPending ||= changed;
    S.collector=value;
    if(S.semantic&&value.model_queue)S.semantic.queue=value.model_queue;
    if(changed&&!value.tasks.some(t=>t.active))collectionReloadPending=true;
    const editing=busy||document.body.classList.contains?.('menu-open')||semanticDraftDirty||(page.startsWith('monitor')&&monitorDraftDirty)||(page==='live'&&liveDraftDirty)||$('#modal')?.open||document.activeElement?.matches('input,textarea,select,[contenteditable="true"]');
    if(changed&&page==='settings'&&!editing&&$('#model-queue-panel')){$('#model-queue-panel').outerHTML=modelQueuePanel();icons();}
    if(page==='live'&&changed){if($('#live-results'))redrawLiveResults();if($('#live-status'))$('#live-status').outerHTML=liveStatus();if($('#live-tracking'))$('#live-tracking').outerHTML=liveTrackingPanel();icons();}
    if(page==='live'&&liveScope!=='current'&&!editing&&liveArchive&&liveArchiveModelKey!==JSON.stringify(value.model_queue||null))await refreshLiveArchive(true,true);
    if(!editing&&collectionPanelPending&&page==='monitor'){
      const openedPanels=openPanelIds();
      if($('#monitor-overview'))$('#monitor-overview').outerHTML=monitorOverview();
      if($('#discovery-overview'))$('#discovery-overview').outerHTML=discoveryOverview();
      if($('#work-pool'))redrawWorkPool();
      if($('#monitor-settings'))$('#monitor-settings').outerHTML=monitorSettingsPanel();
      syncCollectionForms();
      if($('#monitor-result-panel'))redrawMonitorResults();
      // Keep the user's chosen panel state while monitoring updates.
      if($('#schedule-panel'))$('#schedule-panel').outerHTML=schedulePanel();
      if($('#collection-panel')){
        const opened=[...document.querySelectorAll('details[data-checkpoint-id][open]')].map(e=>e.dataset.checkpointId);
        $('#collection-panel').outerHTML=collectorPanel();
        for(const id of opened){const detail=$(`details[data-checkpoint-id="${id}"]`);if(detail)detail.open=true;}
      }
      icons();
      restorePanelIds(openedPanels);
      collectionPanelPending=false;
    }
    if(page==='monitor'&&!editing)await refreshMonitorHistory();
    if(collectionReloadPending&&!editing&&!(page==='live'&&value.live_monitor?.active_id)){await load();collectionReloadPending=false;}
  }catch{if(page==='monitor')toast('暂时无法刷新采集状态；不会因此重启任务',true);}
  finally{collectionPolling=false;queueCollectionPoll();}
}

$('#modal').addEventListener('cancel',event=>{event.preventDefault();closeModal();});
