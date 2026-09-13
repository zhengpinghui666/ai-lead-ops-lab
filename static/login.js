'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const phaseNames = {opening_browser:'正在打开登录页',arming_relay:'正在准备短信接收',waiting_sms:'等待手机验证码',code_received:'已收到验证码',code_filled:'已填写验证码',checking_browser:'正在确认页面登录',checking_identity:'正在核对账号身份',manual_required:'需要在登录页处理',cancelling:'正在取消',resuming:'正在恢复采集',completed:'登录恢复完成',cancelled:'已取消',timeout:'等待超时',account_mismatch:'登录账号不匹配',relay_unavailable:'中转连接未完成',browser_failed:'登录流程未完成',identity_failed:'账号身份未确认',interrupted:'服务重启，任务已结束',resume_pending:'登录已恢复，采集待继续',phone_tested:'手机连接测试通过'};
const phaseHints = {opening_browser:'请保留项目专用登录窗口。',arming_relay:'先准备接收任务，再进行本次登录。',waiting_sms:'手机快捷指令会转发本次验证码；也可以在同一登录页手动输入。',code_received:'正在将本次验证码交给登录页。',code_filled:'等待页面登录与独立身份复核，暂时不用重复输入。',checking_browser:'等待登录页确认；如出现其他验证，请在该页面完成。',checking_identity:'正在通过 HTTP 核对预期账号，核对成功后才会保存会话。',manual_required:'请在项目登录窗口处理页面提示，完成后点击下方“核对身份”。',cancelling:'正在关闭本次登录任务并清理中转记录。'};
let state, busy=false, dirty=false, originDirty=false, polling=false, pollTimer, secretTimer;
const formatTime = seconds => seconds ? new Date(seconds*1000).toLocaleString('zh-CN',{hour12:false}) : '未记录';
function feedback(message,error=false){const el=$('#feedback');el.hidden=!message;el.textContent=message;el.classList.toggle('error',error);}
function hidePhone(){clearTimeout(secretTimer);$('#phone-origin').value='';$('#phone-authorization').value='';$('#phone-scriptable').value='';$('#phone-credentials').hidden=true;}
function activeJob(){return state?.active ? state.job : null;}
function topStatus(value){
  const job=value.active&&value.job;
  if(!value.available)return ['状态暂不可用',value.error||'无法读取登录恢复状态，请检查本机服务。','需要处理'];
  if(job)return [phaseNames[job.status]||'正在恢复登录',job.kind==='phone_test'?'请在手机上运行转发快捷指令，完成一次连接测试。':phaseHints[job.status]||'请等待当前任务结束。','任务进行中'];
  const last=value.history?.at(-1);
  if(last?.status==='resume_pending')return ['登录已恢复，采集待继续','账号核对已完成，但原采集任务未启动。请返回采集页核对任务记录。','需要处理'];
  const outcomes={cancelled:['本次登录已取消','任务已结束；需要时可重新开始。'],timeout:['本次登录等待超时','检查手机转发或登录页提示，再开始一次新的恢复。'],account_mismatch:['登录账号与预期不符','请核对下方账号，在专用窗口登录正确账号后再尝试。'],identity_failed:['账号身份尚未确认','本次没有确认恢复成功，请核对登录页与预期账号。'],browser_failed:['登录流程未完成','请检查项目专用浏览器是否正常，再尝试恢复。'],relay_unavailable:['中转连接暂不可用','先检查中转连接，再开始新的登录任务。'],interrupted:['上次登录已中断','服务重启结束了上次任务，需要时可重新开始。'],phone_tested:['手机连接测试已通过','可以保存自动恢复设置；真实短信登录仍以实际结果为准。']};
  if(last&&outcomes[last.status]&&(!last.account||last.account===value.config?.account))return [...outcomes[last.status],phaseNames[last.status]];
  if(last?.status==='completed'&&value.session?.ready&&last.account===value.config?.account)return ['登录恢复完成',last.resumed_task_id?`已按原范围接续采集 #${last.resumed_task_id}，可返回采集页查看进度。`:'账号身份已核对，会话已保存。','已完成'];
  if(value.session?.ready)return ['本机会话已保存','日常采集会优先复用已有会话。需要重新登录时，使用专用登录页完成恢复。','已保存会话'];
  return ['需要恢复账号登录',value.relay?.ready?'点击“检查并恢复登录”，在专用窗口继续。':'先完成中转部署与手机连接，再开始自动填写验证码的登录流程。','待连接'];
}
function render(){
  if(!state)return;
  const [title,description,label]=topStatus(state),job=activeJob(),ready=!!state.relay?.ready,available=!!state.available;
  $('#status-title').textContent=title;$('#status-description').textContent=description;$('#status-badge').textContent=label;
  $('#status-badge').className='badge '+(job?'warn':state.session?.ready?'good':'');
  $('#job-context').hidden=!job;$('#job-context').textContent=job?`账号 ${job.account} · ${formatTime(job.created_at)} 开始${job.task_id?' · 接续采集 #'+job.task_id:''}`:'';
  const canAuto=ready&&state.checks?.health_at&&state.checks?.phone_test_at;
  if(!dirty&&state.config){$('#login-account').value=state.config.account;$('#auto-recover').checked=state.config.auto_recover;}
  if(!originDirty)$('#relay-origin').value=state.relay?.origin||'';
  $('#login-account').disabled=busy||!!job||!available;
  $('#auto-recover').disabled=busy||!!job||!available||(!canAuto&&!$('#auto-recover').checked);
  $('#auto-help').textContent=canAuto?'仅在确认登录失效时尝试一次；搜索验证会单独处理。':'完成中转检查和手机连接测试后可开启。';
  $('#config-status').textContent=dirty?'有未保存修改；不会被后台刷新覆盖。':'保存设置不会启动登录或采集。';
  $('#save-config').disabled=busy||!!job||!available;
  $('#start-login').disabled=busy||!!job||!ready||!!state.collection_busy||dirty;
  $('#start-login').hidden=!ready||!!job;
  $('#setup-link').hidden=ready||!!job||!available;
  $('#action-hint').textContent=!ready?'下一步：完成中转部署与手机连接。':dirty?'请先保存账号修改，再开始登录。':state.collection_busy&&!job?'当前有采集任务，请先结束该任务。':'';
  $('#manual-complete').hidden=!job||job.kind!=='login'||!['waiting_sms','manual_required','checking_browser','code_filled'].includes(job.status);
  $('#manual-complete').disabled=busy;
  $('#cancel-login').hidden=!job;$('#cancel-login').disabled=busy||job?.status==='cancelling';
  $('#check-relay').disabled=busy||!!job||!ready;
  $('#test-phone').disabled=busy||!!job||!ready||!!state.collection_busy;
  $('#show-phone').disabled=busy||!!job||!ready;
  $('#provision-relay').disabled=busy||!!job||!!state.relay?.paired||!available;
  $('#provision-relay').textContent=state.relay?.paired?'本机配对已生成':'生成本机配对';
  $('#bind-relay').disabled=busy||!!job||!state.relay?.paired||ready;
  $('#relay-origin').readOnly=ready;
  $('#relay-step').textContent=state.checks?.health_at?'已检查 · 连接成功':ready?'地址已绑定 · 待检查':'尚未部署连接';
  $('#phone-step').textContent=state.checks?.phone_test_at?'已通过连接测试':'尚未验证';
  $('#session-step').textContent=state.session?.ready?'会话已保存':'等待登录';
  $('#pairing-badge').textContent=state.checks?.phone_test_at?'已连接':ready?'待手机测试':'待连接';
  $('#pairing-description').textContent=ready?'中转地址已绑定。先检查连接，再使用 iPhone 蜂窝网络测试转发。':'通过固定 HTTPS 中转，手机使用蜂窝网络也能转发本次登录验证码。';
  const select=$('#resume-task'),selected=select.value;
  const options='<option value="">只恢复登录，不启动采集</option>'+(state.resume_candidates||[]).map(t=>`<option value="${t.id}">恢复采集 #${t.id} · ${esc(t.target)}</option>`).join('');
  if(select.dataset.options!==options){select.innerHTML=options;select.dataset.options=options;select.value=selected;}
  select.disabled=busy||!!job;$('#resume-choice').hidden=!state.resume_candidates?.length||!!job||!ready;
  $('#phone-test-message').hidden=!state.test_message;$('#test-message').textContent=state.test_message||'';
  $('#login-history').innerHTML=(state.history||[]).slice().reverse().map(row=>`<div class="login-history-row"><div><strong>${row.kind==='phone_test'?'手机连接测试':'账号 '+esc(row.account)}${row.task_id?' · 采集 #'+row.task_id:''}</strong><small>${formatTime(row.updated_at)}${row.resumed_task_id?' · 已接续 #'+row.resumed_task_id:''}</small>${row.relay_cleanup_pending?'<small>中转清理未确认，将按有效期失效。</small>':''}</div><span class="badge">${esc(phaseNames[row.status]||'状态待核对')}</span></div>`).join('')||'<p class="muted">暂无恢复记录。打开页面不会请求短信。</p>';
  if(state.collection_busy&&!job)$('#status-description').textContent+=' 当前有采集任务，结束后可恢复登录。';
}
async function refresh(){
  if(polling)return;polling=true;
  try{const response=await fetch('/api/login-recovery');const value=await response.json();if(!response.ok)throw Error(value.error||'状态读取失败');state=value;render();}
  catch{feedback('暂时无法刷新本机状态；不会因此重启任务。',true);}
  finally{polling=false;schedule();}
}
function schedule(){clearTimeout(pollTimer);if(!document.hidden)pollTimer=setTimeout(refresh,state?.active?2000:15000);}
async function post(action,body={}){
  if(!state?.csrf)throw Error('页面尚未连接，请刷新后重试');
  const response=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-ClubOps-Token':state.csrf},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'操作未完成');return value.result;
}
async function perform(action){if(busy)return;busy=true;render();feedback('');try{await action();}catch(error){feedback(error.message,true);}finally{busy=false;await refresh();render();}}
document.addEventListener('input',event=>{if(event.target.closest('#recovery-form')){dirty=true;$('#config-status').textContent='有未保存修改；不会被后台刷新覆盖。';$('#start-login').disabled=true;}if(event.target.id==='relay-origin')originDirty=true;});
document.addEventListener('submit',event=>{
  event.preventDefault();
  if(event.target.id==='recovery-form')void perform(async()=>{await post('login-recovery-save',{account:$('#login-account').value.trim(),auto_recover:$('#auto-recover').checked});dirty=false;$('#login-settings-dialog').close();feedback('设置已保存；未启动登录或采集。');});
  if(event.target.id==='relay-form')void perform(async()=>{await post('login-relay-bind',{origin:$('#relay-origin').value.trim()});originDirty=false;feedback('中转地址已绑定，可以检查连接。');});
});
document.addEventListener('click',event=>{
  const command=event.target.closest('[data-command]')?.dataset.command;if(!command)return;
  if(command==='menu'||command==='close-menu'){const open=command==='menu';setMobileNavigation(open);return;}
  if(command==='hide-phone'){hidePhone();return;}
  if(command==='settings'){$('#login-settings-dialog').showModal();return;}
  if(command==='close-settings'){$('#login-settings-dialog').close();return;}
  if(command==='setup'){$('#login-settings-dialog').showModal();$('#deployment-details').open=true;$('#deployment-details').scrollIntoView({block:'center',behavior:'smooth'});$('#deployment-details summary').focus();return;}
  if(command==='reset'){dirty=false;render();return;}
  void perform(async()=>{
    if(command==='start'){const task=$('#resume-task').value;await post('login-recovery-start',{kind:'login',...(task?{task_id:Number(task)}:{})});feedback('登录任务已开始，请保留项目专用窗口。');}
    if(command==='cancel'||command==='complete'){const job=activeJob();if(!job)throw Error('任务已结束，请查看最新状态');await post('login-recovery-'+command,{id:job.id});}
    if(command==='check'){await post('login-relay-check');feedback('中转连接检查通过。');}
    if(command==='phone-test'){await post('login-recovery-start',{kind:'phone_test'});feedback('请在 iPhone 上运行转发快捷指令。');}
    if(command==='provision'){await post('login-relay-provision');feedback('本机配对已生成并加密保存。部署中转后绑定地址。');}
    if(command==='show-phone'){const value=await post('login-relay-phone');hidePhone();$('#phone-origin').value=value.origin;$('#phone-authorization').value=value.authorization;$('#phone-scriptable').value=JSON.stringify({origin:value.origin,account:value.account,authorization:value.authorization},null,2);$('#phone-credentials').hidden=false;secretTimer=setTimeout(hidePhone,60000);}
  });
});
document.addEventListener('visibilitychange',()=>{hidePhone();if(!document.hidden)void refresh();else schedule();});
document.addEventListener('keydown',event=>{if(event.key==='Escape'){hidePhone();setMobileNavigation(false);}});
window.addEventListener('pagehide',hidePhone);
window.lucide?.createIcons();
void refresh();

