'use strict';
// Isolated UI requests. No platform, browser or database.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const events=new Map(),windows=new Map(),nodes=new Map(),timers=new Map(),requests=[];let clock=0;
const el=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',className:'',focus(){},setSelectionRange(){},addEventListener(){},setAttribute(){},classList:{remove(){},add(){},toggle(){}}});return nodes.get(id);};
const context=vm.createContext({AbortController,console,location:{hash:'#leads'},
 document:{querySelector:el,querySelectorAll:()=>[],addEventListener:(k,v)=>events.set(k,v),body:el('body')},
 window:{addEventListener:(k,v)=>windows.set(k,v),scrollTo(){}},
 setTimeout:(fn,ms)=>{timers.set(++clock,{fn,ms});return clock;},clearTimeout:id=>timers.delete(id),
 fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))});
const run=code=>vm.runInContext(code,context);
run(fs.readFileSync('static/app.js','utf8'));
run("render=()=>{$('#main').innerHTML=leads();};queueCollectionPoll=()=>{};icons=()=>{};rememberViewScroll=()=>{};stashDraft=()=>{};setMobileNavigation=()=>{};");
const tick=async()=>{await new Promise(r=>setImmediate(r));};
const fire=async()=>{const [id,t]=[...timers].find(([,t])=>t.ms<1000)||[];assert.ok(t,'A bounded search/page request is pending');timers.delete(id);t.fn();await tick();};
const payload=(page,total=63,marker='row')=>({view:'leads',leads:[{id:page,nickname:marker,external_id:'test',game:'无畏契约',category:'buyer',stage:'new',comment_count:1,source_kind:'comment',latest:{raw_text:marker,facts:{},analysis_method:'human'}}],comments:[],stats:{pending:0},collector:{},lead_list:{page,pages:Math.max(1,Math.ceil(total/30)),page_size:30,total,start:total?(page-1)*30+1:0,end:Math.min(page*30,total),comment_count:100,unlinked_count:0}});
const complete=async(req,value)=>{req.resolve({ok:true,json:async()=>value});await tick();};
(async()=>{
 assert.match(requests[0].url,/lead_page=1&lead_page_size=30&lead_tab=buyer/);
 await complete(requests[0],payload(1));
 await run("handleAction({dataset:{action:'lead-page',id:'2'}})");
 assert.doesNotMatch(el('#main').innerHTML,/<table/,'Old rows are not shown under pending page/filter labels');
 assert.match(el('#main').innerHTML,/aria-busy="true"/);await fire();
 const old=requests.at(-1);assert.match(old.url,/lead_page=2/);
 const input=value=>events.get('input')({target:{id:'lead-search',value,selectionStart:value.length}});
 input('冷门');input('冷门昵称');
 assert.equal(old.options.signal.aborted,true);
 assert.equal([...timers.values()].filter(t=>t.ms===300).length,1,'Typing is debounced');
 await fire();const fresh=requests.at(-1);
 assert.match(fresh.url,/lead_page=1/);assert.match(decodeURIComponent(fresh.url),/lead_query=冷门昵称/);
 await complete(fresh,payload(1,1,'精准结果'));
 await complete(old,payload(2,63,'过期结果'));
 assert.equal(run('S.leads[0].nickname'),'精准结果','A response from the old page cannot replace a new search');
 assert.match(el('#main').innerHTML,/共 1 位用户/);assert.doesNotMatch(el('#main').innerHTML,/过期结果/);
 input('读取失败');await fire();requests.at(-1).reject(Error('模拟读取失败'));await tick();
 assert.match(el('#main').innerHTML,/模拟读取失败/);assert.doesNotMatch(el('#main').innerHTML,/<table/);
 await run("handleAction({dataset:{action:'lead-retry'}})");await fire();await complete(requests.at(-1),payload(1));
 await run("handleAction({dataset:{action:'lead-tab',tab:'all'}})");await fire();
 assert.match(requests.at(-1).url,/lead_tab=all/);await complete(requests.at(-1),payload(1));
 run('leadPage=3');await run("handleAction({dataset:{action:'lead-page',id:'3'}})");await fire();
 await complete(requests.at(-1),payload(2,35));assert.equal(run('leadPage'),2,'Shrinking lists clamp the selected page');
 run("location.hash='#leads/detail/2'");windows.get('hashchange')();
 assert.match(requests.at(-1).url,/lead_id=2/);assert.doesNotMatch(requests.at(-1).url,/lead_page=/);
 run("location.hash='#leads'");windows.get('hashchange')();
 assert.match(requests.at(-1).url,/lead_page=2/);assert.match(requests.at(-1).url,/lead_tab=all/);
 await complete(requests.at(-1),payload(2,35));
 await events.get('change')({target:{id:'published-filter',value:'custom'}});
 assert.match(el('#main').innerHTML,/请至少选择/);assert.equal([...timers.values()].filter(t=>t.ms<1000).length,0,'Invalid date input never triggers a broad query');
 await run("handleAction({dataset:{action:'clear-lead-filters'}})");await fire();
 assert.match(requests.at(-1).url,/lead_page=1/);assert.match(requests.at(-1).url,/lead_query=&/);
 const reads=requests.length;await run("handleAction({dataset:{action:'live-open-lead',id:'6500'}})");
 assert.equal(context.location.hash,'leads/detail/6500','Live evidence opens the exact demand, even when absent from the first page');
 assert.equal(requests.length,reads,'Cross-source navigation does not wait on another full archive read');
 console.log('PASS: full-list request scope, debounce, stale response, error recovery, page clamp, detail return and invalid dates. Synthetic only.');
})().catch(e=>{console.error(e);process.exitCode=1;});
