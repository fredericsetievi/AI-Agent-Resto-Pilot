/*
 * RestoPilot production adapter (loaded before the original /assets/app.js).
 *
 * In the deployed Atoms app only /api/v1/* reaches the Python backend, where the
 * unchanged RestoPilot FastAPI app is mounted at /api/v1/restopilot. The original
 * dashboard calls same-origin paths such as /v1/overview, so this shim prefixes
 * those requests. Request bodies, headers (Idempotency-Key, X-Demo-Role) and
 * responses are passed through untouched; nothing is mocked.
 */
(function () {
  var PREFIX = '/api/v1/restopilot';
  var ROUTED = /^\/(v1|health|webhooks)(\/|$|\?)/;
  var originalFetch = window.fetch.bind(window);

  function rewrite(url) {
    if (typeof url !== 'string') return url;
    if (url.indexOf(location.origin) === 0) url = url.slice(location.origin.length);
    return ROUTED.test(url) ? PREFIX + url : url;
  }

  window.fetch = function (input, init) {
    if (typeof input === 'string') return originalFetch(rewrite(input), init);
    if (input instanceof URL) return originalFetch(rewrite(input.href), init);
    if (input instanceof Request) {
      var target = rewrite(input.url);
      if (target !== input.url) return originalFetch(new Request(target, input), init);
    }
    return originalFetch(input, init);
  };
})();