function setMobileNavigation(open,restoreFocus=true){
  if(open)document.body.classList.add('menu-open');else document.body.classList.remove('menu-open');
  $('.mobile-menu').setAttribute('aria-expanded',String(open));
  const shell=$('.shell');if(shell)shell.inert=open;
  if(open)$('.mobile-nav-close').focus();else if(restoreFocus)$('.mobile-menu').focus();
}
document.addEventListener('keydown',event=>{
  if(event.key!=='Tab'||!document.body.classList.contains('menu-open'))return;
  const controls=[...document.querySelectorAll('#sidebar a,#sidebar button')].filter(el=>el.getClientRects().length);
  const first=controls[0],last=controls[controls.length-1];
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
});
window.matchMedia?.('(max-width:700px)').addEventListener('change',event=>{if(!event.matches)setMobileNavigation(false,false);});


$('#login-settings-dialog').addEventListener('close',()=>{hidePhone();dirty=false;originDirty=false;render();});
function settingsBackdrop(event){const d=$('#login-settings-dialog'),r=d.getBoundingClientRect();return event.target===d&&(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom);}
let settingsBackdropDown=false;
$('#login-settings-dialog').addEventListener('pointerdown',event=>{settingsBackdropDown=event.button===0&&settingsBackdrop(event);});
$('#login-settings-dialog').addEventListener('pointercancel',()=>{settingsBackdropDown=false;});
$('#login-settings-dialog').addEventListener('click',event=>{const dismiss=settingsBackdropDown&&settingsBackdrop(event);settingsBackdropDown=false;if(dismiss)$('#login-settings-dialog').close();});
