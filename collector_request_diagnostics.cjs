'use strict';
// Strict public-value allowlist. Never retain signed URLs or authentication data.
const fields=new Set(('device_platform aid channel aweme_id cursor count item_type cut_version pc_client_type pc_libra_divert '+
  'browser_language browser_platform browser_name browser_version browser_online engine_name engine_version '+
  'os_name os_version screen_width screen_height cpu_core_num device_memory platform update_version_code '+
  'version_code version_name cookie_enabled support_h265 support_dash downlink effective_type round_trip_time').split(' '));
const ephemeral=['msToken','uifid','verifyFp','fp','webid','a_bogus','rcFT','whale_cut_token'];
const publicHeaders=['user-agent','accept','accept-language','sec-ch-ua','sec-ch-ua-mobile','sec-ch-ua-platform'];
const tickets=['bd-ticket-guard-client-data','bd-ticket-guard-ree-public-key','bd-ticket-guard-version','bd-ticket-guard-web-sign-type','bd-ticket-guard-web-version'];
function requestContext(rawUrl,headers={}){
  const url=new URL(rawUrl),params={},normalized={};
  if(!['www.douyin.com','www-hj.douyin.com'].includes(url.hostname)||!['/aweme/v1/web/comment/list/','/aweme/v1/web/comment/list/reply/'].includes(url.pathname))return {};
  for(const [key,value] of url.searchParams)if(fields.has(key)&&value.length<=120&&/^[\w .+-]*$/.test(value))params[key]=value;
  for(const [name,value] of Object.entries(headers||{}))normalized[name.toLowerCase()]=String(value);
  const safeHeaders={};
  for(const name of publicHeaders){const value=normalized[name];if(value&&value.length<=512&&!/[\r\n]/.test(value))safeHeaders[name]=value;}
  return {version:'comment-request-context-v1',parameters:params,public_headers:safeHeaders,
    session_headers_present:{uifid:Boolean(normalized.uifid)},
    session_fields_present:Object.fromEntries(ephemeral.map(key=>[key,Boolean(url.searchParams.get(key))])),
    ticket_headers_present:Object.fromEntries(tickets.map(key=>[key,Boolean(normalized[key])]))};
}
module.exports={requestContext};
