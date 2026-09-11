'use strict';
// Independent minimal protobuf reader. No vendored client, signing code or auth.
const {gunzipSync}=require('node:zlib');
const utf8=new TextDecoder('utf-8',{fatal:true});
const MAX_FRAME=2*1024*1024,MAX_EXPANDED=4*1024*1024;
function fields(raw){
  if(!Buffer.isBuffer(raw)||raw.length>MAX_EXPANDED)throw Error('invalid frame size');
  let p=0;const result=new Map();
  function integer(){let n=0n;for(let i=0;i<10;i++){if(p>=raw.length)throw Error('truncated');const b=raw[p++];if(i===9&&b>1)throw Error('overflow');n|=BigInt(b&127)<<BigInt(7*i);if(!(b&128))return n;}throw Error('overflow');}
  let count=0;
  while(p<raw.length){
    if(++count>20000)throw Error('too many fields');
    const tag=integer(),number=Number(tag>>3n),wire=Number(tag&7n);if(number<1||number>=2**29)throw Error('bad tag');
    let value;
    if(wire===0)value=integer();
    else if([1,2,5].includes(wire)){const n=wire===2?integer():BigInt(wire===1?8:4);if(n>BigInt(raw.length-p))throw Error('truncated');value=raw.subarray(p,p+Number(n));p+=Number(n);}
    else throw Error('unsupported field');
    if(!result.has(number))result.set(number,[]);result.get(number).push({wire,value});
  }
  return result;
}
function one(f,n,w,otherwise){const a=f.get(n)||[];if(!a.length)return otherwise;if(a.length!==1||a[0].wire!==w)throw Error('ambiguous field');return a[0].value;}
function bytes(f,n){return one(f,n,2,Buffer.alloc(0));}
function text(f,n){return utf8.decode(bytes(f,n));}
function id(f,n){const v=one(f,n,0,0n);return v>0n&&v<2n**63n?v.toString():null;}
function time(f,n){
  const value=one(f,n,0,0n);
  if(value>=1262304000n&&value<4102444800n)return {published_at:new Date(Number(value)*1000).toISOString(),unit:'seconds'};
  if(value>=1262304000000n&&value<4102444800000n)return {published_at:new Date(Number(value)).toISOString(),unit:'milliseconds'};
  return {published_at:null,unit:value===0n?'missing':'invalid'};
}
function socketRoom(address){
  try{const u=new URL(address);if(u.protocol!=='wss:'||!u.hostname.endsWith('.douyin.com')||!/^\/webcast\/im\/push(?:\/|$)/.test(u.pathname))return null;const r=u.searchParams.get('room_id');return /^[1-9][0-9]{4,18}$/.test(r||'')?r:null;}catch{return null;}
}
function httpRoom(address){
  try{const u=new URL(address);if(u.protocol!=='https:'||u.username||u.password||u.port||!u.hostname.endsWith('.douyin.com')||u.pathname!=='/webcast/im/fetch/'||u.searchParams.getAll('room_id').length!==1)return null;const room=u.searchParams.get('room_id');return /^[1-9][0-9]{4,18}$/.test(room||'')?room:null;}catch{return null;}
}
function decodeFrame(frame,roomId){
  if(!Buffer.isBuffer(frame)||frame.length>MAX_FRAME)throw Error('frame too large');
  const push=fields(frame);let payload=bytes(push,8);const encoding=text(push,6);
  if(!payload.length)return {records:[],ignored:0,invalid:0,ended:false,methods:{},errors:{},timestamps:{}};
  if(encoding==='gzip'||payload[0]===31&&payload[1]===139)payload=gunzipSync(payload,{maxOutputLength:MAX_EXPANDED});
  else if(encoding&&encoding!=='identity')throw Error('unsupported encoding');
  return decodeResponse(payload,roomId);
}
function decodeHttpResponse(payload,roomId){
  if(!Buffer.isBuffer(payload)||payload.length>MAX_FRAME)throw Error('response too large');
  if(payload[0]===31&&payload[1]===139)payload=gunzipSync(payload,{maxOutputLength:MAX_EXPANDED});
  return decodeResponse(payload,roomId);
}
function decodeResponse(payload,roomId){
  const response=fields(payload),messages=response.get(1)||[];
  if(messages.length>2000)throw Error('message budget exceeded');
  const output={records:[],ignored:0,invalid:0,ended:false,methods:{},errors:{},timestamps:{},response_bytes:payload.length,response_fields:[...response.keys()].slice(0,20)};
  for(const entry of messages){
    try{
      if(entry.wire!==2)throw Error('bad message');
      const message=fields(entry.value),method=text(message,1);
      const diagnostic=/^Webcast[A-Za-z0-9]{1,70}Message$/.test(method)?method:'unknown';
      output.methods[diagnostic]=(output.methods[diagnostic]||0)+1;
      if(!['WebcastChatMessage','WebcastControlMessage'].includes(method)){output.ignored++;continue;}
      const body=fields(bytes(message,2)),common=fields(bytes(body,1));
      // Some frames omit room_id; their originating socket still identifies the
      // room. An explicit contradictory room is always rejected.
      const commonRoom=id(common,3);
      if(commonRoom&&commonRoom!==roomId)throw Error('room_mismatch');
      if(method==='WebcastControlMessage'){if(one(body,2,0,0n)===3n)output.ended=true;else output.ignored++;continue;}
      const user=fields(bytes(body,2)),content=text(body,3),inner=id(common,2),outer=id(message,3);
      // The envelope and Common IDs differ in current observed frames. They are
      // separate identifiers; retain both and deduplicate by Common when present.
      if(!content.trim()||content.length>2000)throw Error('bad text');
      const nickname=text(user,3);if(nickname.length>200)throw Error('bad nickname');
      const timestamp=time(common,4);output.timestamps[timestamp.unit]=(output.timestamps[timestamp.unit]||0)+1;
      output.records.push({room_id:roomId,message_id:inner,outer_message_id:outer,uid:id(user,1),nickname,text:content,published_at:timestamp.published_at});
    }catch(error){output.invalid++;const reason=['room_mismatch','ambiguous field','bad text','bad nickname'].includes(error.message)?error.message.replaceAll(' ','_'):'decode_error';output.errors[reason]=(output.errors[reason]||0)+1;}
  }
  return output;
}
module.exports={fields,one,decodeFrame,decodeHttpResponse,socketRoom,httpRoom,MAX_FRAME,MAX_EXPANDED};
