// Multi Bridge - Page Hook (Injected directly into page window context)
(function() {
  if (window.__multi_bridge_hooked) return;
  window.__multi_bridge_hooked = true;

  // 1. Strict Popup & Tab Blocker
  window.open = function(url, target, features) {
    console.log("[MultiBridge] Neutralized window.open popup:", url);
    return null;
  };
  window.showModalDialog = function() { return null; };

  try {
    Object.defineProperty(window, 'puOverlay', {
      get: function() { return function() { return false; }; },
      set: function() {},
      configurable: false
    });
  } catch(e) {}

  // Intercept click on links targeting new tabs or ad networks
  document.addEventListener('click', function(e) {
    var el = e.target;
    while (el && el !== document.body) {
      if (el.tagName === 'A') {
        if (el.target === '_blank' || el.target === '_new') {
          el.target = '_self';
        }
        var href = (el.href || '').toLowerCase();
        if (href && (href.startsWith('http://') || href.startsWith('https://'))) {
          if (!href.includes(location.hostname)) {
            if (href.match(/(aliexpress|click|double\?|adcash|propeller|popunder|ay267|cdclub|roadster|official-app|onclickperf|bet\d+|casino)/i)) {
              console.log('[MultiBridge] Blocked ad click:', href);
              e.preventDefault();
              e.stopPropagation();
              return false;
            }
          }
        }
      }
      el = el.parentElement;
    }
  }, true);

  // 2. Stream Interceptor
  function notifyStream(url, source) {
    if (!url || typeof url !== 'string') return;
    if (url.startsWith('data:') || url.startsWith('blob:')) return;
    var uLow = url.toLowerCase();
    // Exclude analytics, beacons, subtitles, images, css, js
    if (uLow.endsWith('.vtt') || uLow.endsWith('.srt') || uLow.endsWith('.jpg') || uLow.endsWith('.png') || uLow.endsWith('.js') || uLow.endsWith('.css')) return;
    if (uLow.includes('google-analytics') || uLow.includes('doubleclick') || uLow.includes('recaptcha') || uLow.includes('turnstile')) return;

    window.postMessage({
      type: 'MULTI_BRIDGE_STREAM_FOUND',
      url: url,
      source: source || 'page_hook'
    }, '*');
  }

  // Hook HTMLMediaElement
  try {
    var origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function() {
      var s = this.currentSrc || this.src;
      if (s && (s.startsWith('http://') || s.startsWith('https://'))) {
        notifyStream(s, 'media_play');
      }
      return origPlay.apply(this, arguments);
    };

    var srcDesc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    if (srcDesc && srcDesc.set) {
      var origSrcSet = srcDesc.set;
      Object.defineProperty(HTMLMediaElement.prototype, 'src', {
        set: function(val) {
          if (val && typeof val === 'string' && (val.startsWith('http://') || val.startsWith('https://'))) {
            notifyStream(val, 'media_src_set');
          }
          return origSrcSet.apply(this, arguments);
        },
        get: srcDesc.get,
        configurable: true
      });
    }
  } catch(e) {}

  // Hook fetch
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function(input, init) {
      try {
        var u = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
        if (u && typeof u === 'string') {
          var uLow = u.toLowerCase();
          if (uLow.includes('.m3u8') || uLow.includes('.mpd') || uLow.includes('.mp4') || uLow.includes('/dash/') || uLow.includes('/hls/')) {
            notifyStream(u, 'page_fetch');
          }
        }
      } catch(e) {}
      return origFetch.apply(this, arguments);
    };
  }

  // Hook XHR
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    try {
      if (url && typeof url === 'string') {
        var uLow = url.toLowerCase();
        if (uLow.includes('.m3u8') || uLow.includes('.mpd') || uLow.includes('.mp4') || uLow.includes('/dash/') || uLow.includes('/hls/')) {
          notifyStream(url, 'page_xhr');
        }
      }
    } catch(e) {}
    return origOpen.apply(this, arguments);
  };

  // Continuous DOM check for active video elements
  setInterval(function() {
    try {
      var vids = document.querySelectorAll('video, audio');
      vids.forEach(function(el) {
        var s = el.currentSrc || el.src;
        if (s && (s.startsWith('http://') || s.startsWith('https://'))) {
          notifyStream(s, 'dom_video_poll');
        }
        var sources = el.querySelectorAll('source');
        sources.forEach(function(sc) {
          var scSrc = sc.src;
          if (scSrc && (scSrc.startsWith('http://') || scSrc.startsWith('https://'))) {
            notifyStream(scSrc, 'dom_source_poll');
          }
        });
      });
    } catch(e) {}
  }, 400);
})();
