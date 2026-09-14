'use strict';
// Rendering actual dashboard functions in isolation. No network or business actions.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8');
const ctx=vm.createContext({trendDays:30,fmt:n=>String(n),esc:s=>String(s),
  button:(text)=>`<button>${text}</button>`,empty:(title,note)=>`<p>${title}</p><p>${note}</p>`,
  notice:s=>s,showModal:(title,html)=>{ctx.modal=html;}});
vm.runInContext(source.slice(source.indexOf('function captchaRateText('),source.indexOf('function incidentFeedback(')),ctx);
const run=code=>vm.runInContext(code,ctx);
assert.equal(run('captchaRateText({submitted:0,passed:0,failed:0})'),'未尝试');
assert.equal(run('captchaRateText({submitted:2,passed:0,failed:0,pass_rate:0})'),'结果未确认','Even a cached legacy zero must not masquerade as failure');
assert.equal(run('captchaRateText({submitted:2,passed:0,failed:0},true)'),'未确认');
assert.equal(run('captchaRateText({submitted:2,passed:0,failed:1})'),'0%');
assert.equal(run('captchaRateText({submitted:3,passed:1,failed:1})'),'50%');
assert.match(run('captchaTodayNote({submitted:0,all_time:{unknown:6}})'),/历史 6 次提交结果未确认/);
run(`d={granularity:'day',date:'2026-09-15',labels:['2026-09-13','2026-09-14','2026-09-15'],
  series:{captcha_rate_same_shape:[null,null,null]},captcha:{types:[{key:'same_shape',submitted:0}],daily:[
    {date:'2026-09-13',types:[{key:'same_shape',encounters:1,submitted:1,passed:0,failed:0}]},
    {date:'2026-09-14',types:[{key:'same_shape',encounters:1,submitted:1,passed:0,failed:0}]},
    {date:'2026-09-15',types:[{key:'same_shape',encounters:0,submitted:0,passed:0,failed:0}]}]}};
  lines=[['captcha_rate_same_shape','同形点选','#507cba']];`);
const unknown=run(`dailyChart(d,'验证码','按日',lines)`);
assert.match(unknown,/尚无已确认的验证码结果/);
assert.doesNotMatch(unknown,/<circle|<svg/,'Unknown-only history does not render a zero-percent line');
assert.match(unknown,/查看每日数值/);
run(`config={labels:d.labels,series:d.series,lines,rates:true,date:d.date,captchaDaily:d.captcha.daily};dailyChartTable(config);`);
assert.match(ctx.modal,/未确认/);assert.doesNotMatch(ctx.modal,/>0%<|>undefined</);
run(`d.series.captcha_rate_same_shape=[null,0,null];d.captcha.daily[1].types[0].failed=1;`);
const confirmed=run(`dailyChart(d,'验证码','按日',lines)`);
assert.match(confirmed,/<circle/,'Explicit failed attempts retain a real zero point');
assert.match(confirmed,/从 2026-09-13 开始/,'The timeline starts at the first event, including unconfirmed events');
assert.doesNotMatch(confirmed,/NaN|undefined/);
const path=run(`dailyPlotMarkup({...config,trendDays:30})`);
assert.equal((path.match(/<circle /g)||[]).length,1,'No fabricated points on unknown or no-attempt dates');
console.log('PASS: captcha no attempt, unknown, explicit zero, partial results, historical dates and detail table.');
