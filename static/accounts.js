'use strict';
(()=>{
 const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let data,selected='',timer,busy=false,loading=false;
 const when=v=>v?new Date(typeof v==='number'?v*1000:v).toLocaleString('zh-CN',{hour12:false}):'尚无记录';
 const tell=(text,error=false)=>{const el=$('#feedback');el.textContent=text;el.hidden=!text;el.classList.toggle('error',error);};
 const button=(label,action,style='small',extra='')=>`<button class="button ${style}" data-account-action="${action}" ${extra}>${label}</button>`;
 const selectedRow=()=>data?.accounts.find(a=>a.account_id===selected);
 const roleLabel=role=>data.roles.find(r=>r.id===role)?.label||role;
 const jobActive=a=>a.login_job&&!a.login_job.finished_at;
 function draw(){
  if(!data)return;
  if(!selectedRow())selected=data.accounts[0]?.account_id||'';
  $('#account-count').textContent=data.accounts.length;
  $('#account-list').innerHTML=data.accounts.map(a=>`<button class="account-choice ${selected===a.account_id?'selected':''}" data-account-action="select" data-id="${esc(a.account_id)}" aria-pressed="${selected===a.account_id}"><span class="account-avatar">${esc(a.account_id.slice(-2))}</span><span><strong>${esc(a.label||a.account_id)}</strong><small>抖音号 ${esc(a.account_id)}</small><small>${a.enabled?a.roles.map(roleLabel).map(esc).join(' · '):'暂未分配任务'}</small></span></button>`).join('')||'<p class="muted account-empty">添加账号后分别设置任务。</p>';
  const a=selectedRow();if(!a){$('#account-detail').innerHTML='<div class="account-empty"><h2>开始添加账号</h2><p>每个账号保存独立登录，按分工处理任务。</p></div>';return;}
  const ready=a.session?.ready,im=a.im_session?.im_read_verified,job=a.login_job,last=a.latest_collection;
  const blocked=last&&['needs_verification','needs_login','rate_limited','identity_failed','session_expired'].includes(last.status);
  $('#account-detail').innerHTML=`<div class="account-detail-head"><div><h2>${esc(a.label||a.account_id)}</h2><small>抖音号 ${esc(a.account_id)}</small></div><div class="actions">${button('管理任务','edit')}${a.storage==='primary'?button('登录与手机设置','primary'):button(ready?'重新登录 / 核对':'登录账号','login',ready?'small':'primary small',busy||jobActive(a)?'disabled':'')}</div></div>
    ${jobActive(a)?`<div class="account-alert"><strong>${esc(job.detail)}</strong><small>账号 ${esc(a.account_id)} · ${when(job.created_at)}</small><div class="actions">${button('已完成登录，核对身份','complete','small',busy?'disabled':'')}${button('取消登录','cancel','small',busy?'disabled':'')}</div></div>`:''}
    ${blocked?`<div class="account-alert"><strong>${last.status==='needs_verification'?'采集需要完成平台验证':last.status==='rate_limited'?'采集遇到平台频率限制':last.status==='session_expired'?'验证窗口已结束，采集尚未恢复':'采集登录需要处理'}</strong><p>批次 #${last.id} · ${when(last.updated_at)}</p><small>已保存登录不代表本次采集验证已经通过。</small></div>`:''}
    <div class="account-status-grid"><div><small>采集登录</small><strong>${ready?'登录已保存':a.sender_uid?'需重新核对':'等待登录'}</strong><small>${ready?'有效期至 '+when(a.session.expires_at):'核对账号后才投入采集'}</small></div><div><small>私信与群聊登录</small><strong>${im?'会话已核对':'尚未核对'}</strong><small>${im?'最近核对 '+when(a.im_session.checked_at):'登录后独立检查消息通道'}</small></div><div><small>该账号已加入群</small><strong>${a.group_count||0} 个</strong><small>按账号分别记录成员身份</small></div></div>
    <div class="account-section-title"><h3>任务分工</h3><small>修改于下一批生效</small></div><div class="account-role-list">${data.roles.map(role=>{const on=a.enabled&&a.roles.includes(role.id);return `<div><span>${esc(role.label)}</span><span class="badge ${on?'good':''}">${on?'已分配':'未分配'}</span></div>`;}).join('')}</div>
    <div class="account-section-title"><h3>最近记录</h3></div><div class="account-recent">${last?`<p>采集批次 #${last.id} · ${esc(({completed:'已完成',needs_verification:'等待验证',session_expired:'验证窗口已结束',needs_login:'需要登录',rate_limited:'平台限流',identity_failed:'身份核对未通过',cancelled:'已取消',interrupted:'运行中断',failed:'采集未完成',running:'正在采集',starting:'正在启动',queued:'等待采集'}[last.status]||'查看采集记录'))}</p><small>${when(last.updated_at)}</small>`:'<p class="muted">尚无该账号的采集批次。</p>'}${job&&!jobActive(a)?`<p>${esc(job.detail)} · ${when(job.updated_at)}</p>`:''}</div>`;
 }
 async function refresh(){
  if(loading)return;loading=true;
  try{const response=await fetch('/api/accounts');const value=await response.json();if(!response.ok)throw Error(value.error||'账号读取失败');data=value;draw();}
  catch(e){tell(e.message,true);}finally{loading=false;clearTimeout(timer);if(!document.hidden)timer=setTimeout(refresh,data?.accounts.some(jobActive)?2000:15000);}
 }
 async function post(action,body){
  if(!data?.csrf)throw Error('账号页面尚未连接');
  const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-ClubOps-Token':data.csrf},body:JSON.stringify(body)});
  const value=await r.json();if(!r.ok)throw Error(value.error||'操作未完成');return value.result;
 }
 function editor(add=false){
  const a=selectedRow();if(!add&&!a)return;
  $('#account-editor-title').textContent=add?'添加账号':'账号任务分工';
  $('#account-editor-body').innerHTML=add?`<form id="account-add-form"><label class="field"><span>抖音号</span><input name="account_id" autocomplete="off" required minlength="2" maxlength="32" pattern="[A-Za-z0-9_.\\-]+"></label><label class="field"><span>账号备注</span><input name="label" maxlength="80" placeholder="例如：评论采集号"></label><p class="muted">创建后登录并核对身份，再分配任务。</p><div class="login-actions"><button class="button primary" type="submit">创建账号</button></div><p class="account-form-error" role="alert"></p></form>`:
   `<form id="account-role-form" data-id="${esc(a.account_id)}" data-revision="${esc(a.revision)}"><label class="field"><span>账号备注</span><input name="label" maxlength="80" value="${esc(a.label)}"></label><div class="account-role-options">${data.roles.map(role=>{const on=a.enabled&&a.roles.includes(role.id);let reason='';if(!a.sender_uid)reason='请先登录账号';else if(['groups','outreach'].includes(role.id)&&!a.im_session?.im_read_verified)reason='请先核对私信与群聊登录';else if(['comments','discovery','live'].includes(role.id)&&!a.session?.ready)reason='请先恢复采集登录';if(role.id==='outreach'&&a.sender_uid!==data.sender_uid)reason='需先在私信通道设置中切换发送账号';return `<label><input type="checkbox" name="roles" value="${role.id}" ${on?'checked':''} ${reason&&!on?'disabled':''}><span><strong>${esc(role.label)}</strong>${reason?`<small>${esc(reason)}</small>`:''}</span></label>`;}).join('')}</div><p class="muted">取消全部任务后，该账号停止接收新任务，已有数据与登录保留。</p><div class="login-actions"><button class="button primary" type="submit" ${!a.sender_uid?'disabled':''}>保存任务分工</button></div><p class="account-form-error" role="alert"></p></form>`;
  $('#account-editor').showModal();
 }
 document.addEventListener('click',event=>{
  const control=event.target.closest('[data-account-action]');if(!control)return;
  const action=control.dataset.accountAction;
  if(action==='select'){selected=control.dataset.id;draw();return;}
  if(action==='close'){control.closest('dialog')?.close();return;}
  if(action==='add'||action==='edit'){editor(action==='add');return;}
  if(action==='primary'){$('#primary-login-dialog').showModal();return;}
  if(busy)return;
  const a=selectedRow();if(!a)return;
  busy=true;draw();
  void(async()=>{try{
   if(action==='login'){await post('account-login-start',{account_id:a.account_id});tell('已开始该账号的独立登录任务。');}
   if(['complete','cancel'].includes(action)){await post('account-login-command',{id:a.login_job.id,command:action});tell(action==='complete'?'正在核对账号身份。':'正在结束登录任务。');}
  }catch(e){tell(e.message,true);}finally{busy=false;await refresh();}})();
 });
 document.addEventListener('submit',event=>{
  const form=event.target;if(!['account-add-form','account-role-form'].includes(form.id))return;
  event.preventDefault();if(busy)return;busy=true;
  const values=new FormData(form),add=form.id==='account-add-form';
  const body=add?{account_id:values.get('account_id').trim(),label:values.get('label').trim()}:{account_id:form.dataset.id,revision:form.dataset.revision,label:values.get('label').trim(),roles:values.getAll('roles'),enabled:values.getAll('roles').length>0};
  form.querySelector('button[type="submit"]').disabled=true;
  void(async()=>{try{await post(add?'account-create':'account-save',body);selected=body.account_id;$('#account-editor').close();tell(add?'账号已创建，请登录并核对身份。':'任务分工已保存；在途批次保持原账号。');}catch(e){form.querySelector('.account-form-error').textContent=e.message;}finally{busy=false;form.querySelector('button[type="submit"]').disabled=false;await refresh();}})();
 });
 for(const id of ['primary-login-dialog','account-editor']){
  const dialog=$('#'+id);let down=false;
  const outside=e=>{const r=dialog.getBoundingClientRect();return e.target===dialog&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom);};
  dialog.addEventListener('pointerdown',e=>{down=e.button===0&&outside(e);});
  dialog.addEventListener('pointercancel',()=>{down=false;});
  dialog.addEventListener('click',e=>{if(down&&outside(e))dialog.close();down=false;});
 }
 document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)void refresh();});
 void refresh();
})();
