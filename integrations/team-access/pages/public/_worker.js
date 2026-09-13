export default {async fetch(request, env) {
  if(request.method==='GET'&&new URL(request.url).pathname==='/_access/entry')return new Response(JSON.stringify({revision:'gateway-entry-v2'}),{headers:{'content-type':'application/json','cache-control':'no-store'}});
  try {return await env.TEAM.get(env.TEAM.idFromName('clubops-workbench-v2')).fetch(new Request(request, {redirect:'manual'}));}
  catch {return new Response('工作台连接暂时不可用，请稍后重试。', {status:503, headers:{'content-type':'text/plain; charset=utf-8','cache-control':'no-store'}});}
}};
