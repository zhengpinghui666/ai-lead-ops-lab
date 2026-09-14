'use strict';
// Retry only the original main-document request that failed before any response.
// Keep the same page/context/account and the original total navigation budget.
const TRANSIENT = new Set(['ERR_FAILED','ERR_CONNECTION_CLOSED','ERR_CONNECTION_RESET',
  'ERR_CONNECTION_ABORTED','ERR_CONNECTION_TIMED_OUT','ERR_TIMED_OUT',
  'ERR_INTERNET_DISCONNECTED','ERR_NETWORK_CHANGED','ERR_NAME_NOT_RESOLVED']);

async function navigate(page, url, {stopped, attempt, retry, pause = ms => new Promise(r => setTimeout(r, ms)), now = () => performance.now()}) {
  const deadline = now() + 25000;
  let failure = null, responded = false, observedResponse = null;
  const main = request => request.isNavigationRequest() && request.frame() === page.mainFrame();
  const failed = request => {
    try {
      if (main(request) && request.url() === url)
        failure = request.failure()?.errorText?.match(/^net::(ERR_[A-Z_]+)$/)?.[1] || null;
    } catch {}
  };
  const response = value => { try { if (main(value.request())) {responded = true; observedResponse = value;} } catch {} };
  page.on('requestfailed', failed);
  page.on('response', response);
  try {
    for (let number = 1; number <= 3; number++) {
      if (stopped()) throw Error('navigation_stopped');
      // Chromium may itself finish/reload its error page during the delay.
      // Preserve any newly received page, especially login/verification gates.
      if (number > 1 && responded) {
        await page.waitForLoadState('domcontentloaded', {timeout:Math.max(1, deadline-now())});
        if (stopped()) throw Error('navigation_stopped');
        return observedResponse;
      }
      failure = null;
      attempt(number);
      try {
        return await page.goto(url, {waitUntil:'domcontentloaded', timeout:Math.max(1, deadline-now())});
      } catch (error) {
        const delay = number === 1 ? 1000 : 3000;
        if (stopped() || number === 3 || responded || !TRANSIENT.has(failure) || now()+delay >= deadline) throw error;
        await retry(number);
        await pause(delay);
      }
    }
  } finally {
    page.off('requestfailed', failed);
    page.off('response', response);
  }
}
module.exports = {navigate};
