export default {async fetch(request, env) {
  try {return await env.TEAM.get(env.TEAM.idFromName('clubops-workbench')).fetch(new Request(request, {redirect:'manual'}));}
  catch {return new Response('工作台连接暂时不可用，请稍后重试。', {status:503, headers:{'content-type':'text/plain; charset=utf-8','cache-control':'no-store'}});}
}};
