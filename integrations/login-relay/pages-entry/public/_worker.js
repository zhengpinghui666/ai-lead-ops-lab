// An alternative HTTPS entry to the same own-account Worker and storage.
// No credentials, SMS text or OTP values are stored or logged by this entry.
const json = (value, status=200) => new Response(JSON.stringify(value), {status,
  headers:{'content-type':'application/json; charset=utf-8', 'cache-control':'no-store',
    'x-content-type-options':'nosniff'}});
const methods = new Map([
  ['/iphone-script','GET'], ['/v1/health','GET'], ['/v1/pending','GET'],
  ['/v1/login','POST'], ['/v1/otp','POST'], ['/v1/take','POST'], ['/v1/cancel','POST']
]);

export default {async fetch(request, env) {
  const url = new URL(request.url);
  if (url.protocol !== 'https:' || url.username || url.password)
    return json({error:'https_required'},400);
  if (url.pathname === '/connection-check' && request.method === 'GET')
    return json({service:'clubops-login-entry',version:1,
      message:'中转入口可以访问。短信转发仍需在电脑和手机完成连接测试。'});
  if (methods.get(url.pathname) !== request.method) return json({error:'not_found'},404);
  if (url.search && url.pathname !== '/iphone-script') return json({error:'invalid_route'},400);
  try {
    // Cloudflare Service binding: fixed Worker, no public workers.dev lookup,
    // no caller-selected upstream, no redirect follow, and no added credentials.
    return await env.RELAY.fetch(new Request(request, {redirect:'manual'}));
  } catch {
    return json({error:'relay_unavailable'},503);
  }
}};
