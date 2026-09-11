'use strict';
const {randomUUID}=require('node:crypto');
const browser=require('./captcha_browser.cjs');
const solver=require('./captcha_solver.cjs');
const learning=require('./captcha_learning.cjs');
const verdicts=require('./captcha_verdict.cjs');
const labels={detected:'检测到验证码，已暂停其他读取页',capturing:'正在读取验证码图片',
  recognizing:'正在使用本地识别模块分析验证码',submitting:'正在提交本批唯一一次验证尝试',
  verifying:'正在检查抖音判定和采集接口是否恢复',accepted:'抖音已确认验证通过，且已收到新的有效采集响应',
  needs_review:'自动处理未完成，保留断点并等待处理'};

function createVerification({config,emit,status,check,stopping},dependencies={}){
  const capture=dependencies.capture||browser.capture,submit=dependencies.submit||browser.submit;
  const recognize=dependencies.solve||solver.solve;
  const observeVerdict=dependencies.observeVerdict||verdicts.observe;
  const delay=dependencies.delay||(ms=>new Promise(r=>setTimeout(r,ms)));
  let attempted=false,submissions=0;
  return async reader=>{
    const id=randomUUID(),started=Date.now();
    const event=async(phase,reason,extra={})=>{
      await emit({type:'verification',event:{attempt_id:id,phase,transport:'local_browser',submissions,
        elapsed_ms:Math.min(900000,Date.now()-started),...(reason?{reason}:{}),...extra}});
      await status('running',labels[phase]);
    };
    let adapter,evidence,prediction,judgement;
    let platformVerdict={status:'unknown',source:'none'},readRecovered=false;
    let outcome={outcome:'interrupted',reason:'processing_interrupted'};
    const review=async reason=>{outcome={outcome:'needs_review',reason};await event('needs_review',reason,{...(adapter?{adapter}:{}),platform_verdict:platformVerdict.status,verdict_source:platformVerdict.source});return false;};
    await event('detected');
    if(config.captcha?.mode!=='auto')return review('manual_mode');
    // At most one automatic handling attempt, even when the solver declines.
    const alreadyAttempted=attempted;attempted=true;
    try{
      check();await event('capturing');
      const challenge=await capture(reader.page,{check});check();adapter=challenge.adapter;
      evidence=await learning.begin(challenge,config,id);check();
      if(evidence?.skipped){await emit({type:'diagnostic',stage:'captcha_learning',snapshot:{title:'验证码样本记录',
        visible_text:evidence.reason.includes('capacity')?'本地样本库已达到容量限制，本次未能归档。已有样本保留，已暂停。':'本次样本未能写入本地记录，已有样本保留，已暂停。',responses:[]}});return review('sample_archive_unavailable');}
      if(alreadyAttempted)return review('batch_attempt_limit');
      if(!challenge.payload)return review(challenge.reason||'unsupported_type');
      await require('./captcha_samples.cjs').save(challenge,config);check();
      await event('recognizing',undefined,{adapter:challenge.adapter});
      prediction=await learning.recalled(challenge,config)||await recognize(challenge.payload,{python:config.captcha.python,stopping});check();
      if(prediction.status!=='predicted')return review(prediction.reason||'recognition_declined');
      // A strict adapter validates dimensions and rechecks the original challenge
      // before any interaction. Prediction and actual read recovery remain separate.
      let version=reader.readVersion();
      judgement=await observeVerdict(reader.page,{check});check();
      await event('submitting');
      submissions=1; // Reserve the attempt before entering potentially uncertain UI work.
      const result=await submit(reader.page,challenge,prediction,check,()=>{version=reader.readVersion();judgement.arm();});check();
      if(!result.submitted){submissions=0;return review(result.reason||'submission_declined');}
      await event('verifying',undefined,prediction.image_sha256?{image_sha256:prediction.image_sha256}:{});
      const deadline=Date.now()+10000;
      for(let n=0;n<20&&Date.now()<deadline;n++){
        check();await reader.settleVerification();check();
        platformVerdict=await judgement.read();check();
        if(platformVerdict.status==='failed')return review('platform_verification_failed');
        // Popup disappearance alone, an old response or an unrelated endpoint is
        // insufficient. reader ties the version to the active page and video.
        if(await reader.readableAfter(version)){
          readRecovered=true;
          if(platformVerdict.status==='passed'){
            await event('accepted',undefined,{platform_verdict:'passed',verdict_source:platformVerdict.source});
            outcome={outcome:'read_recovered'};return true;
          }
        }
        await delay(500);
      }
      return review(readRecovered?'platform_verdict_unobserved':'acceptance_not_observed');
    }catch(error){
      check(); // Cancellation, rate limiting and a closed browser retain their stop status.
      if(error?.message==='point_challenge_changed_during_selection')return review('point_selection_changed');
      if(['point_selection_not_registered','point_confirmation_unavailable'].includes(error?.message))return review(error.message);
      return review('automatic_processing_error');
    }finally{
      await judgement?.close().catch(()=>{});
      const saved=await learning.finish(evidence,{...outcome,prediction,submissions,platformVerdict,readRecovered,elapsed_ms:Date.now()-started});
      if(evidence?.attemptId&&!saved)await emit({type:'diagnostic',stage:'captcha_learning',snapshot:{title:'验证码样本记录',
        visible_text:'样本的处理结果未能写入，记录保留为未知，不计为成功。',responses:[]}}).catch(()=>{});
    }
  };
}
module.exports={createVerification};
