'use strict';
// Real child-process/IPC tests with a synthetic Playwright fixture. Never accesses the platform.
const {spawn}=require('node:child_process');
const path=require('node:path');
const assert=require('node:assert/strict');
function run(scenario,{interactive=false,onStatus,kind='search',candidatePolicy=null,videoLimit=1}={}){
  return new Promise((resolve,reject)=>{
    const child=spawn(process.execPath,[path.join(__dirname,process.env.CLUBOPS_TEST_RUNNER||'collector_worker.cjs')],{cwd:__dirname,windowsHide:true,env:{...process.env,CLUBOPS_PLAYWRIGHT:path.join(__dirname,'tests/fixtures/playwright_fixture.cjs'),CLUBOPS_FIXTURE_SCENARIO:scenario},stdio:['pipe','pipe','pipe']});
    const messages=[];let pending='',errors='';
    const timer=setTimeout(()=>{child.kill();reject(Error(`Fixture ${scenario} did not exit`));},25000);
    child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    child.stderr.on('data',s=>errors+=s);
    child.stdout.on('data',s=>{pending+=s;const lines=pending.split('\n');pending=lines.pop();for(const line of lines){if(!line)continue;const m=JSON.parse(line);messages.push(m);if(m.type==='status')onStatus?.(m,child);}});
    child.on('error',reject);
    child.on('close',code=>{clearTimeout(timer);if(code!==0)reject(Error(`${scenario}: ${code}: ${errors}`));else resolve(messages);});
    child.stdin.write(JSON.stringify({kind,target:kind==='video'?'https://www.douyin.com/video/7600000000000000001':'SYNTHETIC FIXTURE ONLY',video_limit:videoLimit,comment_limit:2,interactive,candidate_policy:candidatePolicy,profile_dir:path.join(__dirname,'tests/not-a-real-browser-profile')})+'\n');
  });
}
const terminal=messages=>messages.filter(m=>m.type==='status').at(-1)?.status;
(async()=>{
  const candidatePolicy={version:'candidate-rotation-v1',as_of_ms:100000,round:1,history:[{video_id:'7600000000000000001',last_selected_ms:90000,priority_ms:200000}]};
  const [rotated,gatedPool]=await Promise.all([run('candidate-rotation',{candidatePolicy}),run('search-503',{candidatePolicy})]);
  assert.equal(terminal(rotated),'completed');
  assert.equal(rotated.find(m=>m.type==='candidates').records.length,3);
  assert.equal(rotated.find(m=>m.type==='targets').records[0].video_id,'7600000000000000009');
  assert.equal(rotated.find(m=>m.type==='comment').record.video_id,'7600000000000000009');
  assert.ok(rotated.some(m=>m.type==='diagnostic'&&m.stage==='candidate_selection'));
  assert.equal(terminal(gatedPool),'network_error');assert.ok(!gatedPool.some(m=>m.type==='targets'||m.type==='candidates'));
  const upstream=await Promise.all(['navigation-502','search-503','comment-503','gateway-title-only'].map(s=>run(s)));
  for(const messages of upstream.slice(0,3)){
    assert.equal(terminal(messages),'network_error');
    assert.equal(messages.filter(m=>m.type==='comment').length,0);
    assert.ok(!JSON.stringify(messages).includes('PRIVATE_RESPONSE_SENTINEL'));
  }
  const navigation=upstream[0].find(m=>m.type==='diagnostic'&&m.stage==='finished-error').snapshot;
  assert.equal(navigation.navigation_http_status,502);assert.equal(navigation.navigation_retry_after_seconds,90);
  for(const messages of upstream.slice(1,3)){
    const meta=messages.find(m=>m.type==='diagnostic'&&m.stage==='finished-error').snapshot.responses.find(m=>m.status===503);
    assert.equal(meta.retry_after_seconds,180);assert.equal(meta.body_bytes,undefined);
  }
  assert.equal(terminal(upstream[3]),'no_data');
  assert.equal(upstream[3].find(m=>m.type==='diagnostic').snapshot.navigation_http_status,200);
  const timed=await run('comment-navigation-timeout');
  assert.equal(terminal(timed),'network_error');
  assert.equal(timed.filter(m=>m.type==='comment').length,0);
  const timeoutEvidence=timed.find(m=>m.type==='diagnostic'&&m.stage==='document-timeout').snapshot;
  assert.equal(timeoutEvidence.navigation_error,'navigation_timeout');
  assert.equal(timeoutEvidence.navigation_http_status,null);
  assert.ok(!timed.some(m=>m.type==='status'&&m.status==='needs_interaction'));
  const [missing,mixedMissing]=await Promise.all([run('unavailable-video'),run('unavailable-first',{videoLimit:2})]);
  for(const messages of [missing,mixedMissing]){
    assert.equal(terminal(messages),'completed');
    assert.equal(messages.filter(m=>m.type==='checkpoint'&&m.status==='unavailable').length,1);
    assert.match(messages.filter(m=>m.type==='status').at(-1).detail,/1 个作品明确不存在/);
  }
  assert.equal(missing.filter(m=>m.type==='comment').length,0);
  assert.ok(mixedMissing.some(m=>m.type==='comment'&&m.record.video_id==='7600000000000000009'));
  const [success,cancelled,closed,eof,schema,invalid,overflow,mixed,resumed,replies,device,publicRead,scrollPartial,scrollDone,emptyNull,ambiguousNull,emptySearch,htmlSearch,unavailableSearch]=await Promise.all([
    run('success'),
    run('verification-cancel',{interactive:true,onStatus:(m,c)=>{if(m.status==='needs_verification')c.stdin.write('{"command":"cancel"}\n');}}),
    run('verification-window-close',{interactive:true}),
    run('verification-eof',{interactive:true,onStatus:(m,c)=>{if(m.status==='needs_verification')c.stdin.end();}}),
    run('schema-error'),run('invalid-id'),run('queue-overflow'),run('mixed-schema'),
    run('resume-search-dom',{interactive:true,onStatus:(m,c)=>{if(m.status==='needs_interaction')c.stdin.write('{"command":"resume"}\n');}}),
    run('embedded-replies'),
    run('device-challenge',{interactive:true,onStatus:(m,c)=>{if(m.status==='needs_verification')c.stdin.write('{"command":"cancel"}\n');}}),
    run('public-comments-login-to-post',{kind:'video'}),run('scroll-unavailable'),run('scroll-late-budget'),run('empty-null'),run('ambiguous-null'),
    run('search-empty-body'),run('search-html-body'),run('search-body-unavailable')
  ]);
  assert.equal(terminal(success),'completed');assert.equal(success.filter(m=>m.type==='comment').length,1);
  assert.ok(success.some(m=>m.type==='targets'));assert.ok(success.some(m=>m.type==='checkpoint'&&m.status==='done'));
  assert.equal(terminal(cancelled),'cancelled');assert.equal(terminal(closed),'interrupted');assert.equal(terminal(eof),'cancelled');
  assert.equal(terminal(device),'cancelled');assert.ok(device.some(m=>m.status==='needs_verification'));
  assert.equal(terminal(publicRead),'completed');
  assert.equal(publicRead.filter(m=>m.type==='comment').length,1);
  assert.equal(publicRead.find(m=>m.type==='video').record.video_title,'合成夹具：无畏契约陪玩');
  assert.equal(publicRead.find(m=>m.type==='comment').record.video_title,'合成夹具：无畏契约陪玩');
  assert.ok(!publicRead.some(m=>m.status==='needs_login'));
  assert.equal(terminal(scrollPartial),'partial');assert.equal(scrollPartial.filter(m=>m.type==='comment').length,1);
  assert.ok(scrollPartial.some(m=>m.type==='diagnostic'&&m.stage==='comment-scroll-unavailable'));
  assert.equal(terminal(scrollDone),'completed');assert.equal(scrollDone.filter(m=>m.type==='comment').length,2);
  assert.equal(terminal(emptyNull),'completed');assert.equal(emptyNull.filter(m=>m.type==='comment').length,0);
  assert.ok(emptyNull.some(m=>m.type==='checkpoint'&&m.status==='done'));
  assert.equal(terminal(ambiguousNull),'schema_changed');assert.equal(ambiguousNull.filter(m=>m.type==='comment').length,0);
  const emptyMeta=ambiguousNull.find(m=>m.type==='diagnostic'&&m.stage==='comment-schema').snapshot.responses.find(m=>m.kind==='comment');
  assert.equal(emptyMeta.comments_type,'null');assert.equal(emptyMeta.status_code,0);assert.equal(emptyMeta.total,1);
  assert.equal(emptyMeta.has_more,0);assert.equal(emptyMeta.comments_count,undefined);
  for(const [messages,expected,error] of [[emptySearch,'empty_response','empty_body'],[htmlSearch,'schema_changed','invalid_json'],[unavailableSearch,'network_error','body_unavailable']]){
    assert.equal(terminal(messages),expected);
    assert.equal(messages.filter(m=>m.type==='comment').length,0);
    const responseMeta=messages.find(m=>m.type==='diagnostic'&&m.stage==='search-response-unreadable').snapshot.responses[0];
    assert.equal(responseMeta.body_error,error);assert.equal(responseMeta.status,200);
    assert.ok(!JSON.stringify(messages).includes('PRIVATE_RESPONSE_SENTINEL'),'Raw response content and errors must not enter diagnostics');
  }
  const emptySearchMeta=emptySearch.find(m=>m.type==='diagnostic').snapshot.responses[0];
  assert.equal(emptySearchMeta.body_bytes,0);assert.equal(emptySearchMeta.content_kind,'json');
  assert.equal(htmlSearch.find(m=>m.type==='diagnostic').snapshot.responses[0].content_kind,'html');
  assert.equal(terminal(schema),'schema_changed');assert.equal(terminal(invalid),'schema_changed');
  assert.equal(terminal(overflow),'resource_limited');assert.equal(overflow.filter(m=>m.type==='comment').length,0);
  assert.equal(terminal(mixed),'partial');assert.equal(mixed.filter(m=>m.type==='comment').length,1);
  assert.ok(mixed.some(m=>m.type==='checkpoint'&&m.status==='partial'));
  assert.equal(terminal(resumed),'completed');assert.equal(resumed.filter(m=>m.type==='comment').length,1);
  assert.equal(terminal(replies),'completed');
  const replyRecords=replies.filter(m=>m.type==='comment').map(m=>m.record);
  assert.equal(replyRecords.length,2,'Roots and replies share the same per-video budget');
  assert.equal(replyRecords[1].parent_comment_id,replyRecords[0].comment_id);
  for(const messages of [cancelled,closed,eof,schema,invalid,device])assert.equal(messages.filter(m=>m.type==='comment').length,0);
  console.log('PASS: 28 child-process scenarios including missing-work isolation, document timeout, candidate rotation/gated discovery, HTTP failures, login/verification and bounded responses. Synthetic only.');
})().catch(e=>{console.error(e);process.exitCode=1;});
