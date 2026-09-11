'use strict';
const assert=require('node:assert/strict');
const {runPool}=require('./collector_pool.cjs');
const delay=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  let active=0,peak=0;const starts=[],ends=[],progress=[];
  const r=await runPool([1,2,3,4],2,async value=>{starts.push(value);active++;peak=Math.max(peak,active);await delay(value===1?35:10);ends.push(value);active--;return value*2;},{onActivity:async s=>progress.push({...s})});
  assert.deepEqual(r.outcomes,[2,4,6,8]);assert.equal(peak,2);assert.equal(r.peak,2);assert.deepEqual(starts,[1,2,3,4]);assert.equal(ends[0],2);assert.equal(progress.at(-1).active,0);
  let finished=false,admitted=[];
  await assert.rejects(()=>runPool([1,2,3],2,async n=>{admitted.push(n);await delay(n===1?5:20);if(n===1)throw Error('stop all');finished=true;}),/stop all/);
  assert.equal(finished,true,'Other admitted lane must settle before rejection');assert.deepEqual(admitted,[1,2]);
  const callbackStarts=[];let firstDone=false,callbackSettled=false,reported=false;
  await assert.rejects(()=>runPool([1,2,3],2,async n=>{
    callbackStarts.push(n);await delay(n===1?5:20);if(n===1)firstDone=true;if(n===2)callbackSettled=true;
  },{onActivity:async s=>{if(firstDone&&s.index===0&&s.active===1)throw Error('telemetry failed');},onError:()=>{reported=true;throw Error('reporting failed');}}),/telemetry failed/);
  assert.deepEqual(callbackStarts,[1,2]);assert.equal(callbackSettled,true);assert.equal(reported,true);
  for(const limit of [0,5,1.5])await assert.rejects(()=>runPool([],limit,async()=>{}),/Invalid/);
  console.log('PASS: bounded video pool, actual overlap, ordered outcomes, fail-stop admission and full settlement. Synthetic only.');
})().catch(e=>{console.error(e);process.exitCode=1;});
