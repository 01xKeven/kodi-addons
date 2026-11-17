// Multi Bridge - Content Script (Runs in all frames)

// 1. Inject page_hook.js into the main page context
try {
  var s = document.createElement('script');
  s.src = chrome.runtime.getURL('page_hook.js');
  s.onload = function() { s.remove(); };
  (document.head || document.documentElement).appendChild(s);
} catch(e) {}

// 2. Synchronize captcha response tokens between textarea and input elements
function syncTokens() {
  try {
    var ta = document.querySelector('textarea[name="g-recaptcha-response"]');
    var inpResp = document.querySelector('input[name="g-recaptcha-response"]');
    var inpToken = document.querySelector('input[name="token"]');
    var val = (ta && ta.value) ? ta.value : (inpResp ? inpResp.value : '');
    if (val && val.length > 20) {
      if (inpResp && inpResp.value !== val) inpResp.value = val;
      if (inpToken && inpToken.value !== val) inpToken.value = val;
    }
  } catch(e) {}
}

// 3. Clean up malicious overlay iframes
setInterval(function() {
  try {
    document.querySelectorAll('iframe').forEach(function(f) {
      if (f.src && (f.src.includes('povv') || f.src.includes('gtent') || f.src.includes('dirpod') || f.src.includes('cdclub') || f.src.includes('ay267'))) {
        f.remove();
      }
    });
    syncTokens();
  } catch(e) {}
}, 250);

document.addEventListener('submit', syncTokens, true);
document.addEventListener('click', function(e) {
  if (e.target && (e.target.id === 'btn_download' || e.target.name === 'imhuman')) {
    syncTokens();
  }
}, true);

// 4. Relay captured stream from page_hook to background and local server
window.addEventListener('message', function(evt) {
  if (evt.data && evt.data.type === 'MULTI_BRIDGE_STREAM_FOUND' && evt.data.url) {
    var streamUrl = evt.data.url;
    var source = evt.data.source || 'content_script';

    // A. Notify background service worker
    try {
      chrome.runtime.sendMessage({
        type: 'STREAM_FOUND',
        url: streamUrl,
        source: source
      }).catch(function() {});
    } catch(e) {}

    // B. Direct local fetch fallback
    try {
      var port = (typeof BRIDGE_PORT !== 'undefined' && BRIDGE_PORT) ? BRIDGE_PORT : 45454;
      fetch('http://127.0.0.1:' + port + '/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          status: 'ok',
          url: streamUrl,
          headers: {
            'User-Agent': navigator.userAgent,
            'Referer': location.href
          },
          source: source
        })
      }).catch(function() {});
    } catch(e) {}
  }
});
