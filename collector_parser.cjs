'use strict';
// Normalizes only responses produced by ordinary page navigation. No request signing/replay.
const id = value => typeof value==='string' && /^\d{5,30}$/.test(value) ? value : Number.isSafeInteger(value)&&value>9999 ? String(value) : '';
const text = (value, max=5000) => typeof value==='string' ? value.trim().slice(0,max) : '';
const gamePattern=/无畏契约|无畏契約|無畏契約|瓦罗兰特|瓦羅蘭特|valorant|瓦陪|陪瓦|打瓦|瓦手游|手瓦|瓦友|(?:^|[\s#＃])瓦(?=$|[\s#＃])/i;
function inSearchScope(row,keyword){
  return !gamePattern.test(text(keyword))||gamePattern.test(text(row?.video_title));
}
function searchPageMatches(url,keyword){
  try{const u=new URL(url);return u.origin==='https://www.douyin.com'&&decodeURIComponent(u.pathname)===`/search/${keyword}`;}catch{return false;}
}
function video(value) {
  if(!value || typeof value!=='object') return null;
  const vid=id(value.aweme_id);
  if(!vid) return null;
  const sec=value.author?.sec_uid,author=typeof sec==='string'&&/^[A-Za-z0-9_-]{10,200}$/.test(sec)?{author_sec_uid:sec,author_nickname:text(value.author?.nickname,120)}:{};
  return {video_id:vid,video_title:text(value.desc)||vid,video_url:`https://www.douyin.com/video/${vid}`,...author,...(Number.isSafeInteger(value.create_time)&&value.create_time>0?{published_at:value.create_time}:{})};
}
function searchVideos(body) {
  const found=new Map();
  const items=Array.isArray(body?.data)?body.data:Array.isArray(body?.aweme_list)?body.aweme_list:[];
  for(const item of items) {
    // Only known video result containers; never recurse through recommendations or user histories.
    const row=video(item?.aweme_info || item);
    if(row) found.set(row.video_id,row);
  }
  return [...found.values()];
}
function comments(body, expectedVideo, context={}) {
  if(body?.status_code!==undefined&&body.status_code!==0)return {rows:[],recognized:false,skipped:0,nonText:0,invalid:0,hasMore:null};
  // Observed task #9: a successful, explicitly empty response uses null instead of [].
  // Missing fields or conflicting totals remain unrecognized, not invented zero comments.
  if(body?.comments===null&&body.status_code===0&&body.total===0&&body.has_more===0)
    return {rows:[],recognized:true,skipped:0,nonText:0,invalid:0,hasMore:false};
  if(!Array.isArray(body?.comments)) return {rows:[],recognized:false,skipped:0,nonText:0,invalid:0,hasMore:null};
  const rows=[],seen=new Set();
  // One bounded embedded reply level only; never invent unseen replies or recurse.
  const limit=1000;
  let skipped=0,nonText=0,invalid=0,examined=0,truncated=false;
  const reference=value=>value==null||value===''||value===0||value==='0'?'':id(value)||null;
  function append(c,root='') {
    if(examined>=limit){truncated=true;return false;}
    examined++;
    const cid=id(c?.cid),vid=c?.aweme_id==null?expectedVideo:id(c.aweme_id);
    const raw=text(c?.text);
    const thread=reference(c?.reply_id),direct=reference(c?.reply_to_reply_id);
    const parent=direct||thread||root;
    if(!cid || typeof c?.text!=='string' || !id(vid) || vid!==expectedVideo || thread===null || direct===null ||
      (root&&thread&&thread!==root) || parent===cid) {skipped++;invalid++;return false;}
    if(seen.has(cid))return true;
    seen.add(cid);
    // A valid image-only parent can still have independently readable text replies.
    if(!raw){skipped++;nonText++;return true;}
    // Do not mix sec_uid and numeric UID in the same identity namespace.
    const uid=id(c.user?.uid);
    const timestamp=Number.isSafeInteger(c.create_time)&&c.create_time>0 ? c.create_time : null;
    rows.push({comment_id:cid,video_id:vid,text:raw,user_id:uid,nickname:text(c.user?.nickname,120),
      published_at:timestamp,parent_comment_id:parent,video_title:context.video_title||vid,
      video_url:`https://www.douyin.com/video/${vid}`});
    return true;
  }
  for(const c of body.comments) {
    if(examined>=limit){truncated=true;break;}
    if(!append(c))continue;
    if(Array.isArray(c.reply_comment))for(const reply of c.reply_comment){
      if(examined>=limit){truncated=true;break;}
      append(reply,id(c.cid));
    }
  }
  return {rows,recognized:true,skipped,nonText,invalid,truncated,hasMore:body.has_more===0?false:body.has_more===1?true:null};
}
function responseKind(url, expectedVideo='', expectedKeyword='') {
  let u;try{u=new URL(url);}catch{return '';}
  if(u.origin!=='https://www.douyin.com')return '';
  if(/^\/aweme\/v\d+\/web\/(?:general\/search|search\/item)\//.test(u.pathname))return !expectedKeyword||u.searchParams.get('keyword')===expectedKeyword?'search':'';
  if(/^\/aweme\/v\d+\/web\/comment\/list\//.test(u.pathname)&&id(expectedVideo)&&u.searchParams.get('aweme_id')===expectedVideo)return 'comment';
  return '';
}
function contentPageKind(url,expectedVideo){
  let u;try{u=new URL(url);}catch{return '';}
  if(u.origin!=='https://www.douyin.com'||!id(expectedVideo))return '';
  const match=u.pathname.match(/^\/(video|note)\/(\d{5,30})\/?$/);
  return match&&match[2]===expectedVideo?match[1]:'';
}
function pageVideoTitle(value,url,expectedVideo){
  if(!contentPageKind(url,expectedVideo))return '';
  const title=text(value).replace(/\s*-\s*抖音\s*$/,'').trim();
  if(!title||title===expectedVideo||title==='抖音'||/验证码中间页|发现更多精彩视频|安全验证|使用原设备扫码/.test(title))return '';
  return title;
}
function blockFromText(value) {
  if(/访问过于频繁|操作频繁|请求过于频繁|稍后再试.{0,12}频繁/.test(value))return 'rate_limited';
  if(/验证码中间页|拖动滑块|请完成.{0,8}验证|安全验证|完成下方验证|验证码验证|使用原设备扫码|为保障账号安全[\s\S]{0,80}扫码验证/.test(value))return 'needs_verification';
  // Task #5: writing requires login, while public comments are already readable.
  // Ignore only known write-only prompts, never an actual read/search login gate.
  const reading=value.replace(/(?:请先)?登录后(?:即可)?(?:发表|发布|发送)(?:评论|弹幕)|登录后即可参与互动讨论/g,'');
  if(/登录后.{0,12}(?:查看|评论|搜索)|登录即可.{0,12}(?:评论|搜索)|请先登录/.test(reading))return 'needs_login';
  return '';
}
function blockFromBody(body){
  if(!body||typeof body!=='object')return '';
  if(body.verify_type||body.verify_data)return 'needs_verification';
  const reason=body.search_nil_info?.search_nil_type;
  return typeof reason==='string'&&/verify|antispam|risk|captcha/i.test(reason)?'needs_verification':'';
}
module.exports={id,video,searchVideos,comments,responseKind,contentPageKind,pageVideoTitle,blockFromText,blockFromBody,inSearchScope,searchPageMatches};
