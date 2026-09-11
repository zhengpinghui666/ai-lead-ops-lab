'use strict';
// Deterministic local UI logic; no real records, browser or platform requests.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const events=new Map(),elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{focus(){},addEventListener(){},innerHTML:'',textContent:''});return elements.get(id);};
const context=vm.createContext({document:{querySelector:element,addEventListener:(type,fn)=>events.set(type,fn)},location:{hash:'#leads'},window:{addEventListener(){}},fetch:()=>new Promise(()=>{}),setTimeout:()=>1,clearTimeout(){},console});
vm.runInContext(fs.readFileSync('static/app.js','utf8'),context);
const run=code=>vm.runInContext(code,context),reference=Date.parse('2026-09-09T12:00:00Z');
context.reference=reference;
run("let renders=0;render=()=>{renders++;};");
function matches(filter,time){run(`publishedFilter=${JSON.stringify(filter)}`);return run(`publicationMatches({published_at:${JSON.stringify(time)}},reference)`);}

for(const [filter,hours] of [['hour',1],['day',24],['week',168],['month',720]]){
  assert.equal(matches(filter,new Date(reference-hours*3600000).toISOString()),true);
  assert.equal(matches(filter,new Date(reference-hours*3600000-1).toISOString()),false);
  assert.equal(matches(filter,new Date(reference).toISOString()),true);
  assert.equal(matches(filter,new Date(reference+1).toISOString()),false);
  assert.equal(matches(filter,null),false);
  assert.equal(matches(filter,'bad-time'),false);
}
assert.equal(matches('unknown',null),true);assert.equal(matches('unknown','bad-time'),true);
assert.equal(matches('unknown','2026-09-09T10:00:00+08:00'),false);
assert.equal(matches('future','2026-09-09T12:00:01Z'),true);
assert.equal(matches('future','2026-09-09T12:00:00Z'),false);
assert.equal(matches('all',null),true);
run("publishedFilter='day'");
assert.equal(run("publicationMatches({published_at:'2026-01-01T00:00:00Z',discovered_at:'2026-09-09T11:59:00Z'},reference)"),false,'Recent ingestion cannot make an old comment recent');

run("publishedFilter='custom';publishedFrom='2026-09-09';publishedUntil='2026-09-09'");
for(const [time,expected] of [['2026-09-08T15:59:59.999Z',false],['2026-09-08T16:00:00Z',true],['2026-09-09T15:59:59.999Z',true],['2026-09-09T16:00:00Z',false]]){
  assert.equal(run(`publicationMatches({published_at:'${time}'},reference)`),expected,'Inclusive Beijing date boundaries');
}
run("publishedFrom='2026-09-10'");assert.match(run('publicationError()'),/不能晚于/);
assert.equal(run("publicationMatches({published_at:'2026-09-09T12:00:00Z'},reference)"),false);
run("publishedFrom='';publishedUntil=''");assert.match(run('publicationError()'),/至少选择/);
for(const day of ['2026-02-30','2026-13-01','2026-9-9','<script>'])assert.equal(run(`dateBound(${JSON.stringify(day)})`),null);
assert.notEqual(run("dateBound('2024-02-29')"),null);
run("publishedFrom='2026-09-09';publishedUntil=''");assert.equal(run('publicationError()'),'');
assert.equal(run("publicationMatches({published_at:'2026-09-10T12:00:00Z'},reference)"),true,'Custom range does not silently impose a different upper date');
assert.equal(run("date('bad-time')"),'未提供');assert.equal(run("age('bad-time')"),'发布时间未知');
assert.match(run("date('2026-09-09T03:00:00Z')"),/2026/);
assert.match(run("date('2026-09-09T03:00:00Z')"),/11:00/);

run(`const earlier={id:1,nickname:'合成甲',external_id:'test-a',game:TARGET_GAME,category:'uncertain',latest:{published_at:'2026-09-09T10:00:00+08:00',discovered_at:'2026-09-10T00:00:00Z',facts:{}}};
const later={id:2,nickname:'合成乙',external_id:'test-b',game:TARGET_GAME,category:'buyer',latest:{published_at:'2026-09-09T03:00:00Z',discovered_at:'2026-09-09T04:00:00Z',facts:{}}};
const unknown={id:3,nickname:'合成丙',external_id:'test-c',game:TARGET_GAME,category:'uncertain',latest:{published_at:null,discovered_at:'2026-09-09T05:00:00Z',facts:{}}};
S={leads:[earlier,later,unknown],comments:[]};resetLeadFilters();`);
assert.equal(run('filteredLeads().map(l=>l.id).join()'),'2,1,3');
run("leadSort='discovered'");assert.equal(run('filteredLeads().map(l=>l.id).join()'),'1,3,2');
assert.equal(run('filteredLeads(["buyer"]).map(l=>l.id).join()'),'2');
run("publishedFilter='unknown'");assert.equal(run('filteredLeads().map(l=>l.id).join()'),'3');
assert.equal(run('S.leads.length'),3,'Filtering never deletes original records');

run(`S.comments=[{game:TARGET_GAME,published_at:'2026-09-09T11:00:00Z'},{game:TARGET_GAME,published_at:'2026-09-03T12:00:00Z'},{game:TARGET_GAME,published_at:'2026-01-01T00:00:00Z',discovered_at:'2026-09-09T11:00:00Z'},{game:'',published_at:null},{game:TARGET_GAME,published_at:'2026-09-10T12:00:00Z'},{game:'其他游戏',published_at:'2026-09-09T11:00:00Z'}];`);
const summary=run('publicationSummary(reference)');assert.match(summary,/近 24 小时发布 <b>1<\/b>/);assert.match(summary,/近 7 天 <b>2<\/b>/);assert.match(summary,/时间未知 <b>1<\/b>/);assert.match(summary,/未来时间待核对 <b>1<\/b>/);

(async()=>{
  await events.get('change')({target:{id:'published-filter',value:'custom'}});
  assert.equal(run('publishedFilter'),'custom');assert.equal(run('selected'),null);assert.equal(run('renders'),1);
  assert.match(run('filterBar()'),/id="published-from"/);
  assert.match(run('filterBar()'),/id="lead-sort"/);
  await events.get('change')({target:{id:'published-from',value:'2026-09-09'}});
  assert.equal(run('publishedFrom'),'');assert.equal(run('renders'),1,'Typing a date must not replace its native input');
  element('#published-from').value='2026-09-09';element('#published-until').value='2026-09-09';
  await run("handleAction({dataset:{action:'apply-published-range'}})");
  assert.equal(run('publishedFrom'),'2026-09-09');assert.equal(run('publishedUntil'),'2026-09-09');assert.equal(run('renders'),2);
  await run("handleAction({dataset:{action:'clear-lead-filters'}})");
  assert.equal(run('publishedFilter'),'all');assert.equal(run('leadSort'),'published');assert.equal(run('publishedFrom'),'');
  assert.ok(!run('filterBar()').includes('id="published-from"'));
  console.log('PASS: publication windows, Beijing inclusive dates, unknown/future separation, numeric ordering, local filter events and no data deletion. Synthetic only.');
})().catch(e=>{console.error(e);process.exitCode=1;});
