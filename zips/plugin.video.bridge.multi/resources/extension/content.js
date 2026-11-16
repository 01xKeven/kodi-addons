// Neutralize popunder overlay in page context
try {
  const s = document.createElement('script');
  s.textContent = 'window.puOverlay = function() { return false; };';
  (document.head || document.documentElement).appendChild(s);
  s.remove();
} catch(e) {}

function syncTokens() {
  try {
    const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
    const inpResp = document.querySelector('input[name="g-recaptcha-response"]');
    const inpToken = document.querySelector('input[name="token"]');
    const val = (ta && ta.value) ? ta.value : (inpResp ? inpResp.value : '');
    if (val && val.length > 20) {
      if (inpResp && inpResp.value !== val) inpResp.value = val;
      if (inpToken && inpToken.value !== val) inpToken.value = val;
    }
  } catch(e) {}
}

// Continuously remove malicious overlay ads and sync tokens
setInterval(function() {
  try {
    document.querySelectorAll('iframe').forEach(function(f) {
      if (f.src && (f.src.includes('povv') || f.src.includes('gtent') || f.src.includes('dirpod'))) {
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
