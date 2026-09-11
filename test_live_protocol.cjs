'use strict';
const assert=require('node:assert/strict');
const {gzipSync}=require('node:zlib');
const {decodeFrame,decodeHttpResponse,socketRoom,httpRoom,MAX_FRAME,MAX_EXPANDED}=require('./live_protocol.cjs');
const ROOM='10000000000000001',UID='10000000000000002',MID='10000000000000003';
function v(n){n=BigInt(n);const a=[];while(n>127n){a.push(Number(n&127n)|128);n>>=7n;}a.push(Number(n));return Buffer.from(a);}
function f(n,x){return typeof x==='number'||typeof x==='bigint'?Buffer.concat([v(n*8),v(x)]):Buffer.concat([v(n*8+2),v(Buffer.byteLength(x)),Buffer.from(x)]);}
function message({room=ROOM,uid=UID,mid=MID,outer=MID,method='WebcastChatMessage',content='无畏契约找陪练',nickname='合成测试',timestamp=1788998400,status=3}={}){
  const common=Buffer.concat([f(2,BigInt(mid||0)),f(3,BigInt(room)),f(4,timestamp)]);
  const body=method==='WebcastControlMessage'?Buffer.concat([f(1,common),f(2,status)]):Buffer.concat([f(1,common),f(2,Buffer.concat([f(1,BigInt(uid||0)),f(3,nickname)])),f(3,content)]);
  return Buffer.concat([f(1,method),f(2,body),f(3,BigInt(outer||0))]);
}
function response(messages){return Buffer.concat(messages.map(x=>f(1,x)));}
function frame(messages,{gzip=true}={}){const payload=response(messages);return Buffer.concat([f(6,gzip?'gzip':'identity'),f(7,'msg'),f(8,gzip?gzipSync(payload):payload)]);}
assert.equal(httpRoom(`https://live.douyin.com/webcast/im/fetch/?room_id=${ROOM}`),ROOM);
for(const url of [`http://live.douyin.com/webcast/im/fetch/?room_id=${ROOM}`,`https://live.douyin.com.evil/webcast/im/fetch/?room_id=${ROOM}`,`https://live.douyin.com/webcast/im/fetch/?room_id=${ROOM}&room_id=99999`,`https://user@live.douyin.com/webcast/im/fetch/?room_id=${ROOM}`])assert.equal(httpRoom(url),null);
assert.deepEqual(decodeHttpResponse(response([message()]),ROOM),decodeFrame(frame([message()]),ROOM));
assert.deepEqual(decodeHttpResponse(gzipSync(response([message()])),ROOM),decodeFrame(frame([message()]),ROOM));
assert.throws(()=>decodeHttpResponse(Buffer.alloc(MAX_FRAME+1),ROOM));
assert.throws(()=>decodeHttpResponse(gzipSync(Buffer.alloc(MAX_EXPANDED+1)),ROOM));
assert.equal(socketRoom(`wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?room_id=${ROOM}`),ROOM);
assert.equal(socketRoom(`wss://evil.douyin.com.evil/webcast/im/push/v2/?room_id=${ROOM}`),null);
assert.equal(socketRoom(`wss://imapi.douyin.com/private/messages?room_id=${ROOM}`),null);
const decoded=decodeFrame(frame([message()]),ROOM);
assert.equal(decoded.records[0].uid,UID);assert.equal(decoded.records[0].message_id,MID);assert.equal(decoded.records[0].nickname,'合成测试');
assert.equal(decoded.records[0].published_at,new Date(1788998400000).toISOString());
assert.equal(decoded.timestamps.seconds,1);
const milliseconds=decodeFrame(frame([message({timestamp:1788998400123})]),ROOM);
assert.equal(milliseconds.records[0].published_at,new Date(1788998400123).toISOString());
assert.equal(milliseconds.timestamps.milliseconds,1);
for(const timestamp of [1,1788998400000000n])assert.equal(decodeFrame(frame([message({timestamp})]),ROOM).records[0].published_at,null);
assert.equal(decodeFrame(frame([message({room:'12345'})]),ROOM).invalid,1);
assert.equal(decodeFrame(frame([message({room:'0'})]),ROOM).records[0].room_id,ROOM,'An absent per-message room may use its bound socket room');
const separateIds=decodeFrame(frame([message({outer:'99999'})]),ROOM).records[0];
assert.equal(separateIds.message_id,MID);assert.equal(separateIds.outer_message_id,'99999');
const missing=decodeFrame(frame([message({uid:null,mid:null,outer:null,timestamp:0})],{gzip:false}),ROOM).records[0];
assert.equal(missing.uid,null);assert.equal(missing.message_id,null);assert.equal(missing.published_at,null);
assert.equal(decodeFrame(frame([message({method:'WebcastGiftMessage'})]),ROOM).ignored,1);
assert.equal(decodeFrame(frame([message({method:'WebcastControlMessage'})]),ROOM).ended,true);
assert.equal(decodeFrame(frame([message({content:Buffer.from([255])})]),ROOM).invalid,1);
for(const invalid of [Buffer.from([0]),Buffer.from([0x42,5,1]),f(8,Buffer.from([31,139,0]))])assert.throws(()=>decodeFrame(invalid,ROOM));
assert.throws(()=>decodeFrame(Buffer.concat([f(6,'gzip'),f(8,gzipSync(Buffer.alloc(MAX_EXPANDED+1)))]),ROOM));
console.log('PASS: live frames, exact numeric IDs, room binding, missing values, end signal, malformed UTF-8/protobuf and gzip bounds. Synthetic only.');
module.exports={frame,response,message,ROOM};
