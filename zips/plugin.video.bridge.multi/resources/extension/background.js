let BRIDGE_PORT = 45454;

try {
  fetch(chrome.runtime.getURL('port.json'))
    .then(r => r.json())
    .then(data => { if (data && data.port) BRIDGE_PORT = data.port; })
    .catch(() => {});
} catch(e) {}

function getStreamManifestUrl(rawUrl) {
  if (!rawUrl || rawUrl.startsWith('data:') || rawUrl.startsWith('blob:')) return null;
  const clean = rawUrl.split('?')[0].split('#')[0].toLowerCase();
  
  if (clean.endsWith('.ts')) return null;

  // Direct DASH or HLS manifests
  if (clean.endsWith('.mpd') || clean.includes('.mpd')) return rawUrl;
  if (clean.endsWith('.m3u8') || clean.includes('.m3u8')) return rawUrl;

  // If a DASH fragment is loaded (e.g. /dash/.../init-a1-x3.mp4 or seg-...)
  if (clean.includes('/dash/') && (clean.includes('init-') || clean.includes('seg-') || clean.endsWith('.mp4'))) {
    const base = rawUrl.substring(0, rawUrl.lastIndexOf('/') + 1);
    const query = rawUrl.includes('?') ? rawUrl.substring(rawUrl.indexOf('?')) : '';
    return base + 'manifest.mpd' + query;
  }

  // Direct standard video files (full videos, not chunks)
  if (!clean.includes('init-') && !clean.includes('seg-') && !clean.includes('chunk-') && !clean.includes('frag-')) {
    if (clean.endsWith('.mp4') || clean.endsWith('.webm') || clean.endsWith('.mkv')) {
      return rawUrl;
    }
  }

  return null;
}

let sent = false;

function handleRequest(details) {
  if (sent) return;
  const manifestUrl = getStreamManifestUrl(details.url);
  if (manifestUrl) {
    sent = true;
    const headers = {};
    if (details.requestHeaders) {
      for (const h of details.requestHeaders) {
        headers[h.name] = h.value;
      }
    }
    fetch(`http://127.0.0.1:${BRIDGE_PORT}/stream`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        status: 'ok',
        url: manifestUrl,
        headers: headers,
        source: 'extension'
      })
    }).catch(() => {});
  }
}

try {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    handleRequest,
    {urls: ["<all_urls>"]},
    ["requestHeaders", "extraHeaders"]
  );
} catch(e) {
  try {
    chrome.webRequest.onBeforeSendHeaders.addListener(
      handleRequest,
      {urls: ["<all_urls>"]},
      ["requestHeaders"]
    );
  } catch(e2) {
    chrome.webRequest.onBeforeRequest.addListener(
      function(details) {
        if (sent) return;
        const manifestUrl = getStreamManifestUrl(details.url);
        if (manifestUrl) {
          sent = true;
          fetch(`http://127.0.0.1:${BRIDGE_PORT}/stream`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              status: 'ok',
              url: manifestUrl,
              headers: {},
              source: 'extension_request'
            })
          }).catch(() => {});
        }
      },
      {urls: ["<all_urls>"]}
    );
  }
}
