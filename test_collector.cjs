'use strict';
const assert=require('node:assert/strict');
const p=require('./collector_parser.cjs');
// Synthetic fixtures only. These tests never access Douyin or write the live database.
const vid='7600000000000000001',cid='7600000000000000002',uid='123456789012';
assert.equal(p.id(7600000000000000001),'','Unsafe numeric IDs must not silently lose precision');
assert.equal(p.id(vid),vid);assert.equal(p.id('demo-comment'),'');
const results=p.searchVideos({data:[{aweme_info:{aweme_id:vid,desc:'测试夹具：无畏契约陪玩'}},{aweme_info:{aweme_id:vid,desc:'相同视频'}},{user:{uid}}]});
assert.equal(results.length,1);assert.equal(results[0].video_id,vid);
assert.deepEqual(p.searchVideos({recommendations:[{aweme_id:vid}]}),[]);
const fixture={comments:[{cid,aweme_id:vid,text:'测试夹具：国服找陪练',create_time:1750000000,user:{uid,nickname:'测试用户'}}],has_more:1};
let r=p.comments(fixture,vid);assert.equal(r.rows.length,1);assert.equal(r.rows[0].published_at,1750000000);assert.equal(r.rows[0].user_id,uid);assert.equal(r.hasMore,true);
assert.equal(p.comments(fixture,'7600000000000000009').rows.length,0);
assert.equal(p.comments({comments:[{cid,text:'测试夹具',user:{sec_uid:'opaque-id'}}]},vid).rows[0].user_id,'');
assert.equal(p.comments({comments:[{cid,text:'测试夹具'}]},vid).rows[0].published_at,null);
assert.equal(p.comments({comments:[{cid:7600000000000000002,text:'bad ID'}]},vid).skipped,1);
assert.equal(p.comments({status_code:7},vid).recognized,false);
assert.equal(p.comments({comments:[],has_more:0},vid).recognized,true);
assert.deepEqual(p.comments({status_code:0,comments:null,total:0,has_more:0},vid),{rows:[],recognized:true,skipped:0,nonText:0,invalid:0,hasMore:false});
for(const empty of [{comments:null,total:0,has_more:0},{status_code:0,comments:null,total:1,has_more:0},{status_code:0,comments:null,total:0,has_more:1},{status_code:0,comments:null},{status_code:0,total:0,has_more:0}])
  assert.equal(p.comments(empty,vid).recognized,false,'Null/missing comments require explicit successful empty evidence');
const reply='7600000000000000003',direct='7600000000000000004';
r=p.comments({comments:[{cid,aweme_id:vid,text:'',reply_comment:[{cid:reply,text:'图片下的合成文字回复'}]},
  {cid:direct,text:'   '},{cid:'7600000000000000005'},{cid:7600000000000000006,text:''}]},vid);
assert.equal(r.nonText,2);assert.equal(r.invalid,2);assert.equal(r.skipped,4);
assert.equal(r.rows.length,1);assert.equal(r.rows[0].comment_id,reply);assert.equal(r.rows[0].parent_comment_id,cid);
const root={cid,aweme_id:vid,text:'合成主评论',reply_id:'0',reply_comment:[
  {cid:reply,text:'合成回复：多少钱',reply_id:cid,user:{uid}},
  {cid:direct,text:'合成回复的回复',reply_id:cid,reply_to_reply_id:reply}
]};
r=p.comments({comments:[root,{...root,reply_comment:[{cid:'7600000000000000005',text:'后加载回复'}]}]},vid);
assert.equal(r.rows.length,4);assert.equal(r.rows[1].parent_comment_id,cid);assert.equal(r.rows[2].parent_comment_id,reply);
assert.equal(r.rows[3].parent_comment_id,cid);
for(const bad of [{aweme_id:7600000000000000001},{aweme_id:''},{reply_id:7600000000000000002},{reply_to_reply_id:7600000000000000002},{reply_id:cid}]){
  assert.equal(p.comments({comments:[{cid,text:'异常合成记录',...bad}]},vid).rows.length,0);
}
r=p.comments({comments:[{...root,reply_comment:[{cid:reply,text:'不一致主评论',reply_id:direct},{cid:direct,text:'合法回复',reply_comment:[{cid:reply,text:'不得递归读取'}]}]}]},vid);
assert.equal(r.skipped,1);assert.equal(r.rows.length,2);
r=p.comments({comments:[{...root,reply_comment:Array.from({length:1001},(_,i)=>({cid:String(80000+i),text:'有界合成回复'}))}],has_more:0},vid);
assert.equal(r.rows.length,1000);assert.equal(r.truncated,true);
const url=`https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${vid}&irrelevant_token=secret`;
assert.equal(p.responseKind(url,vid),'comment');
assert.equal(p.responseKind(url,'7600000000000000009'),'');
assert.equal(p.responseKind(url.replace('www.douyin.com','evil.example'),vid),'');
assert.equal(p.responseKind('https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=test'),'search');
assert.equal(p.responseKind('https://www.douyin.com/aweme/v1/web/user/profile/other/?uid=test'),'');
assert.equal(p.blockFromText('请完成安全验证'),'needs_verification');
assert.equal(p.blockFromText('验证码中间页\n    '),'needs_verification');
assert.equal(p.blockFromText('登录后查看评论'),'needs_login');
assert.equal(p.blockFromText('全部评论\n请先登录后发表评论\n已经公开的评论\n登录后即可参与互动讨论\n立即登录'),'');
assert.equal(p.blockFromText('请先登录后发表评论\n登录后查看更多评论'),'needs_login');
assert.equal(p.blockFromText('请先登录后发表评论\n请完成安全验证'),'needs_verification');
assert.equal(p.pageVideoTitle('无畏契约陪玩避坑指南！ - 抖音',`https://www.douyin.com/video/${vid}`,vid),'无畏契约陪玩避坑指南！');
assert.equal(p.pageVideoTitle('验证码中间页',`https://www.douyin.com/video/${vid}`,vid),'');
assert.equal(p.pageVideoTitle('其他视频',`https://www.douyin.com/video/7600000000000000009`,vid),'');
assert.equal(p.pageVideoTitle('搜索结果',`https://www.douyin.com/search/keyword`,vid),'');
assert.equal(p.pageVideoTitle('图文作品 - 抖音',`https://www.douyin.com/note/${vid}`,vid),'图文作品');
assert.equal(p.contentPageKind(`https://www.douyin.com/note/${vid}/?tab=comment`,vid),'note');
for(const wrong of [`https://www.douyin.com/note/${cid}`,`https://www.douyin.com:444/note/${vid}`,`http://www.douyin.com/note/${vid}`,`https://other.example/note/${vid}`,`https://www.douyin.com/search/${vid}`]){
  assert.equal(p.contentPageKind(wrong,vid),'');assert.equal(p.pageVideoTitle('错误来源',wrong,vid),'');
}
// Phrases actually observed in task #4; no identity or QR content retained here.
assert.equal(p.blockFromText('登录后即可搜索更多精彩视频\n使用原设备扫码\n为保障账号安全，请使用「抖音 APP」扫码验证，以确保为本人操作'),'needs_verification');
assert.equal(p.blockFromText('为保障账号安全\n请使用「抖音 APP」扫码验证'),'needs_verification');
assert.equal(p.blockFromText('扫码登录\n验证码登录\n密码登录\n获取验证码'),'');
assert.equal(p.blockFromText('访问过于频繁'),'rate_limited');
assert.equal(p.blockFromText('用户登录按钮 视频正文'),'');
console.log('PASS: collector parser fixtures: IDs, isolation, timestamps, schema mismatch and access signals. Not live-data proof.');
