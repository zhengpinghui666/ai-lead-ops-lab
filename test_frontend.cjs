'use strict';
// Structural smoke tests only. This is not a browser or visual acceptance test.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const lucide=require('./static/vendor/lucide.min.js');
const python=process.env.CLUBOPS_TEST_PYTHON||process.env.CLUBOPS_PYTHON||'python';
const result=spawnSync(python,['-c',`import tempfile,json; from pathlib import Path; import clubops as a; import daily_dashboard as dashboard
with tempfile.TemporaryDirectory(prefix='clubops-ui-test-') as td:
 a.DATA_DIR=Path(td); a.init(); a.seed_demo(); print(json.dumps({'live':dict(a.state(),dashboard=dashboard.snapshot()),'demo':dict(a.state('demo'),dashboard=dashboard.snapshot('demo'))}))`],{cwd:__dirname,encoding:'utf8'});
assert.equal(result.status,0,result.stderr);
const fixture=JSON.parse(result.stdout);
const elements=new Map();
function element(key){if(!elements.has(key))elements.set(key,{insertAdjacentHTML(position,html){this.innerHTML+=html;},innerHTML:'',textContent:'',className:'',hidden:false,value:'',querySelector(){return null;},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;},focus(){},setSelectionRange(){},classList:{remove(){},toggle(){}},getBoundingClientRect(){return {left:0,top:0,right:100,bottom:100};}});return elements.get(key);}
const listeners=new Map(),posts=[];
// Removed sidebar/header elements must not be required for any page to render.
const removedElements=new Set(['#club-name','#breadcrumb','#mode-badge','#mode-toggle']);
const document={querySelectorAll:()=>[],querySelector:key=>removedElements.has(key)?null:element(key),addEventListener(type,fn){listeners.set(type,fn);},createElement(){return {click(){}};},body:element('body')};
class TestFormData {constructor(form){if(form.failFormData)throw Error('synthetic form read failure');this.values=form.values;} [Symbol.iterator](){return this.values[Symbol.iterator]();} getAll(name){return this.values.filter(([k])=>k===name).map(([,v])=>v);}}
const context=vm.createContext({document,FormData:TestFormData,location:{hash:''},window:{lucide:{createIcons(){}},addEventListener(){},scrollTo(){}},fetch:async(url,options)=>{if(url.startsWith('/api/live-history'))return {ok:true,json:async()=>({rows:[],total:0,anchor_id:0,limit:25})};if(options?.method==='POST'){posts.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({result:{id:99}})};}return {ok:true,json:async()=>({...fixture.live,csrf:'test-only-token'})};},setTimeout,clearTimeout,crypto:globalThis.crypto,Blob,URL,URLSearchParams,AbortController,console,fixtures:fixture});
const run=code=>vm.runInContext(code,context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'static/app.js'),'utf8'),context);
function checkHtml(html){assert.ok(html.length>100);assert.ok(!html.includes('undefined'),'Undefined appears in UI');assert.ok(!html.includes('NaN'),'NaN appears in UI');for(const [,name] of html.matchAll(/data-lucide="([^"]+)"/g)){const key=name.replace(/(^|-)([a-z])/g,(_,sep,c)=>c.toUpperCase());assert.ok(lucide[key],`Missing icon ${name}`);}}
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  run("history={replaceState(){}};");
  assert.equal(run('monitorResultFilter'),'valuable');
  assert.equal(run('liveFilter'),'valuable');
  assert.equal(run('liveScope'),'all');
  const preparationNotice=run(`emptyConversation([{id:26,status:'failed',content:'合成消息',uid_http:{phase:'identity',evidence:{submission_reserved:false}}}])`);
  assert.match(preparationNotice,/账号认证未通过，消息尚未提交/);
  assert.ok(!preparationNotice.includes('平台未接受'));
  assert.match(run(`messageFailureDetail({status:'failed',uid_http:{phase:'send',evidence:{submission_reserved:true,platform_message:'仅关注的人可私信'}}})`),/仅关注的人可私信/);
  assert.match(run(`messageFailureDetail({status:'unknown',uid_http:{phase:'identity',evidence:{submission_reserved:false}}})`),/尚未取得确定回执/);
  assert.ok(!run(`messageFailureDetail({status:'failed',uid_http:{phase:'identity',evidence:{}}})`).includes('尚未提交'));
  assert.match(run(`messageFailureDetail({status:'failed',uid_http:{phase:'prepare_send',evidence:{submission_reserved:false,demand_freshness:{status:'expired'}}}})`),/需求已超过一天，消息尚未提交/);
  assert.match(run(`messageFailureDetail({status:'unknown',uid_http:{phase:'prepare_send',evidence:{submission_reserved:false,demand_freshness:{status:'expired'}}}})`),/尚未取得确定回执/);
  run(`groupState={groups:[{id:1,name:'瓦搭子群',participants:156,enabled:1,member:1,status:'running',message_count:2,screened_count:1}],enabled:1,messages:[{id:1,uid:'12345',nickname:'<script>unsafe</script>',group_title:'瓦搭子群',raw_text:'<img src=x onerror=alert(1)>',filter_reason:'未通过初筛',category:'uncertain',analysis_method:'rules'},{id:2,uid:'12346',group_title:'瓦搭子群',raw_text:'找陪练',filter_reason:'',category:'buyer',analysis_method:'model',model_result:{status:'completed',result:{category:'buyer',reason:'合成结果'}},outreach:{status:'accepted',detail:'已提交'}}]};`);
  const groupHtml=run('groupPage()');checkHtml(groupHtml);
  assert.ok(groupHtml.includes('&lt;script&gt;unsafe'));
  assert.ok(!groupHtml.includes('<img src=x'));
  assert.ok(groupHtml.includes('data-action="group-message-detail"'));
  assert.ok(!groupHtml.includes('公开群筛选'),'Group discovery belongs to source management');
  const messageDetail=run('groupMessageDetail(groupState.messages[1])');checkHtml(messageDetail);
  assert.ok(messageDetail.includes('服务端接受 · 未确认送达'));
  run("section='sources'");const groupSources=run('groupPage()');run("section=''");
  assert.ok(groupSources.includes('公开群筛选'));
  assert.ok(groupSources.includes('data-action="group-discovery-toggle"'));
  assert.ok(groupSources.includes('data-enabled="true"'));
  const publicHtml=run(`groupDiscoveryPanel({enabled:true,detail:'<script>bad</script>',candidates:[{status:'pending'},{status:'joined'}]})`);
  assert.ok(publicHtml.includes('待审核 <b>1</b>'));
  const uncertainGroups=run(`groupDiscoveryPanel({enabled:true,counts:{uncertain:3,question:1,full:1},candidates:[]})`);
  assert.ok(uncertainGroups.includes('结果未确认 <b>3</b>'));
  assert.ok(uncertainGroups.includes('入群问答 <b>1</b>'));
  assert.ok(!uncertainGroups.includes('待审核 <b>3</b>'));
  assert.ok(publicHtml.includes('暂停筛选'));
  assert.ok(publicHtml.includes('&lt;script&gt;bad'));
  assert.ok(!groupHtml.includes('data-action="group-send"'));
  run('groupState=null;');
  for(const value of elements.values())value.classList.add=()=>{};
  for(const mode of ['live','demo']){
    for(const page of ['overview','monitor','monitor-settings','live','leads','recruit','roster','inbox','analytics','settings']){
      run(`S=fixtures.${mode}; mode='${mode}';page='${page}';selected=null;conversation=null;render();`);
      checkHtml(element('#main').innerHTML);
      assert.ok(!/data-action=["'](?:import|export|add-source)["']/.test(element('#main').innerHTML),'No manual file-transfer actions in product pages');
      assert.ok(!element('#main').innerHTML.includes('可在导出中查看'));
      if(['monitor','live','settings','monitor-settings'].includes(page))assert.ok(!/<form id="(?:settings|semantic|monitor|live)-form"/.test(element('#main').innerHTML),'Configuration belongs in dialogs');
      assert.equal(element('#demo-banner').hidden,mode!=='demo');
    }
  }
  run(`S={...fixtures.live,collector:{tasks:[],plans:[],board:{rows:[{id:1,external_id:'123456',title:'<script>unsafe</script>',url:'https://www.douyin.com/video/123456',state:'history',continuous_monitoring:false,archived_comments:12,fresh_comments:0,model_pending:0}],summary:{tracked:0,reading:0,works:1,fresh_comments:0,model_pending:0}},results:{rows:[{text:'A',video_url:'video-A'},{text:'B',video_url:'video-B'}]}}};workFilter='all';`);
  const poolHtml=run('workPool()');checkHtml(poolHtml);
  run("workFilter='vertical'");assert.ok(!run('workPool()').includes('&lt;script&gt;unsafe'));
  run("S.collector.board.rows[0].verticality={matched:true,label:'垂直对口',reason:'<script>reference',tags:['标签'],reference_hits:[],history_samples:0,history_hits:0};");
  assert.ok(run('workPool()').includes('&lt;script&gt;unsafe'));run("workFilter='all'");
  run("referenceState={rows:[{id:1,revision:1,status:'pending',rule:{},reason:'',content:{title:'<script>样本',copy:'文案',tags:['<img>'],author_name:'作者',author_sec_uid:''}}]};assetReferenceReviewDialog(1)");
  assert.ok(!element('#modal-content').innerHTML.includes('<script>'));assert.ok(!element('#modal-content').innerHTML.includes('<img>'));assert.match(element('#modal-content').innerHTML,/name="service_terms"/);

  run("discoverySettingsDialog()");checkHtml(element('#modal-content').innerHTML);
  assert.match(element('#modal-content').innerHTML,/同一关键词再次发现间隔/);
  run("discoveryAuthorsDialog()");checkHtml(element('#modal-content').innerHTML);
  assert.match(element('#modal-content').innerHTML,/从相关作品中识别作者后/);
  run("workQuery='not-found'");assert.match(run('workPool()'),/暂无对应作品/);
  run("workQuery='123456'");assert.match(run('workPool()'),/unsafe/);run("workQuery=''");

  assert.ok(poolHtml.includes('未纳入持续监控'));
  assert.ok(poolHtml.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
  assert.match(run('workMetrics({metrics:{likes:0,comments:123,shares:null,favorites:5}})'),/点赞<b>0<\/b>/);
  assert.match(run('workMetrics({metrics:{likes:0,comments:123,shares:null,favorites:5}})'),/平台评论<b>123<\/b>/);
  assert.match(run('workMetrics({})'),/未获取/);
  run("workFilter='tracked'");assert.ok(run('workPool()').includes('当前没有持续跟踪'));
  run("monitorResultFilter='all';monitorResultVideo='video-A'");assert.equal(run('monitorResultRows().length'),1);
  assert.equal(run('monitorResultRows()[0].text'),'A');
  run("workFilter='all';monitorResultVideo='';S=fixtures.demo;mode='demo';unlinkedCommentsDialog()");checkHtml(element('#modal-content').innerHTML);
  assert.equal(run('typeof importDialog'), 'undefined');
  for(const command of ['videoDialog()','memberDialog()','memberDialog(1)','reviewDialog(1)','historyDialog(1)','contactDialog(1)','followDialog(1)']){run(command);checkHtml(element('#modal-content').innerHTML);}
  assert.equal(run(`esc('<img src=x onerror="alert(1)">')`),'&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(run("avatarInitials('🕷️')"),'🕷️');
  assert.equal(run("avatarInitials('甲👨‍👩‍👧‍👦')"),'甲👨‍👩‍👧‍👦');
  assert.equal(run('parentEvidence({parent_context:{status:"none"}})'), '');
  assert.match(run('parentEvidence({parent_external_id:"missing-parent"})'), /未读到上级评论原文/);
  assert.match(run('parentEvidence({parent_context:{status:"conflict",external_id:"wrong-parent"}})'), /回复关系冲突/);
  const parentHtml=run('parentEvidence({parent_context:{status:"available",external_id:"synthetic-parent",raw_text:"<img src=x onerror=alert(1)>",nickname:"<script>",source_name:"合成测试"}})');
  assert.match(parentHtml, /已读到上级评论原文/);assert.ok(!parentHtml.includes('<img'));assert.ok(!parentHtml.includes('<script>'));
  assert.equal(run('ruleEvidence({analysis_method:"pending"})'), '');
  assert.match(run('ruleEvidence({analysis_method:"rules",facts:{}})'), /旧规则结果没有保存命中片段/);
  const ruleHtml=run('ruleEvidence({analysis_method:"rules",facts:{rules_version:"rules-v2",warnings:["<script>合成冲突"],evidence:[{kind:"request",source:"comment",text:"<img src=x>",negated:true},{kind:"service_context",source:"parent",text:"陪练"}]}})');
  assert.match(ruleHtml,/rules-v2/);assert.match(ruleHtml,/含否定 · 不作正向命中/);assert.match(ruleHtml,/询价语境/);assert.match(ruleHtml,/上级原文/);
  assert.ok(!ruleHtml.includes('<script>'));assert.ok(!ruleHtml.includes('<img'));
  assert.match(run('ruleEvidence({analysis_method:"human",facts:{rules_version:"rules-v2",evidence:[]}})'), /不代表人工已确认这些字段/);
  assert.match(ruleHtml, /此条保留历史判断，升级不会自动改写/);
  const attributedHtml=run('ruleEvidence({analysis_method:"rules",facts:{rules_version:S.settings.ruleset_version,warnings:["转述归属未确定"],evidence:[{kind:"reported",source:"comment",text:"据说"},{kind:"budget",source:"comment",text:"预算500",attribution:"unconfirmed"}]}})');
  assert.match(attributedHtml, /转述 \/ 举例/);
  assert.match(attributedHtml, /归属待核对 · 未提取为此用户字段/);
  assert.ok(!attributedHtml.includes('升级不会自动改写'));
  run('reviewDialog(1)');assert.match(element('#modal-content').innerHTML,/查看初筛依据/);
  run('historyDialog(1)');assert.match(element('#modal-content').innerHTML,/查看初筛依据/);
  run(`page='leads';tab='all';query='no-match-at-all';render()`);
  assert.ok(element('#main').innerHTML.includes('当前筛选下没有线索'));
  run(`conversation=1;draftText='用户甲的草稿';draftKey='key-a';stashDraft();restoreDraft(2)`);
  assert.equal(run('draftText'),'');run('restoreDraft(1)');assert.equal(run('draftText'),'用户甲的草稿');
  run(`S=fixtures.live;S.collector={tasks:[],results:{task:{id:3,status:'completed'},counts:{observed:2,accepted:1,filtered:1},rows:[{external_id:'1',text:'找陪练<script>',text_origin:'observation',nickname:'test',user_identifier:'123',video_url:'https://www.douyin.com/video/7600000000000000001',include_matches:['陪练'],exclude_matches:[],filter_reason:''},{external_id:'2',text:'',text_origin:'unavailable',nickname:'',filter_reason:'filtered_blocked'}]}};monitorResultFilter='all';monitorResultQuery='';`);
  const resultHtml=run('monitorResultsPanel()');checkHtml(resultHtml);
  assert.match(resultHtml,/评论发布时间/);assert.match(resultHtml,/首次采集时间 ↓/);
  assert.match(resultHtml,/<mark>陪练<\/mark>&lt;script&gt;/);
  assert.ok(!resultHtml.includes('<script>'));
  assert.ok(!resultHtml.includes('旧任务未保存原文'));
  assert.ok(!resultHtml.includes('命中屏蔽词'));
  assert.equal(run('JSON.stringify(monitorResults().counts)'),JSON.stringify({observed:1,accepted:1,filtered:0}));
  run("monitorResultFilter='accepted'");assert.equal(run('monitorResultRows().length'),1);
  run("monitorResultFilter='filtered';monitorResultQuery='test'");assert.equal(run('monitorResultRows().length'),0);
  run("monitorResultFilter='all';monitorResultQuery='';monitorResultPage=99");run('monitorResultsPanel()');assert.equal(run('monitorResultPage'),1);
  const fullMonitor=run('monitor()');assert.ok(fullMonitor.includes('id="monitor-result-panel"'));assert.ok(!fullMonitor.includes('id="work-pool"'));assert.ok(!fullMonitor.includes('id="collection-panel"'));
  assert.ok(!run('transportField()').includes('value="http"'),'Old backend cannot silently accept an unsupported HTTP selection');
  run("mode='live';S.collector.http={installed:true,session:{ready:true},live_verified:false}");
  assert.match(run('transportField()'), /value="http" selected/);
  run('collectorDialog()');assert.match(element('#modal-content').innerHTML,/后端 HTTP/);
  assert.ok(!element('#modal-content').innerHTML.includes('打开浏览器并采集'));
  assert.match(run('connectionRows()'), /会话已准备/);
  assert.ok(!run('connectionRows()').includes('本工作区已读到评论'));
  run("mode='demo'");
  const appHtml=fs.readFileSync(path.join(__dirname,'static/app.html'),'utf8');
  assert.ok(!appHtml.includes('onclick='));
  assert.ok(!appHtml.includes('id="club-name"'));
  assert.ok(!appHtml.includes('class="workspace"'));
  assert.ok(!appHtml.includes('class="topbar"'));
  for(const id of ['breadcrumb','mode-badge','mode-toggle'])assert.ok(!appHtml.includes(`id="${id}"`));
  assert.ok(!appHtml.includes('data-action="mode"'));
  assert.ok(!appHtml.includes('data-action="refresh"'));
  assert.match(appHtml,/<button class="icon-button mobile-menu" data-action="menu" aria-label="打开导航" aria-controls="sidebar" aria-expanded="false">/);
  assert.equal(run('mode'), 'demo'); // Tests may still exercise isolated demo data internally.
  assert.match(fs.readFileSync(path.join(__dirname,'static/app.js'),'utf8'), /mode='live'/);
  assert.match(appHtml,/<nav id="nav" aria-label="主导航">/);
  run("S=fixtures.demo;mode='demo';page='overview';render()");
  assert.equal((element('#nav').innerHTML.match(/<a /g)||[]).length,6);
  assert.doesNotMatch(element('#nav').innerHTML,/#groups|#live/);assert.match(element('#nav').innerHTML,/采集监控/);
  assert.ok(!element('#nav').innerHTML.includes('#roster'));
  assert.ok(!element('#nav').innerHTML.includes('#recruit'));
  assert.match(element('#nav').innerHTML,/私信导流/);
  assert.ok(!element('#main').innerHTML.includes('可接单人员'));
  assert.match(element('#main').innerHTML,/今日运营/);
  assert.match(element('#main').innerHTML,/验证码确认通过率/);
  assert.match(element('#main').innerHTML,/未尝试/);
  assert.match(element('#main').innerHTML,/所选时段暂无记录/);
  assert.ok(!element('#main').innerHTML.includes('按小时累计'));
  for(const days of [7,90]){
    run(`trendDays=${days};render();`);
    assert.match(element('#main').innerHTML,/所选时段暂无记录/);
  }
  run('trendDays=30;render();');
  const trimmedChart=run(`dailyChart({granularity:'day',date:'2026-09-13',labels:['2026-09-10','2026-09-11','2026-09-12','2026-09-13'],series:{intent_users:[0,2,0,3],dm_accepted:[0,0,0,1]}},...trendGroups.demand)`);
  assert.ok(!trimmedChart.includes('2026-09-10'));
  assert.match(trimmedChart,/从 2026-09-11 开始/);
  assert.match(trimmedChart,/2026-09-12 · 当日 0/,'Quiet days between real activity must remain');
  assert.equal((trimmedChart.match(/<circle /g)||[]).length,6);
  const demandDetail=run('leadDetail(S.leads[0])');
  assert.ok(!demandDetail.includes('俱乐部候选人员'));
  assert.ok(!demandDetail.includes('暂无符合已知条件的可用人员'));
  run('followDialog(S.leads[0].id)');
  assert.ok(!element('#modal-content').innerHTML.includes('name="assigned_member"'));
  assert.match(element('#modal-content').innerHTML,/value="referred"/);
  assert.ok(!element('#modal-content').innerHTML.includes('value="won"'));
  assert.match(element('#modal-content').innerHTML,/保存不会发送消息/);
  run("const legacyStage=S.leads[0].stage;S.leads[0].stage='won';followDialog(S.leads[0].id);S.leads[0].stage=legacyStage;");
  assert.match(element('#modal-content').innerHTML,/value="won" selected/);
  assert.match(element('#modal-content').innerHTML,/已成交（历史）/);
  run("page='analytics';render()");
  assert.match(element('#main').innerHTML,/人工确认导流/);
  assert.ok(!element('#main').innerHTML.includes('已记录成交'));
  run("page='overview';render()");
  assert.doesNotMatch(element('#main').innerHTML,/service-shortcuts|data-action="service-leads"/);
  assert.ok(!element('#main').innerHTML.includes('三角洲'));
  run("const reviewLead=S.leads.find(l=>l.latest.facts?.service_type==='对局复盘'&&l.category==='buyer');");
  assert.ok(run('matching(reviewLead).every(m=>m.region===reviewLead.latest.facts.region)'));
  assert.ok(run("replyTemplate(reviewLead).includes('对局录像')"));
  run("query='';gameFilter='';serviceFilter='新手陪练';");
  assert.ok(run("filteredLeads(['buyer']).length>0"));
  assert.ok(run("filteredLeads(['buyer']).every(l=>l.latest.facts.service_type==='新手陪练')"));
  run('memberDialog()');assert.ok(element('#modal-content').innerHTML.includes('name="service_types"'));
  assert.ok(element('#modal-content').innerHTML.includes('value="无畏契约"'));
  run("mode='live';S=fixtures.live;collectorDialog()");checkHtml(element('#modal-content').innerHTML);
  assert.ok(element('#modal-content').innerHTML.includes('开始本批采集'));
  assert.ok(element('#modal-content').innerHTML.includes('name="page_concurrency"'));
  // The plan's hidden name=id must not disable its mode-specific instructions.
  for(const formId of ['collector-form','plan-form','monitor-form']){
    const label={hidden:false,classList:{add(){}}},target={value:'7600000000000000001\n7600000000000000003',setAttribute(n,v){this[n]=v;}},hint={};
    const kind={value:'video'},transport={value:'http'},limit={value:'2',closest:()=>label};
    const fields={'[data-collection-target]':target,'[data-collection-target-hint]':hint,'[name="kind"]':kind,'[name="transport"]':transport,'[name="video_limit"]':limit};
    context.collectionFormFixture={id:{tagName:'INPUT'},getAttribute:()=>formId,querySelector:s=>fields[s]};
    run('syncCollectionForm(collectionFormFixture)');
    assert.equal(target.rows,3);assert.equal(target.maxLength,2000);assert.equal(limit.disabled,true);assert.equal(label.hidden,true);
    assert.match(hint.textContent,/最多 5 个/);assert.equal(target['aria-describedby'],'collection-target-hint-'+formId);
    transport.value='local_browser';run('syncCollectionForm(collectionFormFixture)');assert.match(hint.textContent,/每批支持一个/);
    kind.value='search';run('syncCollectionForm(collectionFormFixture)');
    assert.equal(target.rows,1);assert.equal(target.maxLength,80);assert.equal(limit.disabled,false);assert.equal(label.hidden,false);
    assert.equal(limit.value,'2');assert.equal(target.value,'7600000000000000001\n7600000000000000003');
  }
  delete context.collectionFormFixture;
  // Disabled search-only controls are absent from real FormData, never null/NaN.
  for(const kind of ['video','search']){
    const values=[['kind',kind],['transport','http'],['target',kind==='video'?'7600000000000000001\n7600000000000000003':'无畏契约陪玩'],['comment_limit','10'],['page_concurrency','2'],['request_id','synthetic-form-only']];
    if(kind==='search')values.push(['video_limit','2']);
    await listeners.get('submit')({target:{getAttribute:()=> 'collector-form',values,querySelector:()=>null},preventDefault(){}});
    assert.match(posts.at(-1).url,/\/api\/collector-start\?/);
    assert.equal(posts.at(-1).body.kind,kind);assert.equal(posts.at(-1).body.comment_limit,10);
    assert.equal(Object.hasOwn(posts.at(-1).body,'video_limit'),kind==='search');
    if(kind==='search')assert.equal(posts.at(-1).body.video_limit,2);
  }
  run("S.collector={available:true,tasks:[{id:7,kind:'search',target:'<img src=x>',status:'needs_login',active:true,detail:'等待登录',videos:0,comments:0,inserted:0,duplicate:0,revised:0,skipped:0,video_limit:1,comment_limit:10,updated_at:null}]};page='monitor';section='runs';render()");
  assert.ok(element('#main').innerHTML.includes('已处理，继续读取'));
  assert.ok(element('#main').innerHTML.includes('Alt + Tab'));
  assert.ok(element('#main').innerHTML.includes('data-action="collector-cancel"'));
  assert.ok(!element('#main').innerHTML.includes('<img src=x>'));
  run("S.collector.tasks[0].active=false;render()");
  assert.ok(element('#main').innerHTML.includes('浏览器会话已关闭'));
  assert.ok(!element('#main').innerHTML.includes('已处理，继续读取'));
  assert.ok(!element('#main').innerHTML.includes('Alt + Tab'));
  run("S.collector.tasks=[];mode='demo';render()");
  assert.ok(element('#main').innerHTML.includes('演示区不会访问抖音'));
  // Simplification must not remove operations, hide live tasks, or imply HTTP works.
  run("mode='live';S=fixtures.live;S.collector={available:true,tasks:[],plans:[]};page='monitor';render()");
  assert.match(element('#main').innerHTML,/data-action="collector-new"/);
  assert.match(element('#main').innerHTML,/状态：本地依赖就绪/);
  assert.match(element('#main').innerHTML,/HTTP 不会自动回退到浏览器/);
  assert.ok(!element('#main').innerHTML.includes('可启动 · 按批执行'));
  assert.ok(!element('#main').innerHTML.includes('DISCOVERY & MONITORING'));
  assert.ok(!element('#main').innerHTML.includes('期望间隔'));
  run("S.collector.tasks=[{id:3,status:'completed',active:false},{id:2,status:'completed',active:false},{id:1,status:'needs_login',active:true}].map(t=>({...t,kind:'search',target:'合成任务',detail:'合成说明',videos:1,comments:0,inserted:0,duplicate:0,revised:0,skipped:0,video_limit:1,comment_limit:10}));render()");
  const compactMonitor=element('#main').innerHTML,historyAt=compactMonitor.indexOf("id='collection-history'");
  assert.ok(historyAt>0);
  assert.ok(compactMonitor.indexOf('#1 · 关键词发现')<historyAt,'Active task stays outside collapsed history');
  assert.ok(compactMonitor.indexOf('#3 · 关键词发现')<historyAt,'Latest task stays visible');
  assert.ok(compactMonitor.indexOf('#2 · 关键词发现')>historyAt,'Older completed task moves to history');
  assert.match(compactMonitor,/<details id='collection-history' class='folded section-gap' >/, 'History starts collapsed');
  assert.ok(compactMonitor.indexOf('已处理，继续读取')<historyAt,'Login action remains visible');
  assert.match(compactMonitor,/<span class='collector-readiness' role='status'>状态：采集中<\/span>/);
  run("S.collector.tasks=[];S.collector.plans=[]");
  assert.match(run('monitorControls()'),/最近 1 小时/);
  assert.match(run('monitorControls()'),/data-action="monitor-start"/);
  checkHtml(run('monitorSettingsPanel()'));
  assert.match(run('monitorSettingsPanel()'),/name="window_value"[^>]*value="1"/);
  assert.match(run('monitorSettingsPanel()'),/name="page_concurrency"[^>]*max="4"/);
  assert.match(run('monitorSettingsPanel()'),/name="interval_seconds"[^>]*min="30"/);
  assert.match(run('monitorSettingsPanel()'),/name="include_keywords"/);
  assert.match(run('monitorSettingsPanel()'),/name="exclude_keywords"/);
  assert.ok(!run('monitorControls()').includes('data-action="monitor-config"'));
  run("S.collector.monitor={...monitorConfig(),enabled:true,status:'running',target:'<script>test'}");
  assert.match(run('monitorControls()'),/data-action="monitor-stop"/);
  assert.ok(!run('monitorControls()').includes('<script>'));
  assert.match(run('monitorSettingsPanel()'),/fieldset class="monitor-fields" disabled/);
  run("S.collector.monitor.include_keywords='<script>x</script>';S.collector.monitor.exclude_keywords='</textarea><img src=x>'");
  assert.ok(!run('monitorSettingsPanel()').includes('<script>'));
  assert.ok(!run('monitorSettingsPanel()').includes('<img src=x>'));
  run("S.collector.monitor={...monitorConfig(),enabled:false,status:'paused',active_task_id:9,stopping:true}");
  assert.match(run('monitorControls()'),/正在关闭/);
  assert.match(run('monitorControls()'),/data-action="monitor-stop" disabled/);
  run("S.collector.monitor={...monitorConfig(),active_task_id:null,stopping:false,lookback_hours:3}");
  assert.match(run('monitorSettingsPanel()'),/value="hours" selected/);
  run("markMonitorDraft({closest:s=>s==='#monitor-form'})");
  assert.equal(run('monitorDraftDirty'),true);
  assert.throws(()=>run('monitorStartDialog()'),/先保存监控设置/);
  run('monitorDraftDirty=false');
  run('monitorStartDialog()');assert.match(element('#modal-content').innerHTML,/不会发送私信/);
  run('closeModal();delete S.collector.monitor');
  assert.match(run('schedulePanel()'),/<details id='schedule-panel' class='folded section-gap' >/);
  run("S.collector.plans=[{id:1,name:'合成计划',target:'合成关键词',status:'attention',detail:'需处理',priority:2,run_count:1,run_limit:2,settled_count:1,interval_seconds:300,video_limit:1,comment_limit:10}]");
  assert.match(run('schedulePanel()'),/<details id='schedule-panel' class='folded section-gap' open>/);
  run("S.collector.plans=[];page='roster';gameFilter='';render()");
  assert.equal((element('#main').innerHTML.match(/data-action="add-member"/g)||[]).length,1);
  assert.ok(!element('#main').innerHTML.includes('data-action="roster-game"'));
  run("page='settings';section='connections';render()");assert.ok(!element('#main').innerHTML.includes('关于这次重建'));
  assert.match(element('#main').innerHTML,/纯 HTTP 采集/);
  run("page='overview';section='metrics';render()");assert.ok(!element('#main').innerHTML.includes('开始搭建你的工作流'));
  assert.match(element('#main').innerHTML,/今日运行与承接/);
  run("section=''");
  run("mode='live';S=fixtures.live;planDialog()");checkHtml(element('#modal-content').innerHTML);
  assert.ok(element('#modal-content').innerHTML.includes('保存为暂停计划'));
  assert.ok(element('#modal-content').innerHTML.includes('name="page_concurrency"'));
  run("S.collector={available:true,plans:[],tasks:[{id:9,kind:'search',target:'测试夹具',status:'interrupted',active:false,resumable:true,checkpoints:[{video_id:'7600000000000000001',video_title:'测试视频',video_url:'https://www.douyin.com/video/7600000000000000001',status:'reading'}],videos:1,comments:0,inserted:0,duplicate:0,revised:0,skipped:0,video_limit:1,comment_limit:10}]};checkpointDialog(9)");
  checkHtml(element('#modal-content').innerHTML);assert.ok(element('#modal-content').innerHTML.includes('从断点继续读取'));
  // A hidden input named id shadows HTMLFormElement.id in a real browser.
  // Keep the clobbered property to regress the bug found by actual UI testing.
  for(const [formId,action,values] of [
    ['plan-form','collection-plan-save',[['id',''],['name','合成计划'],['target','无畏契约陪玩']]],
    ['checkpoint-form','collector-restart',[['id','9'],['request_id','synthetic-resume']]],
    ['member-form','member',[['id','1'],['name','演示人员'],['game','无畏契约'],['service_types','新手陪练']]],
    ['review-form','review',[['id','1'],['category','buyer']]],
    ['follow-form','lead',[['id','1'],['stage','following']]]
  ]){
    const submitButton={disabled:false,isConnected:true},errorNode={textContent:'',hidden:true};
    const form={id:{tagName:'INPUT'},getAttribute:n=>n==='id'?formId:null,values,querySelector:s=>s==='.form-error'?errorNode:submitButton};
    const count=posts.length;
    await listeners.get('submit')({target:form,preventDefault(){}});
    assert.equal(posts.length,count+1,`${formId} must submit despite hidden name=id`);
    assert.ok(posts.at(-1).url.includes(`/api/${action}?`));
    assert.equal(posts.at(-1).body.id,values[0][1]);
    assert.equal(submitButton.disabled,false);assert.equal(run('busy'),false);
  }
  for(const [unit,value,expected] of [['days','7',168],['hours','3',3]]){
    const form={getAttribute:()=> 'monitor-form',values:[['window_value',value],['window_unit',unit],['target','无畏契约陪玩'],['page_concurrency','3'],['interval_seconds','60'],['include_keywords','陪练,多少钱'],['exclude_keywords','接单']],querySelector:()=>null};
    run('monitorDraftDirty=true');
    await listeners.get('submit')({target:form,preventDefault(){}});
    assert.match(posts.at(-1).url,/\/api\/monitor-save\?/);
    assert.equal(posts.at(-1).body.lookback_hours,expected);
    assert.ok(!Object.hasOwn(posts.at(-1).body,'window_unit'));
    assert.equal(posts.at(-1).body.page_concurrency,'3');
    assert.equal(posts.at(-1).body.interval_seconds,'60');
    assert.equal(posts.at(-1).body.include_keywords,'陪练,多少钱');
    assert.equal(posts.at(-1).body.exclude_keywords,'接单');
    assert.equal(run('monitorDraftDirty'),false);
  }
  await listeners.get('submit')({target:{getAttribute:()=> 'monitor-start-form',values:[],querySelector:()=>null},preventDefault(){}});
  assert.match(posts.at(-1).url,/\/api\/monitor-start\?/);
  assert.deepEqual(posts.at(-1).body,{});
  run("S=fixtures.demo;mode='demo';reviewDialog(1)");
  assert.match(element('#modal-content').innerHTML,/name="review_token"/);
  assert.match(element('#modal-content').innerHTML,/id="review-fields"[^>]*disabled/);
  await listeners.get('change')({target:{name:'fields_action',value:'confirm'}});assert.equal(element('#review-fields').disabled,false);
  await listeners.get('change')({target:{name:'fields_action',value:'keep'}});assert.equal(element('#review-fields').disabled,true);
  const humanHtml=run('humanEvidence({raw_text:"current",manual_fields:{budget:"",region:"<img src=x>"},review_history:[{category:"uncertain",reason:"<script>test",raw_text:"old <svg>",manual_fields:{budget:"100"}}]})');
  assert.match(humanHtml,/人工标记为未知/);assert.match(humanHtml,/现已修订/);assert.ok(!humanHtml.includes('<img'));assert.ok(!humanHtml.includes('<script>'));assert.ok(!humanHtml.includes('<svg>'));
  for(const action of ['keep','confirm','reset']){
    const values=[['id','1'],['category','uncertain'],['review_token','synthetic-token'],['reason','合成核对'],['fields_action',action],['fact_game','无畏契约'],['fact_region','亚服'],['fact_budget','']];
    const form={getAttribute:()=> 'review-form',values,querySelector:()=>null};
    await listeners.get('submit')({target:form,preventDefault(){}});
    const body=posts.at(-1).body;assert.equal(body.review_token,'synthetic-token');
    if(action==='keep')assert.ok(!Object.hasOwn(body,'manual_fields'));
    if(action==='confirm'){assert.equal(Object.keys(body.manual_fields).length,7);assert.equal(body.manual_fields.region,'亚服');assert.equal(body.manual_fields.budget,'');}
    if(action==='reset')assert.deepEqual(body.manual_fields,{});
    assert.ok(!Object.hasOwn(body,'fact_region'));
  }
  const errorNode={textContent:'',hidden:true},submitButton={disabled:false,isConnected:true};
  await listeners.get('submit')({target:{id:'plan-form',getAttribute:()=> 'plan-form',failFormData:true,querySelector:s=>s==='.form-error'?errorNode:submitButton},preventDefault(){}});
  assert.equal(run('busy'),false);assert.equal(submitButton.disabled,false);
  assert.equal(errorNode.hidden,false);assert.match(errorNode.textContent,/synthetic form read failure/);
  // Every secondary workspace remains renderable and uses its existing data view.
  run("S=fixtures.demo;mode='demo';query='';gameFilter='';");
  for(const [view,part] of [['overview','metrics'],['monitor','sources'],['monitor','runs'],['live','sources'],['live','runs'],['leads','detail/1'],['inbox','contact/1'],['inbox','contact/999999'],['analytics','quality'],['settings','connections']]){
    run(`page='${view}';section='${part}';render();`);checkHtml(element('#main').innerHTML);
  }
  run("page='leads';section='';render();");assert.doesNotMatch(element('#main').innerHTML,/id="lead-detail"/);
  run("page='overview';dailyTrend='demand';section='';render();");assert.equal((element('#main').innerHTML.match(/class="card daily-chart"/g)||[]).length,1);
  for(const theme of ['collection','analysis','captcha']){run(`dailyTrend='${theme}';render();`);checkHtml(element('#main').innerHTML);}
  run("dailyTrend='demand';section='';");
  run("const analyticsBefore=S;S={...fixtures.demo,leads:[{id:101,source_kind:'uid_test'},{id:102,source_kind:'comment'}],jobs:[{lead_id:101,status:'replied'},{lead_id:102,status:'accepted'}]};page='analytics';");
  const businessAnalytics=run('analytics()');assert.match(businessAnalytics,/<span>已回复<\/span><b>0<\/b>/);assert.match(businessAnalytics,/<span>服务端接受<\/span><b>1<\/b>/);run('S=analyticsBefore');
  // Personal HTTP rendering never treats configuration or server acceptance as delivery.
  run(`S=JSON.parse(JSON.stringify(fixtures.demo));mode='live';conversation=S.leads[0].id;S.leads[0].source_kind='uid_test';S.leads[0].contact_basis='opt_in';S.leads[0].contact_note='synthetic consent';S.leads[0].do_not_contact=false;S.messages=[];S.jobs=[{id:98,lead_id:conversation,content:'offline only',status:'draft'}];S.uid_messaging={can_attempt:false,issues:['<script>not configured']};`);
  run("page='inbox';S.messaging_test={status:'not_configured',issues:['legacy OpenID config'],sender:'test',recipient:'test'}");
  let httpHtml=run('inbox()');checkHtml(httpHtml);
  assert.match(httpHtml,/data-action="uid-settings"/);assert.doesNotMatch(httpHtml,/data-action="uid-http-send"/);
  run("section='contact/'+conversation");const blockedDetail=run('inbox()');assert.match(blockedDetail,/data-action="uid-http-send"[^>]*disabled/);run("section=''");
  run('uidSettingsDialog()');assert.match(element('#modal-content').innerHTML,/个人号 HTTP · 数字 UID/);
  assert.match(element('#modal-content').innerHTML,/data-action="uid-http-probe"[^>]*disabled/);
  assert.ok(!httpHtml.includes('<script>'));assert.ok(!httpHtml.includes('HTTP 单条测试'));
  run('uidTargetDialog()');
  assert.match(element('#modal-content').innerHTML,/name="uid" type="text"[^>]*inputmode="numeric"[^>]*required/);
  assert.ok(!element('#modal-content').innerHTML.includes('type="number"'),'UID must retain exact digits as text');
  run('S.uid_messaging.can_attempt=true');httpHtml=run('inbox()');
  assert.match(httpHtml,/配置就绪 · 未验收/);run("section='contact/'+conversation");const readyDetail=run('inbox()');assert.match(readyDetail,/data-action="uid-http-send"/);assert.doesNotMatch(readyDetail,/data-action="uid-http-send"[^>]*disabled/);run("section=''");
  assert.ok(!/data-action="uid-http-probe"[^>]*disabled/.test(httpHtml));
  run("const beforeProbeApi=api;let probeCalls=[];api=async(action,body)=>{probeCalls.push({action,body});return {status:'identity_verified',detail:'<script>synthetic identity only',checked_at:'2026-09-10T00:00:00Z'};};const probeButton={dataset:{action:'uid-http-probe'}};draftText='keep my draft';");
  await run('handleAction(probeButton)');
  assert.equal(run('JSON.stringify(probeCalls)'), '[{"action":"uid-http-probe","body":{}}]');
  assert.equal(run('draftText'),'keep my draft');assert.equal(run('probeButton.disabled'),false);
  assert.ok(!element('#modal-content').innerHTML.includes('<script>'));
  assert.match(element('#modal-content').innerHTML,/HTTP 登录身份核对/);
  assert.match(element('#modal-content').innerHTML,/发送时仍会重新核对/);
  run('api=beforeProbeApi');
  run(`S.semantic={can_analyze:true,config:{enabled:true,host:'127.0.0.1',port:11434,model:'synthetic:1',timeout_seconds:30}};const semRow={...S.comments[0],model_routing:{model_allowed:true},analysis_input_hash:'saved-input-hash',analysis_method:'human',model_result:{status:'completed',engine:'synthetic',detail:'<script>test detail',result:{category:'buyer',certainty:'clear',reason:'<img>test reason',facts:{evidence:[{kind:'category',source:'comment',text:'<script>test quote'}]}}}};`);
  assert.ok(!run('modelEvidence({...semRow,model_routing:{model_allowed:false}})').includes('data-action="semantic-analyze"'));
  run("const vocabularyFetch=fetch;fetch=async url=>({ok:true,json:async()=>url.includes('/api/asset-keyword?')?{term:'瓦搭',revision:1,status:'pending',kind:'service',scope:'asset',reason:'',sources:[]}:{rows:[],counts:{active:0,pending:1,total:1},limit:500}})");
  await run('assetKeywordsDialog()');assert.match(element('#modal-content').innerHTML,/作品与作者发现/);
  await run("keywordReviewDialog('瓦搭')");assert.match(element('#modal-content').innerHTML,/name="scope"/);assert.match(element('#modal-content').innerHTML,/value="asset" selected/);run('fetch=vocabularyFetch');
  const modelHtml=run('modelEvidence(semRow)');assert.ok(!modelHtml.includes('<script>'));assert.ok(!modelHtml.includes('<img>'));
  assert.match(modelHtml,/人工判断优先/);assert.match(modelHtml,/尚未测定准确率/);assert.match(modelHtml,/type="button"/);
  run('reviewDialog(semRow.id,semRow)');assert.ok(!element('#modal-content').innerHTML.includes('data-action="semantic-analyze"'),'Do not start a model call from an unsaved human review form');
  assert.match(run('semanticPanel()'),/保存不启动分析或下载模型/);
  assert.match(run('semanticPanel()'),/name="auto_analyze"/);
  assert.match(run('semanticPanel()'),/name="max_concurrency"/);
  assert.match(run('semanticPanel()'),/name="live_model_enabled"/);
  run('S.semantic.config.live_model_enabled=false');
  assert.ok(!run("modelEvidence({...semRow,evidence_type:'live'})").includes('data-action="semantic-analyze"'));
  assert.match(run("modelEvidence({...semRow,evidence_type:'live'})"),/已保存模型结果/);
  assert.match(run('modelEvidence(semRow)'),/data-action="semantic-analyze"/);
  assert.match(run('liveMonitor()'),/仅规则初筛 · 模型已关闭/);
  run('S.semantic.running=true;S.semantic.at_capacity=false');
  assert.ok(!run('modelEvidence(semRow)').includes('disabled'));
  run('S.semantic.at_capacity=true');
  assert.match(run('modelEvidence(semRow)'),/disabled/);
  run('S.semantic.running=false;S.semantic.at_capacity=false');
  run("S.semantic.config.backend='openai_compatible';S.semantic.config.api_base_url='https://api.example.test/v1';S.semantic.api_key_configured=true");
  assert.match(run('semanticPanel()'),/value="openai_compatible" selected/);
  assert.match(run('semanticPanel()'),/name="api_key" type="password" value=""/);
  assert.match(run('connectionRows()'),/远程 API 已配置/);
  run('const beforeApiFormState=S');
  for(const key of ['', 'synthetic-ui-key']){
    const form={getAttribute:()=> 'semantic-form',values:Object.entries({backend:'openai_compatible',api_base_url:'https://api.example.test/v1',api_key:key,model:'test-model',host:'127.0.0.1',port:'11434',enabled:'true',auto_analyze:'true',timeout_seconds:'60',max_concurrency:'3',live_model_enabled:'false'}),querySelector:()=>null};
    await listeners.get('submit')({target:form,preventDefault(){}});
    assert.match(posts.at(-1).url,/\/api\/semantic-save\?/);
    assert.equal(posts.at(-1).body.backend,'openai_compatible');
    assert.equal(posts.at(-1).body.auto_analyze,true);
    assert.equal(posts.at(-1).body.max_concurrency,3);
    assert.equal(posts.at(-1).body.live_model_enabled,false);
    assert.equal(posts.at(-1).body.api_key,key||undefined);
    assert.equal(run('semanticDraftDirty'),false);
  }
  run('S=beforeApiFormState');
  run("S.semantic.queue={counts:{queued:1},rows:[{id:1,evidence_type:'comment',record_id:12,status:'queued',detail:'<script>bad'}],active:1,capacity:200}");
  const queueHtml=run('modelQueuePanel()');assert.ok(!queueHtml.includes('<script>'));assert.match(queueHtml,/停止当前队列/);assert.match(queueHtml,/等待 1/);
  run('S.semantic.queue.concurrency_limit=3');
  assert.match(run('modelQueuePanel()'),/同时最多分析 3 条/);
  run("markMonitorDraft({closest:s=>s==='#semantic-form'})");assert.equal(run('semanticDraftDirty'),true);run('semanticDraftDirty=false');
  run("const semOldApi=api,semOldLoad=load;let semCalls=[];api=async(a,b)=>{semCalls.push({a,b});return {status:'completed',detail:'synthetic'};};load=async()=>{};");
  await run("handleAction({dataset:{action:'semantic-analyze',kind:'comment',id:'12',hash:'saved-hash'}})");
  assert.equal(run('semCalls[0].a'),'semantic-analyze');assert.equal(run('semCalls[0].b.id'),12);
  assert.equal(run("Object.keys(semCalls[0].b).sort().join(',')"),'evidence_type,id,input_hash,request_id');
  run('api=semOldApi;load=semOldLoad;S.semantic={can_analyze:false};');
  run("section='contact/'+conversation");
  for(const terminal of ['submitting','unknown','accepted','failed']){
    run(`S.jobs[0].status='${terminal}';S.jobs[0].uid_http={phase:'send',evidence:{server_message_id:'10000000000000001'}}`);
    httpHtml=run('inbox()');assert.ok(!httpHtml.includes('data-action="uid-http-send"'));assert.match(httpHtml,/10000000000000001/);
  }
  run("S.jobs[0].status='draft';S.leads[0].do_not_contact=true");assert.match(run('inbox()'),/data-action="uid-http-send"[^>]*disabled/);
  run(`section='';S=fixtures.live;mode='live';page='live';liveFilter='all';liveScope='current';S.collector={tasks:[],live_monitor:{config:{room_url:'https://live.douyin.com/12345',duration_seconds:60,max_messages:100,include_keywords:'陪练',exclude_keywords:''},sessions:[],current:null,rows:[{id:1,raw_text:'找陪练<script>',nickname:'<img>',uid:'10000000000000002',message_id:null,published_at:null,filter_reason:'',category:'uncertain',analysis_method:'rules',reason:'synthetic',facts:{},include_matches:[],exclude_matches:[]}],events:[],active_id:null}};render();`);
  checkHtml(element('#main').innerHTML);assert.ok(!element('#main').innerHTML.includes('<script>'));assert.ok(!element('#main').innerHTML.includes('<img>'));
  assert.match(run('liveStatus()'),/不能回看未采集的历史弹幕/);assert.match(element('#main').innerHTML,/10000000000000002/);
  run("section='sources'");assert.match(run('liveTrackingPanel()'),/开启24h监控|直播间库/);run("section=''");
  assert.match(element('#main').innerHTML,/id="live-tracking"/);assert.doesNotMatch(element('#main').innerHTML,/id="live-status"/);
  run("S.collector.live_monitor.library={counts:{total:1},rows:[{room_url:'https://live.douyin.com/12345',title:'<script>room',enabled:1,saved_messages:2}]};liveLibraryDialog();");
  assert.ok(!element('#modal-content').innerHTML.includes('<script>'));
  assert.match(element('#modal-content').innerHTML,/当前没有符合筛选/);run("liveLibraryFilter='all';liveLibraryDialog()");
  assert.match(element('#modal-content').innerHTML,/暂停关注/);
  assert.match(run('liveSettingsPanel()'),/discovery_interval_minutes|offline_retry_minutes/);
  run("markMonitorDraft({closest:s=>s==='#live-form'})");assert.equal(run('liveDraftDirty'),true);
  assert.match(run('liveStatus()'),/data-action="live-start"[^>]*disabled/);
  run("liveSearch='nonexistent'");assert.equal(run('liveRows().length'),0);
  const liveRecord={id:1,evidence_type:'live',external_id:'10000000000000099',person_id:1,lead_id:1,
    raw_text:'合成直播原文',source_url:'https://live.douyin.com/12345',source_title:'直播间 12345',source_name:'直播观察',
    category:'uncertain',game:'',analysis_method:'human',reason:'合成核对',facts:{budget:''},rule_facts:{rules_version:'rules-v2',evidence:[{source:'comment',kind:'request',text:'合成直播原文'}]},
    manual_fields:{budget:''},review_history:[],review_token:'live-only-token',parent_context:{status:'none'}};
  context.liveFixture=liveRecord;
  run('reviewDialog(1,liveFixture)');checkHtml(element('#modal-content').innerHTML);
  assert.match(element('#modal-content').innerHTML,/name="evidence_type" value="live"/);
  assert.match(element('#modal-content').innerHTML,/弹幕 ID 10000000000000099/);
  assert.match(element('#modal-content').innerHTML,/此条弹幕/);
  assert.match(run('sourceLink(liveFixture)'),/查看直播间/);assert.ok(!run('sourceLink(liveFixture)').includes('原视频'));
  const liveReviewForm={getAttribute:()=> 'review-form',values:[['id','1'],['evidence_type','live'],['category','uncertain'],['review_token','live-only-token'],['reason','合成核对'],['fields_action','reset']],querySelector:()=>null};
  await listeners.get('submit')({target:liveReviewForm,preventDefault(){}});
  assert.match(posts.at(-1).url,/\/api\/live-review\?/);assert.deepEqual(posts.at(-1).body.manual_fields,{});
  run("S=JSON.parse(JSON.stringify(fixtures.demo));S.leads[0].latest=liveFixture;S.leads[0].live_count=3;S.leads[0].comment_count=0;S.leads[0].evidence_count=3;");
  const liveLeadHtml=run('leadDetail(S.leads[0])');checkHtml(liveLeadHtml);
  assert.match(liveLeadHtml,/data-action="live-review"/);assert.match(liveLeadHtml,/查看 3 条历史/);assert.match(run('leadTable([S.leads[0]])'),/3 条弹幕/);
  assert.match(run("ruleEvidence({...liveFixture,rule_category:'uncertain',rule_reason:'合成先前规则'})"),/保留的规则分类：待判断/);
  for(const category of ['uncertain','noise','social','seller','recruit']){
    const neutral=run(`replyTemplate({category:'${category}',game:'',latest:{facts:{}}})`);
    assert.ok(!neutral.includes('无畏契约'));assert.ok(!neutral.includes('陪玩'));assert.ok(!neutral.includes('预算'));
  }
  assert.match(run("replyTemplate({category:'buyer',game:'无畏契约',latest:{facts:{service_type:'对局复盘'}}})"),/对局复盘/);
  run("mode='live';S=JSON.parse(JSON.stringify(fixtures.live));collectorDialog(null,{kind:'author',target:'7619966169662950656',transport:'http',video_limit:2})");
  checkHtml(element('#modal-content').innerHTML);
  assert.match(element('#modal-content').innerHTML, /value="author" selected/);
  assert.match(element('#modal-content').innerHTML, /发现后最多读取视频数/);
  console.log('PASS: 20 page states, dialogs, author discovery option, escaping, filters, draft isolation, form recovery, live stream and HTTP UID rendering. Synthetic only.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>run('clearTimeout(collectionPollTimer);clearTimeout(toastTimer);'));
