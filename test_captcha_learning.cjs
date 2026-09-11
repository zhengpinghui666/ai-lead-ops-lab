'use strict';
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),assert=require('node:assert/strict'),{randomUUID}=require('node:crypto');
const learning=require('./captcha_learning.cjs'),{createVerification}=require('./collector_verification.cjs');
let passed=0;const pass=name=>{passed++;console.log('PASS '+name);};
(async()=>{
  const dataDir=await fs.mkdtemp(path.join(os.tmpdir(),'clubops-learning-'));
  const options={dataDir},config={id:46,profile_dir:path.join(dataDir,'browser-profile')};
  const archive=path.join(dataDir,'private/captcha-learning');
  const confirmed={platformVerdict:{status:'passed',source:'platform_response_message'},readRecovered:true};
  const readImage=async name=>fs.readFile(path.join(__dirname,'tests/fixtures/captcha',name));
  const [target,background,point]=await Promise.all(['target.png','background.png','point-pair.png'].map(readImage));
  const size=raw=>[raw.readUInt32BE(16),raw.readUInt32BE(20)];
  const slider={adapter:'legacy_slider_dom',payload:{method:'slide_match',target_image:target.toString('base64'),background_image:background.toString('base64')},cookie:'DO_NOT_STORE',url:'https://secret.invalid'};
  const prediction={status:'predicted',coordinate_type:'center_xy_in_image_pixels',background_size:size(background),target_size:size(target),result:{target:[180,70]},cookie:'DO_NOT_STORE'};
  const record=async receipt=>JSON.parse(await fs.readFile(path.join(archive,'attempts',receipt.attemptId+'.json'),'utf8'));
  try{
    assert.equal(await learning.begin(slider,config,randomUUID(),options),null);
    assert.equal((await learning.status(options)).enabled,false);pass('new installs are disabled');
    await learning.configure(true,options);
    assert.equal(await learning.begin(slider,{...config,profile_dir:'some-other-profile'},randomUUID(),options),null);
    assert.equal(await learning.begin(slider,config,'../escape',options),null);pass('dedicated profile and attempt identity required');
    const first=await learning.begin(slider,config,randomUUID(),options);assert.ok(first.attemptId);
    assert.equal((await learning.status(options)).cases,1);
    assert.equal((await fs.readdir(path.join(archive,'cases'))).length,3);
    assert.equal(await learning.finish(first,{outcome:'needs_review',reason:'acceptance_not_observed',submissions:1,prediction,elapsed_ms:80}),true);
    assert.equal(await learning.recalled(slider,config,options),null);pass('both slider images saved; unconfirmed result is not a successful label');
    const second=await learning.begin(slider,config,randomUUID(),options);
    await learning.finish(second,{...confirmed,outcome:'read_recovered',submissions:1,prediction,elapsed_ms:90});
    assert.equal((await learning.status(options)).cases,1);assert.equal((await learning.status(options)).attempts,2);
    const recalled=await learning.recalled(slider,config,options);
    assert.deepEqual(recalled.result,prediction.result);assert.equal(recalled.prediction_basis,'exact_image_platform_confirmed');
    assert.equal(await learning.finish(second,{outcome:'needs_review',submissions:1}),false);pass('deduplicated images preserve distinct attempts and immutable outcomes');
    const changed={...slider,payload:{...slider.payload,target_image:Buffer.concat([target,Buffer.from('changed')]).toString('base64')}};
    assert.equal(await learning.recalled(changed,config,options),null);
    assert.equal(await learning.recalled({...slider,adapter:'different_dom'},config,options),null);pass('different pixels or adapter never reuse an answer');
    const conflict=await learning.begin(slider,config,randomUUID(),options);
    await learning.finish(conflict,{...confirmed,outcome:'read_recovered',submissions:1,prediction:{...prediction,result:{target:[190,70]}}});
    assert.equal(await learning.recalled(slider,config,options),null);pass('conflicting successful coordinates decline recall');
    const points={adapter:'douyin_same_shape_pair',payload:{method:'same_shape_pair',image:point.toString('base64')}};
    const pointReceipt=await learning.begin(points,config,randomUUID(),options);
    const pointPrediction={status:'predicted',coordinate_type:'xy_in_image_pixels',image_size:size(point),result:{points:[[40,40],[330,190]]}};
    await learning.finish(pointReceipt,{...confirmed,outcome:'read_recovered',submissions:1,prediction:pointPrediction});
    assert.deepEqual((await learning.recalled(points,config,options)).result,pointPrediction.result);pass('point-selection samples share the same evidence store');
    const unknown=await learning.begin({adapter:'new_dom',evidence_payload:{method:'observed_images',images:[point.toString('base64')]}},config,randomUUID(),options);
    await learning.finish(unknown,{outcome:'needs_review',reason:'unsupported_type',submissions:0});
    assert.equal((await record(unknown)).method,'observed_images');
    const unavailable=await learning.begin({adapter:'new_dom',reason:'image_pixels_unavailable'},config,randomUUID(),options);
    await learning.finish(unavailable,{outcome:'needs_review',reason:'image_pixels_unavailable',submissions:0});
    assert.equal((await record(unavailable)).case_id,null);pass('unsupported images and missing-image observations remain explicitly separate');
    const noInput=await learning.begin(points,config,randomUUID(),options);
    await learning.finish(noInput,{outcome:'read_recovered',submissions:0,prediction:pointPrediction});
    assert.equal((await record(noInput)).outcome,'interrupted');pass('a no-input event cannot be labelled recovered');
    for(const verdict of [{status:'unknown',source:'none'},{status:'failed',source:'platform_response_message'},{status:'passed',source:'ocr_prediction'}]){
      const unproven=await learning.begin(points,config,randomUUID(),options);
      await learning.finish(unproven,{outcome:'read_recovered',readRecovered:true,platformVerdict:verdict,submissions:1,prediction:pointPrediction});
      assert.equal((await record(unproven)).passed,false);assert.notEqual((await record(unproven)).outcome,'read_recovered');
    }
    pass('read recovery without an explicit platform pass is never a successful record');
    const files=await fs.readdir(path.join(archive,'attempts'));
    const raw=(await Promise.all(files.map(n=>fs.readFile(path.join(archive,'attempts',n),'utf8')))).join('');
    assert.ok(!raw.includes('DO_NOT_STORE')&&!raw.includes('secret.invalid')&&!raw.includes(target.toString('base64')));
    assert.equal((await record(second)).human_label,null);pass('metadata whitelist excludes credentials, URLs and image bodies');
    const flags=path.join(dataDir,'private/captcha-learning.json');
    const settings=JSON.parse(await fs.readFile(flags,'utf8'));
    await fs.writeFile(flags,JSON.stringify({...settings,max_cases:3}));
    assert.equal((await learning.begin(changed,config,randomUUID(),options)).reason,'image_capacity_reached');
    assert.equal((await learning.status(options)).cases,3);
    assert.equal((await learning.status(options)).capacity_reached,true);
    const atLimit=await learning.begin(points,config,randomUUID(),options);assert.ok(atLimit.attemptId);
    await learning.finish(atLimit,{outcome:'needs_review',reason:'acceptance_not_observed',submissions:1});pass('capacity preserves old samples; repeated images and final results still record');
    assert.equal(await learning.recalled(points,config,options),null);
    const latestSample=JSON.parse(await fs.readFile(path.join(archive,'cases',(await record(atLimit)).case_id+'.json'),'utf8'));
    assert.equal(latestSample.latest_result,'not_passed');assert.equal(latestSample.latest_attempt_id,atLimit.attemptId);
    assert.equal((await record(pointReceipt)).passed,true);pass('latest failure invalidates recall while preserving historical confirmed outcomes');
    await fs.writeFile(flags,JSON.stringify({...settings,max_bytes:1024}));
    assert.equal((await learning.begin(changed,config,randomUUID(),options)).reason,'image_capacity_reached');pass('byte quota is enforced before new image writes');
    await fs.writeFile(flags,JSON.stringify(settings));
    const parallel=await Promise.all(Array.from({length:4},()=>learning.begin(changed,config,randomUUID(),options)));
    assert.equal(parallel.filter(r=>r?.attemptId).length,1);
    assert.equal((await learning.status(options)).cases,4);pass('concurrent writers cannot exceed case accounting');
    for(const receipt of parallel.filter(r=>r?.attemptId))await learning.finish(receipt,{outcome:'interrupted',submissions:0});
    const previous=process.env.CLUBOPS_DATA_DIR;process.env.CLUBOPS_DATA_DIR=dataDir;
    try{
      const reconfirmed=await learning.begin(points,config,randomUUID(),options);
      await learning.finish(reconfirmed,{...confirmed,outcome:'read_recovered',submissions:1,prediction:pointPrediction});
      const events=[],reader={page:{},readVersion:()=>1,settleVerification:async()=>{},readableAfter:async()=>true};
      let solveCalls=0,submitCalls=0;
      const flow=createVerification({config:{...config,captcha:{mode:'auto',python:'fake'}},emit:async r=>events.push(r),status:async()=>{},check:()=>{},stopping:()=>false},
        {capture:async()=>points,solve:async()=>{solveCalls++;return pointPrediction;},submit:async(p,c,r,k,before)=>{before();submitCalls++;return {submitted:true};},
         observeVerdict:async()=>{let armed=false;return {arm(){armed=true;},read:async()=>armed?confirmed.platformVerdict:{status:'unknown',source:'none'},close:async()=>{}};},delay:async()=>{}});
      assert.equal(await flow(reader),true);assert.equal(solveCalls,0);assert.equal(submitCalls,1);
      const id=events.find(r=>r.type==='verification').event.attempt_id;
      const saved=await record({attemptId:id});assert.equal(saved.outcome,'read_recovered');assert.equal(saved.passed,true);assert.equal(saved.prediction.prediction_basis,'exact_image_platform_confirmed');
      assert.equal(await flow(reader),false);assert.equal(submitCalls,1);pass('workflow recalls exact successful sample but still verifies fresh read and keeps one attempt');
      const unknownEvents=[];
      const unknownFlow=createVerification({config:{...config,captcha:{mode:'auto',python:'fake'}},emit:async r=>unknownEvents.push(r),status:async()=>{},check:()=>{},stopping:()=>false},
        {capture:async()=>points,solve:async()=>pointPrediction,submit:async()=>({submitted:true}),delay:async()=>{}});
      assert.equal(await unknownFlow(reader),false);
      assert.equal(unknownEvents.at(-1).event.reason,'platform_verdict_unobserved');
      assert.equal((await record({attemptId:unknownEvents[0].event.attempt_id})).passed,false);
      pass('a fresh read without a current platform verdict pauses, including recalled images');
      await fs.writeFile(flags,JSON.stringify({...settings,max_attempts:1}));
      const fullEvents=[];
      const fullFlow=createVerification({config:{...config,captcha:{mode:'auto'}},emit:async r=>fullEvents.push(r),status:async()=>{},check:()=>{},stopping:()=>false},
        {capture:async()=>points,solve:async()=>{throw Error('must not solve');},submit:async()=>{throw Error('must not submit');}});
      assert.equal(await fullFlow(reader),false);assert.equal(fullEvents.at(-1).event.reason,'sample_archive_unavailable');
      assert.equal(fullEvents.at(-1).event.submissions,0);await fs.writeFile(flags,JSON.stringify(settings));
      pass('archive exhaustion pauses without discarding history or sending an unrecorded attempt');
      let cancelled=false;
      const cancelledFlow=createVerification({config:{...config,captcha:{mode:'auto',python:'fake'}},emit:async r=>{events.push(r);if(r.event?.phase==='recognizing')cancelled=true;},status:async()=>{},check:()=>{if(cancelled)throw Error('cancelled');},stopping:()=>cancelled},
        {capture:async()=>slider,solve:async()=>({status:'needs_review',reason:'cancelled'}),submit:async()=>{throw Error('must not submit');}});
      await assert.rejects(cancelledFlow(reader),/cancelled/);
      const cancelledId=events.filter(r=>r.event?.phase==='detected').at(-1).event.attempt_id;
      assert.equal((await record({attemptId:cancelledId})).outcome,'interrupted');pass('cancellation keeps a pending capture as interrupted, never recovered');
    }finally{if(previous===undefined)delete process.env.CLUBOPS_DATA_DIR;else process.env.CLUBOPS_DATA_DIR=previous;}
    await learning.configure(false,options);assert.equal(await learning.begin(slider,config,randomUUID(),options),null);
    assert.equal(await learning.recalled(points,config,options),null);assert.ok((await learning.status(options)).cases>0);pass('disabling stops capture and recall while retaining records');
    console.log(JSON.stringify({passed,images:'local synthetic fixtures',feedback:'simulated workflow, not platform labels',platform_requests:0}));
  }finally{
    assert.ok(path.resolve(dataDir).startsWith(path.resolve(os.tmpdir())+path.sep));
    await fs.rm(dataDir,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
