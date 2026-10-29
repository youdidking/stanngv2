/* ===========================================================
   StanNG — shared front-end utilities
   (i18n dictionary, theme/lang persistence, toasts, sounds,
   ripple effect, small fetch helper). No external CDN deps.
   =========================================================== */

const STANNG = (() => {
  const SFX = {
    click: '/static/sfx/click.ogg',
    success: '/static/sfx/success.ogg',
    error: '/static/sfx/error.ogg',
    notify: '/static/sfx/notify.ogg',
    toggle: '/static/sfx/toggle.ogg',
    open: '/static/sfx/open.ogg',
    close: '/static/sfx/close.ogg',
  };
  const audioCache = {};
  let soundEnabled = localStorage.getItem('stanng_sound') !== 'off';

  function playSfx(name, vol = 0.5) {
    if (!soundEnabled) return;
    try {
      const src = SFX[name];
      if (!src) return;
      const a = new Audio(src);
      a.volume = vol;
      a.play().catch(() => {});
    } catch (e) {}
  }

  function setSoundEnabled(v) {
    soundEnabled = v;
    localStorage.setItem('stanng_sound', v ? 'on' : 'off');
  }
  function isSoundEnabled() { return soundEnabled; }

  // ---------------- theme ----------------
  function getTheme() { return localStorage.getItem('stanng_theme') || 'dark'; }
  function setTheme(t) {
    localStorage.setItem('stanng_theme', t);
    document.documentElement.setAttribute('data-theme', t);
  }
  function applyStoredTheme() { setTheme(getTheme()); }

  // ---------------- lang ----------------
  function getLang() { return localStorage.getItem('stanng_lang') || 'fa'; }
  function setLang(l) {
    localStorage.setItem('stanng_lang', l);
    document.documentElement.setAttribute('lang', l);
    document.documentElement.setAttribute('dir', l === 'fa' ? 'rtl' : 'ltr');
    document.body.classList.toggle('lang-en', l === 'en');
    translatePage();
  }

  function t(key) {
    const lang = getLang();
    const dict = window.I18N || {};
    return (dict[key] && dict[key][lang]) || (dict[key] && dict[key].en) || key;
  }

  function translatePage() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = t(key);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      el.setAttribute('placeholder', t(key));
    });
    document.querySelectorAll('.lang-toggle button').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.lang === getLang());
    });
  }

  // ---------------- toasts ----------------
  function ensureToastStack() {
    let stack = document.querySelector('.toast-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.className = 'toast-stack';
      document.body.appendChild(stack);
    }
    return stack;
  }

  const ICONS = {
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
  };

  function toast(message, type = 'info', duration = 3800) {
    const stack = ensureToastStack();
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span class="toast-icon" style="color:var(--${type === 'success' ? 'emerald' : type === 'error' ? 'crimson' : 'azure'})">${ICONS[type] || ICONS.info}</span><span>${message}</span>`;
    stack.appendChild(el);
    if (type === 'success') playSfx('success');
    else if (type === 'error') playSfx('error');
    else playSfx('notify', 0.35);
    setTimeout(() => {
      el.classList.add('leaving');
      setTimeout(() => el.remove(), 220);
    }, duration);
  }

  // ---------------- api helper ----------------
  async function api(path, options = {}) {
    const opts = Object.assign({ headers: {} }, options);
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      if (typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    }
    opts.credentials = 'same-origin';
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { /* not json */ }
    if (!res.ok) {
      const detail = (data && data.detail) || res.statusText || 'error';
      const err = new Error(detail);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return data;
  }

  // ---------------- ripple ----------------
  function attachRipple(el) {
    el.addEventListener('click', function (e) {
      const rect = el.getBoundingClientRect();
      const ripple = document.createElement('span');
      const size = Math.max(rect.width, rect.height);
      ripple.className = 'ripple';
      ripple.style.width = ripple.style.height = size + 'px';
      ripple.style.left = (e.clientX - rect.left - size / 2) + 'px';
      ripple.style.top = (e.clientY - rect.top - size / 2) + 'px';
      el.style.position = el.style.position || 'relative';
      el.appendChild(ripple);
      setTimeout(() => ripple.remove(), 650);
    });
  }
  function initRipples(root = document) {
    root.querySelectorAll('.btn, .icon-btn, .nav-item').forEach(attachRipple);
  }

  // ---------------- sparkle field ----------------
  function initSparkles(container, count = 26) {
    if (!container) return;
    for (let i = 0; i < count; i++) {
      const s = document.createElement('span');
      s.className = 'sparkle';
      const left = Math.random() * 100;
      const delay = Math.random() * 10;
      const dur = 8 + Math.random() * 10;
      const size = 2 + Math.random() * 3;
      s.style.left = left + '%';
      s.style.bottom = '-10px';
      s.style.width = s.style.height = size + 'px';
      s.style.animationDelay = delay + 's';
      s.style.animationDuration = dur + 's';
      container.appendChild(s);
    }
  }

  // ---------------- button loading helper ----------------
  function setLoading(btn, loading) {
    if (!btn) return;
    btn.classList.toggle('is-loading', loading);
    btn.disabled = loading;
    if (loading && !btn.querySelector('.spin')) {
      const spin = document.createElement('span');
      spin.className = 'spin spinner on-dark';
      btn.appendChild(spin);
    }
  }

  function shake(el) {
    if (!el) return;
    el.classList.remove('shake');
    void el.offsetWidth;
    el.classList.add('shake');
  }

  function fmtBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 2) + ' ' + units[i];
  }

  function fmtDuration(seconds) {
    seconds = Math.floor(seconds);
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const parts = [];
    if (d) parts.push(d + 'd');
    if (h || d) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
  }

  return {
    playSfx, setSoundEnabled, isSoundEnabled,
    getTheme, setTheme, applyStoredTheme,
    getLang, setLang, t, translatePage,
    toast, api, initRipples, attachRipple, initSparkles,
    setLoading, shake, fmtBytes, fmtDuration,
  };
})();

document.addEventListener('DOMContentLoaded', () => {
  STANNG.applyStoredTheme();
  document.documentElement.setAttribute('lang', STANNG.getLang());
  document.documentElement.setAttribute('dir', STANNG.getLang() === 'fa' ? 'rtl' : 'ltr');
  document.body.classList.toggle('lang-en', STANNG.getLang() === 'en');
  STANNG.translatePage();
  STANNG.initRipples();
  const field = document.querySelector('.sparkle-field');
  if (field) STANNG.initSparkles(field);
});
