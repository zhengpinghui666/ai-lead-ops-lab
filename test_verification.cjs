'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const {createVerification}=require('./collector_verification.cjs');
const {movement}=require('./captcha_browser.cjs');
const {blockFromBody}=require('./collector_parser.cjs');

async function units(){
  assert.equal(blockFromBody({status_code:0,search_nil_info:{search_nil_type:'verify_check'}}),'needs_verification');
  assert.equal(blockFromBody({verify_data:{}}),'needs_verification');
  assert.equal(blockFromBody({comments:[]}),'');
  const challenge={background_size:[360,190],target_size:[36,36],boxes:{background:{x:100,y:100,width:180,height:95},target:{x:100,y:123.5,width:18,height:18},handle:{x:100,y:210,width:30,height:30}}};
  const prediction={coordinate_type:'center_xy_in_image_pixels',background_size:[360,190],target_size:[36,36],result:{target:[184,65]}};
  assert.equal(movement(challenge,prediction).dx,83);
  assert.equal(movement(challenge,{...prediction,coordinate_type:'bbox'}),null);
  assert.equal(movement(challenge,{...prediction,result:{target:[184,99]}}),null);
  const events=[],reader={page:{},readVersion:()=>5,settleVerification:async()=>{},readableAfter:async()=>false};
  const flow=createVerification({config:{captcha:{mode:'auto',python:'fake'}},emit:async r=>events.push(r.event),status:async()=>{},check:()=>{},stopping:()=>false},{capture:async()=>({payload:{},adapter:'test'}),solve:async()=>({status:'predicted'}),submit:async()=>({submitted:true}),delay:async()=>{}});
  assert.equal(await flow(reader),false,'A closed popup without new response never resumes');
  assert.equal(events.at(-1).reason,'acceptance_not_observed');
  await flow(reader);assert.equal(events.at(-1).reason,'batch_attempt_limit');
  assert.equal(events.filter(e=>e.phase==='submitting').length,1);
}

function run(scenario){return new Promise((resolve,reject)=>{
  const python=path.join(__dirname,'.tools/ddddocr-eval/venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
  if(!fs.existsSync(python))return resolve({scenario,skipped:'optional ddddocr runtime is not installed'});
  const child=spawn(process.execPath,[path.join(__dirname,'collector_worker.cjs')],{cwd:__dirname,windowsHide:true,
    env:{...process.env,CLUBOPS_PLAYWRIGHT:path.join(__dirname,'tests/fixtures/playwright_verification_fixture.cjs'),CLUBOPS_FIXTURE_SCENARIO:scenario},stdio:['pipe','pipe','pipe']});
  const rows=[];let pending='',error='';
  const timer=setTimeout(()=>{child.kill();reject(Error(scenario+': timeout'));},32000);
  child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');child.stderr.on('data',s=>error+=s);
  child.stdout.on('data',s=>{pending+=s;const lines=pending.split('\n');pending=lines.pop();for(const line of lines){if(!line)continue;const row=JSON.parse(line);rows.push(row);
    if(scenario==='cancel'&&row.type==='verification'&&row.event.phase==='recognizing')child.stdin.write('{"command":"cancel"}\n');
  }});
  child.on('error',reject);child.on('close',code=>{clearTimeout(timer);code===0?resolve({scenario,rows}):reject(Error(error));});
  child.stdin.write(JSON.stringify({kind:scenario.startsWith('note-')?'video':'search',target:scenario.startsWith('note-')?'https://www.douyin.com/video/7600000000000000801':'合成集成测试',interactive:false,video_limit:1,comment_limit:2,page_concurrency:1,
    profile_dir:'synthetic-only',captcha:{mode:'auto',python:scenario==='missing-dependency'?path.join(__dirname,'nonexistent-python'):python}})+'\n');
});}

(async()=>{
  await units();
  const scenarios=['accepted','rejected','no-response','old-response','unsupported','changed','second-challenge','cancel','missing-dependency','iframe-accepted','iframe-stays','note-accepted','note-wrong-page'];
  const results=await Promise.all(scenarios.map(run));
  for(const {scenario,rows,skipped} of results){
    if(skipped){console.log('SKIP: '+scenario+': '+skipped);continue;}
    const phases=rows.filter(r=>r.type==='verification').map(r=>r.event);
    const last=rows.filter(r=>r.type==='status').at(-1)?.status;
    assert.equal(rows.filter(r=>r.type==='fixture'&&r.action==='headless-browser').length,1);
    assert.equal(rows.filter(r=>r.type==='fixture'&&r.action==='visible-browser').length,0);
    assert.ok(rows.filter(r=>r.type==='fixture'&&r.action==='mouse-down').length<=1);
    if(scenario==='accepted'||scenario==='iframe-accepted'||scenario==='note-accepted'){
      assert.equal(last,'completed',scenario+': '+JSON.stringify(phases));assert.equal(rows.filter(r=>r.type==='comment').length,1);
      assert.deepEqual(phases.map(e=>e.phase),['detected','capturing','recognizing','submitting','verifying','accepted']);
      if(scenario==='iframe-accepted')assert.equal(phases.find(e=>e.phase==='recognizing').adapter,'douyin_iframe_slider');
    }else if(scenario==='cancel')assert.equal(last,'cancelled');
    else{
      assert.equal(last,'needs_verification',scenario);
      assert.equal(rows.filter(r=>r.type==='comment').length,0);
      const expected={rejected:'acceptance_not_observed','no-response':'acceptance_not_observed','old-response':'acceptance_not_observed',unsupported:'background_missing',changed:'challenge_changed','second-challenge':'batch_attempt_limit','missing-dependency':'dependency_missing','iframe-stays':'acceptance_not_observed'};
      assert.equal(phases.at(-1).reason,scenario==='note-wrong-page'?'acceptance_not_observed':expected[scenario]);
      if(scenario!=='second-challenge')assert.ok(!phases.some(e=>e.phase==='accepted'));
    }
  }
  console.log('PASS: workflow units and thirteen collector process scenarios, including note identity checks. Browser is synthetic; installed ddddocr worker is real. No platform traffic.');
})().catch(error=>{console.error(error);process.exitCode=1;});
