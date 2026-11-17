// Multi Bridge - Background Service Worker
importScripts('port.js');

let mainTabId = null;
let mainWinId = null;
let sentSuccess = false;
let capturedHeaders = {};

// 1. Strict Popup Killer: Kill any secondary tabs or popups immediately
chrome.tabs.query({active: true, currentWindow: true}, function(tabs) {
  if (tabs && tabs[0]) {
    mainTabId = tabs[0].id;
    mainWinId = tabs[0].windowId;
  }
});

chrome.tabs.onCreated.addListener(function(tab) {
  if (mainTabId === null) {
    mainTabId = tab.id;
    mainWinId = tab.windowId;
    return;
  }
  if (tab.id !== mainTabId) {
    console.log('[MultiBridge SW] Neutralizing popup tab:', tab.id, tab.url);
    try { chrome.tabs.remove(tab.id); } catch(e) {}
  }
});

chrome.windows.onCreated.addListener(function(win) {
  if (mainWinId !== null && win.id !== mainWinId) {
    console.log('[MultiBridge SW] Neutralizing popup window:', win.id);
    try { chrome.windows.remove(win.id); } catch(e) {}
  }
});

// Ad and popup domain patterns to cancel
const AD_PATTERNS = [
  'aliexpress.', 'double?', 'adcash.', 'ay267.', 'cdclub.',
  'roadster24.', 'official-app.', 'onclickperf.', 'propellerads.',
  'popunder', 'clickadu', 'adsterra', 'exoclick'
];

function isAdUrl(url) {
  const u = (url || '').toLowerCase();
  return AD_PATTERNS.some(function(p) { return u.includes(p); });
}

// 2. Stream URL detection
function getStreamManifestUrl(rawUrl) {
  if (!rawUrl || rawUrl.startsWith('data:') || rawUrl.startsWith('blob:')) return null;
  const clean = rawUrl.split('?')[0].split('#')[0].toLowerCase();

  if (clean.endsWith('.ts') && !clean.includes('playlist')) return null;

  // Direct DASH or HLS manifests
  if (clean.endsWith('.mpd') || clean.includes('.mpd')) return rawUrl;
  if (clean.endsWith('.m3u8') || clean.includes('.m3u8')) return rawUrl;

  // DASH fragment normalization
  if (clean.includes('/dash/') && (clean.includes('init-') || clean.includes('seg-') || clean.endsWith('.mp4'))) {
    const base = rawUrl.substring(0, rawUrl.lastIndexOf('/') + 1);
    const query = rawUrl.includes('?') ? rawUrl.substring(rawUrl.indexOf('?')) : '';
    return base + 'manifest.mpd' + query;
  }

  // Full video files (not chunks)
  if (!clean.includes('init-') && !clean.includes('seg-') && !clean.includes('chunk-') && !clean.includes('frag-')) {
    if (clean.endsWith('.mp4') || clean.endsWith('.webm') || clean.endsWith('.mkv')) {
      return rawUrl;
    }
  }

  return null;
}

function sendToReceiver(streamUrl, headers, source) {
  if (sentSuccess) return;
  const port = (typeof BRIDGE_PORT !== 'undefined' && BRIDGE_PORT) ? BRIDGE_PORT : 45454;
  console.log('[MultiBridge SW] Sending captured stream to receiver:', streamUrl);

  const mergedHeaders = Object.assign({}, capturedHeaders, headers || {});

  fetch('http://127.0.0.1:' + port + '/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      status: 'ok',
      url: streamUrl,
      headers: mergedHeaders,
      source: source || 'service_worker'
    })
  })
  .then(function(res) {
    if (res.ok) {
      sentSuccess = true;
      console.log('[MultiBridge SW] Stream delivered successfully to Kodi');
    }
  })
  .catch(function(e) {
    console.error('[MultiBridge SW] Fetch failed to 127.0.0.1:', e);
  });
}

// 3. WebRequest interception
try {
  chrome.webRequest.onBeforeRequest.addListener(
    function(details) {
      if (isAdUrl(details.url)) {
        return {cancel: true};
      }
      const stream = getStreamManifestUrl(details.url);
      if (stream) {
        sendToReceiver(stream, {}, 'webRequest_onBeforeRequest');
      }
    },
    {urls: ['<all_urls>']},
    ['blocking']
  );
} catch(e) {
  try {
    chrome.webRequest.onBeforeRequest.addListener(
      function(details) {
        const stream = getStreamManifestUrl(details.url);
        if (stream) {
          sendToReceiver(stream, {}, 'webRequest_onBeforeRequest_nonblocking');
        }
      },
      {urls: ['<all_urls>']}
    );
  } catch(e2) {}
}

try {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    function(details) {
      if (details.requestHeaders) {
        for (const h of details.requestHeaders) {
          capturedHeaders[h.name] = h.value;
        }
      }
      const stream = getStreamManifestUrl(details.url);
      if (stream) {
        sendToReceiver(stream, capturedHeaders, 'webRequest_onBeforeSendHeaders');
      }
    },
    {urls: ['<all_urls>']},
    ['requestHeaders', 'extraHeaders']
  );
} catch(e) {
  try {
    chrome.webRequest.onBeforeSendHeaders.addListener(
      function(details) {
        if (details.requestHeaders) {
          for (const h of details.requestHeaders) {
            capturedHeaders[h.name] = h.value;
          }
        }
        const stream = getStreamManifestUrl(details.url);
        if (stream) {
          sendToReceiver(stream, capturedHeaders, 'webRequest_onBeforeSendHeaders_fallback');
        }
      },
      {urls: ['<all_urls>']},
      ['requestHeaders']
    );
  } catch(e2) {}
}

// Intercept streams identified by Content-Type header
try {
  chrome.webRequest.onHeadersReceived.addListener(
    function(details) {
      if (sentSuccess) return;
      if (details.responseHeaders) {
        for (const h of details.responseHeaders) {
          if (h.name && h.name.toLowerCase() === 'content-type') {
            const val = (h.value || '').toLowerCase();
            if (val.includes('video/') || val.includes('mpegurl') || val.includes('dash+xml')) {
              console.log('[MultiBridge SW] Stream identified by Content-Type:', val, details.url);
              sendToReceiver(details.url, capturedHeaders, 'contentType_' + val);
              break;
            }
          }
        }
      }
    },
    {urls: ['<all_urls>']},
    ['responseHeaders']
  );
} catch(e) {}

// Messages received from content script
chrome.runtime.onMessage.addListener(function(msg, sender, sendResponse) {
  if (msg && msg.url) {
    sendToReceiver(msg.url, capturedHeaders, msg.source || 'content_msg');
  }
});
