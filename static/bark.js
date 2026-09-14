(()=>{
const $=selector=>document.querySelector(selector);
// Bark configuration is read only when opened; never render or retain the saved key.
let barkState=null,barkBusy=false;
const barkDialog=$('#bark-dialog');
function renderBark(value){
  barkState=value;$('#bark-enabled').checked=value.configured?!!value.enabled:true;
  const outcome={accepted:'Bark 已接受推送；请确认手机是否收到。',unknown:'推送结果未确认；如果手机已收到，可确认；未收到可稍后重新测试。',rejected:'Bark 未接受，请检查地址后重新保存。'};
  $('#bark-status').textContent=value.error||(value.verified?'已确认手机收到，可接收人工处理提醒。':value.last_test?outcome[value.last_test.status]||'等待测试结果。':value.configured?'配置已保存，请发送锁屏测试。':'尚未配置。');
  $('[data-command="bark-test"]').disabled=barkBusy||!value.configured;
  $('[data-command="bark-confirm"]').disabled=barkBusy||!value.last_test||value.last_test.status==='rejected'||!!value.last_test.confirmed_at;
}
async function barkAction(action){
  if(barkBusy)return;barkBusy=true;
  try{if(barkState)renderBark(barkState);renderBark(await action());}
  catch(error){$('#bark-status').textContent=error.message;}
  finally{barkBusy=false;if(barkState){$('[data-command="bark-test"]').disabled=!barkState.configured;$('[data-command="bark-confirm"]').disabled=!barkState.last_test||barkState.last_test.status==='rejected'||!!barkState.last_test.confirmed_at;}}
}
async function barkPost(action,body={}){
  const response=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-ClubOps-Token':barkState?.csrf||''},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'手机通知设置未完成');return {...value.result,csrf:barkState?.csrf};
}
document.addEventListener('click',event=>{
  const action=event.target.closest('[data-command]')?.dataset.command;
  if(action==='bark-settings'){barkDialog.showModal();void barkAction(async()=>{const response=await fetch('/api/bark');const value=await response.json();if(!response.ok)throw Error(value.error||'暂时无法读取');$('#bark-url').value='';return value;});}
  if(action==='bark-close')barkDialog.close();
  if(action==='bark-test')void barkAction(()=>barkPost('bark-test'));
  if(action==='bark-confirm'&&barkState?.last_test)void barkAction(()=>barkPost('bark-confirm',{id:barkState.last_test.id}));
});
document.addEventListener('submit',event=>{if(event.target.id!=='bark-form')return;event.preventDefault();const body={url:$('#bark-url').value.trim(),enabled:$('#bark-enabled').checked};$('#bark-url').value='';void barkAction(()=>barkPost('bark-save',body));});
barkDialog.addEventListener('click',event=>{if(event.target===barkDialog){const r=barkDialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)barkDialog.close();}});
barkDialog.addEventListener('close',()=>{$('#bark-url').value='';});
window.addEventListener('pagehide',()=>{$('#bark-url').value='';});

})();
