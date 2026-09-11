'use strict';
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
function run(scenario,concurrency=2){return new Promise((resolve,reject)=>{
  const child=spawn(process.execPath,[path.join(__dirname,'collector_runner.cjs')],{cwd:__dirname,windowsHide:true,env:{...process.env,CLUBOPS_PLAYWRIGHT:path.join(__dirname,'tests/fixtures/playwright_parallel_fixture.cjs'),CLUBOPS_FIXTURE_SCENARIO:scenario},stdio:['pipe','pipe','pipe']});
  const rows=[];let pending='',errors='',gateIndex=-1,cancelSent=false;
  const timeout=setTimeout(()=>{child.kill();reject(Error(`${scenario}: timeout`));},26000);
  child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');child.stderr.on('data',s=>errors+=s);
  child.stdout.on('data',chunk=>{pending+=chunk;const lines=pending.split('\n');pending=lines.pop();for(const line of lines){if(!line)continue;const r=JSON.parse(line);rows.push(r);
    if(r.type==='status'&&r.status==='needs_verification'){
      gateIndex=rows.length-1;
      if(scenario==='gate-resume'||scenario==='gate-persistent')setTimeout(()=>child.stdin.write('{"command":"resume"}\n'),100);
      if(scenario==='gate-close')child.stdin.write('{"command":"close_window"}\n');
    }
    if(scenario==='cancel-parallel'&&r.type==='parallel'&&r.active_pages===2&&!cancelSent){cancelSent=true;child.stdin.write('{"command":"cancel"}\n');}
  }});
  child.on('error',reject);child.on('close',code=>{clearTimeout(timeout);if(code!==0)reject(Error(`${scenario}: ${errors}`));else resolve({rows,gateIndex});});
  child.stdin.write(JSON.stringify({kind:'search',target:'合成测试，不访问平台',video_limit:3,comment_limit:2,page_concurrency:concurrency,interactive:true,profile_dir:'synthetic-only'})+'\n');
});}
const terminal=r=>r.rows.filter(x=>x.type==='status').at(-1)?.status;
(async()=>{
  const scenarios=['success','gate-resume','gate-persistent','gate-close','gate-rate-limit','rate-limit','wrong-video','cancel-parallel','navigation-rate-limit','navigation-access-denied','navigation-upstream'];
  const results=await Promise.all(scenarios.map(s=>run(s)));
  for(const [index,r] of results.entries()){
    const name=scenarios[index],metrics=r.rows.filter(x=>x.type==='parallel');
    assert.ok(metrics.some(x=>x.peak_pages===2),`${name} overlaps two video readers`);
    assert.ok(metrics.every(x=>x.active_pages<=2&&x.peak_pages<=2));assert.equal(metrics.at(-1).active_pages,0);
    for(const c of r.rows.filter(x=>x.type==='comment'))assert.ok(c.record.text.includes(c.record.video_id),`${name} must not mix video identity`);
    if(['success','gate-resume'].includes(name)){
      assert.equal(terminal(r),'completed',name);assert.equal(r.rows.filter(x=>x.type==='comment').length,6);
      assert.equal(r.rows.filter(x=>x.type==='checkpoint'&&x.status==='done').length,3);
    }else{
      const expected={'gate-persistent':'no_data','gate-close':'interrupted','gate-rate-limit':'rate_limited','rate-limit':'rate_limited','wrong-video':'schema_changed','cancel-parallel':'cancelled','navigation-rate-limit':'rate_limited','navigation-access-denied':'access_denied','navigation-upstream':'network_error'};
      assert.equal(terminal(r),expected[name],name);
      if(name!=='wrong-video')assert.ok(!r.rows.some(x=>x.type==='fixture'&&x.action==='goto'&&x.video_id.endsWith('103')),`${name}: no new target after gate/stop`);
    }
    if(name==='gate-resume'){
      const firstResume=r.rows.findIndex((x,i)=>i>r.gateIndex&&x.type==='status'&&x.status==='running');
      assert.ok(firstResume>r.gateIndex);
      assert.ok(!r.rows.slice(r.gateIndex,firstResume).some(x=>x.type==='fixture'&&['goto','wheel'].includes(x.action)));
    }
    if(name.startsWith('navigation-')){
      const denied=r.rows.findIndex(x=>x.type==='fixture'&&x.action==='navigation-denied');
      assert.ok(denied>=0);
      assert.ok(!r.rows.slice(denied+1).some(x=>x.type==='comment'||x.type==='fixture'&&['goto','wheel'].includes(x.action)),'Navigation limits stop later admissions and buffered commits immediately');
    }
  }
  const serial=await run('success',1);assert.equal(terminal(serial),'completed');assert.ok(serial.rows.filter(x=>x.type==='parallel').every(x=>x.peak_pages===1));
  console.log('PASS: 12 isolated multi-page process scenarios: serial/parallel, correlation, shared verification, persistent gate, closure, response/navigation limits, gateway failures and cancel. No live access.');
})().catch(e=>{console.error(e);process.exitCode=1;});
