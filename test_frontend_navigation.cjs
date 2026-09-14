'use strict';
// Route transitions must retain context and never dispatch collection or messages.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8'),events=new Map();
const context=vm.createContext({location:{hash:'#monitor'},window:{addEventListener:(name,fn)=>events.set(name,fn),scrollTo(){}},console,$:()=>({focus(){}})});
const run=code=>vm.runInContext(code,context);
run(`let page='monitor',section='',S={view:'monitor'},query='保留搜索',gameFilter='保留筛选',monitorDraftDirty=false,liveDraftDirty=false,semanticDraftDirty=false;
  const pages={monitor:[],live:[],groups:[],leads:[],overview:[],inbox:[]};let renders=0,loads=0,stashes=0,polls=0;
  const render=()=>renders++,load=async()=>loads++,stashDraft=()=>stashes++,queueCollectionPoll=()=>polls++,setMobileNavigation=()=>{},loadError=e=>{throw e;};`);
run(source.match(/const sectionSets=.+;/)[0]);
const routeStart=source.indexOf("window.addEventListener('hashchange'");
run(source.slice(routeStart,source.indexOf('\n});',routeStart)+4));
const navigate=hash=>{context.location.hash=hash;events.get('hashchange')();};
navigate('#monitor/sources');
assert.equal(run('section'),'sources');assert.equal(run('loads'),1);assert.equal(run('renders'),0);
assert.equal(run('query'),'保留搜索');assert.equal(run('gameFilter'),'保留筛选');
navigate('#monitor/runs');assert.equal(run('loads'),2);assert.equal(run('section'),'runs');
navigate('#live/sources');assert.equal(run('page'),'live');assert.equal(run('section'),'sources');assert.equal(run('loads'),3);
run("page='leads';section='';S={view:'leads'};query='三十七';gameFilter='无畏契约';");
navigate('#leads/detail/17');assert.equal(run('section'),'detail/17');assert.equal(run('loads'),4);
navigate('#leads');assert.equal(run('query'),'三十七');assert.equal(run('gameFilter'),'无畏契约');
navigate('#leads/nonexistent');assert.equal(run('section'),'');
navigate('#unknown/path');assert.equal(run('page'),'overview');assert.equal(run('section'),'');
assert.ok(run('stashes')>=7,'Draft context is preserved before every route change');
console.log('PASS: section-scoped data navigation, channel boundaries, list filter preservation, unknown-route fallback and draft stashing. No network or mutation.');

context.scrollNodes=[{scrollTop:120,scrollLeft:0},{scrollTop:560,scrollLeft:25}];
context.document={querySelectorAll:()=>context.scrollNodes};
run("const viewScrollCache=new Map(),scrollTargets='synthetic';let renderedViewKey='list-filter-A';");
for(const name of ['rememberViewScroll','restoreViewScroll'])run(source.split('\n').find(line=>line.startsWith('function '+name+'(')));
run('rememberViewScroll();restoreViewScroll("detail-17")');assert.deepEqual(context.scrollNodes.map(n=>n.scrollTop),[0,0]);
run('restoreViewScroll("list-filter-A")');assert.deepEqual(context.scrollNodes.map(n=>n.scrollTop),[120,560]);
assert.equal(context.scrollNodes[1].scrollLeft,25);
console.log('PASS: list/detail scroll restoration and a new filter starting at the top.');

// Both HTML entry points must show the same navigation before data arrives.
const navOf=file=>fs.readFileSync(file,'utf8').match(/<nav id="nav"[\s\S]*?<\/nav>/)[0];
const navEntries=html=>[...html.matchAll(/<a href="([^"]+)"[^>]*><i data-lucide="([^"]+)"><\/i>([^<]+)<\/a>/g)].map(([,href,icon,label])=>[href.replace('/#','#'),icon,label]);
assert.deepEqual(navEntries(navOf('static/app.html')),navEntries(navOf('static/login.html')));
assert.equal([...navOf('static/login.html').matchAll(/aria-current="page"/g)].length,1);
assert.ok(!navEntries(navOf('static/login.html')).some(([href])=>['#live','#groups'].includes(href)));
