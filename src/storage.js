/**
 * NIGHTLINE — State & KV Storage Layer (CPU-optimized, locked NIGHTLINE identity).
 */

import {
  bytesToHex,
  hashPassword,
  isValidUUID,
  randomPassword,
  randomToken,
  randomUUID,
  safeJsonParse,
  sha224Hex,
} from './crypto.js';

export const PANEL_BRAND = 'NIGHTLINE';
export const PANEL_VERSION = '2.5.3';
export const GITHUB_REPO = 'youdidking/NightLine';
export const GITHUB_REPO_URL = 'https://github.com/youdidking/NightLine';
export const STATE_KEY = 'nl:state:v2';
const CACHE_TTL_MS = 15_000;
const USAGE_FLUSH_INTERVAL_MS = 45_000;
const USAGE_FLUSH_BYTES = 512 * 1024;
const MAX_MAP_SIZE = 1000;

export const DEFAULT_PROXY_IPS = [
  'ProxyIP.NL.CMLiussss.net',
  'ProxyIP.DE.CMLiussss.net',
  'ProxyIP.US.CMLiussss.net',
  'nima.nscl.ir',
];

export const DEFAULT_NAT64_PREFIXES = [
  '2a02:898:146:64::',
  '2602:fc59:b0:64::',
  '2602:fc59:11:64::',
];

export const DEFAULT_SETTINGS = {
  enableVless: true,
  enableTrojan: true,
  killSwitch: false,
  wsPath: '/',
  earlyData: 2048,
  tlsPorts: [443, 8443, 2053, 2083, 2087, 2096],
  httpPorts: [80, 8080, 8880, 2052, 2082, 2086, 2095],
  enableHttpPorts: false,
  fingerprint: 'chrome',
  alpn: 'http/1.1',
  enableECH: false,

  cleanIPs: ['www.speedtest.net#Speedtest'],
  customDomains: [],
  proxyIPs: [...DEFAULT_PROXY_IPS],
  enableNAT64: true,
  nat64Prefixes: [...DEFAULT_NAT64_PREFIXES],
  chainProxy: '',

  enableTgProxy: true,
  tgProxySecret: '',
  tgProxyDomain: '',

  enableFragment: true,
  fragmentMode: 'tlshello',
  fragmentPackets: 'tlshello',
  fragmentLength: '100-200',
  fragmentInterval: '1-3',
  enableNoise: false,

  upstreamDoH: 'https://1.1.1.1/dns-query',
  remoteDnsMode: 'private-doh',
  directDns: '8.8.8.8',

  bypassIran: true,
  bypassLAN: true,
  bypassChina: false,
  bypassRussia: false,
  blockQuic: true,
  blockAds: true,
  blockAdult: false,
  sanctionBypass: true,
  customDirectDomains: [],
  customProxyDomains: [
    'chatgpt.com',
    'openai.com',
    'claude.ai',
    'anthropic.com',
    'gemini.google.com',
    'docker.io',
    'github.com',
    'gitlab.com',
  ],
  customBlockDomains: [],

  showSubInfoNodes: true,
  subUpdateHours: 6,
  rootMode: 'panel',
  fallbackDomain: '',
};

let memCache = null;
let memCacheAt = 0;

const pendingUsage = new Map();
let pendingBytesTotal = 0;
let lastUsageFlushAt = 0;

const loginAttempts = new Map();

export function findKV(env) {
  try {
    if (!env || typeof env !== 'object') return null;
    for (const name of ['KV', 'kv', 'NL_KV', 'NIGHTLINE_KV']) {
      const binding = env[name];
      if (binding && typeof binding.get === 'function' && typeof binding.put === 'function') {
        return { binding, name };
      }
    }
  } catch {}
  return null;
}

export function cleanStringList(arr, maxItems = 100) {
  if (typeof arr === 'string') {
    arr = arr.split(/[\r\n,]+/);
  }
  if (!Array.isArray(arr)) return [];
  const seen = new Set();
  const out = [];
  for (const item of arr) {
    const s = String(item ?? '').trim();
    if (!s || s.length > 260 || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
    if (out.length >= maxItems) break;
  }
  return out;
}

const ALLOWED_TLS_PORTS = [443, 2053, 2083, 2087, 2096, 8443];
const ALLOWED_HTTP_PORTS = [80, 8080, 8880, 2052, 2082, 2086, 2095];
const ALLOWED_FPS = ['chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'randomized'];
const ALLOWED_ALPNS = ['http/1.1', 'h2,http/1.1', 'h2', 'h3'];

function validFragmentRange(value, min, max) {
  if (!/^\d+(-\d+)?$/.test(value)) return false;
  const [lo, hi = lo] = value.split('-').map(Number);
  return lo >= min && hi >= lo && hi <= max;
}

export function sanitizeSettings(patch = {}, current = DEFAULT_SETTINGS) {
  const safePatch = patch && typeof patch === 'object' ? patch : {};
  const rawCur = current && typeof current === 'object' ? current : {};
  const base = { ...DEFAULT_SETTINGS };
  for (const k of Object.keys(DEFAULT_SETTINGS)) {
    if (rawCur[k] !== undefined) base[k] = rawCur[k];
  }
  const next = { ...base, showSubInfoNodes: true };

  for (const boolKey of [
    'enableVless', 'enableTrojan', 'killSwitch', 'enableHttpPorts', 'enableECH',
    'enableNAT64', 'enableTgProxy', 'enableFragment', 'enableNoise', 'bypassIran', 'bypassLAN',
    'bypassChina', 'bypassRussia', 'blockQuic', 'blockAds', 'blockAdult',
    'sanctionBypass',
  ]) {
    if (typeof safePatch[boolKey] === 'boolean') next[boolKey] = safePatch[boolKey];
  }

  if (typeof safePatch.tgProxySecret === 'string') {
    const cleanSec = safePatch.tgProxySecret.trim().toLowerCase();
    if (/^[0-9a-f]{32}$/.test(cleanSec)) {
      next.tgProxySecret = cleanSec;
    } else if (/^(?:dd|ee|ef)[0-9a-f]{32}$/.test(cleanSec)) {
      next.tgProxySecret = cleanSec.slice(-32);
    } else if (!cleanSec) {
      next.tgProxySecret = bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
    }
  }
  if (!/^[0-9a-f]{32}$/.test(String(next.tgProxySecret || ''))) {
    next.tgProxySecret = bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
  }

  if (typeof safePatch.tgProxyDomain === 'string') {
    next.tgProxyDomain = safePatch.tgProxyDomain
      .trim()
      .toLowerCase()
      .replace(/^https?:\/\//i, '')
      .replace(/\/.*$/, '')
      .replace(/[^a-z0-9.-]/g, '')
      .slice(0, 120);
  }

  if (typeof safePatch.wsPath === 'string') {
    let p = safePatch.wsPath.trim().split('?')[0] || '/';
    if (!p.startsWith('/')) p = '/' + p;
    next.wsPath = p.slice(0, 64);
  }

  if (safePatch.earlyData !== undefined) {
    const ed = Number(safePatch.earlyData);
    next.earlyData = [0, 1024, 2048, 2560].includes(ed) ? ed : 2048;
  }

  if (Array.isArray(safePatch.tlsPorts)) {
    const ports = safePatch.tlsPorts.map(Number).filter((p) => ALLOWED_TLS_PORTS.includes(p));
    next.tlsPorts = ports.length ? [...new Set(ports)] : [443];
  }

  if (Array.isArray(safePatch.httpPorts)) {
    const ports = safePatch.httpPorts.map(Number).filter((p) => ALLOWED_HTTP_PORTS.includes(p));
    next.httpPorts = ports.length ? [...new Set(ports)] : [80];
  }

  if (typeof safePatch.fingerprint === 'string' && ALLOWED_FPS.includes(safePatch.fingerprint)) {
    next.fingerprint = safePatch.fingerprint;
  }
  if (typeof safePatch.alpn === 'string' && ALLOWED_ALPNS.includes(safePatch.alpn)) {
    next.alpn = safePatch.alpn;
  }

  for (const listKey of [
    'cleanIPs', 'customDomains', 'proxyIPs', 'nat64Prefixes',
    'customDirectDomains', 'customProxyDomains', 'customBlockDomains',
  ]) {
    if (safePatch[listKey] !== undefined) {
      next[listKey] = cleanStringList(safePatch[listKey], 80);
    }
  }

  if (typeof safePatch.chainProxy === 'string') {
    next.chainProxy = safePatch.chainProxy.trim().slice(0, 240);
  }

  if (typeof safePatch.fragmentMode === 'string' && ['severe', 'tlshello', 'mild', 'custom'].includes(safePatch.fragmentMode)) {
    next.fragmentMode = safePatch.fragmentMode;
  }
  if (typeof safePatch.fragmentPackets === 'string') {
    const fp = safePatch.fragmentPackets.trim();
    if (fp === 'tlshello' || validFragmentRange(fp, 1, 65535)) next.fragmentPackets = fp;
  }
  if (typeof safePatch.fragmentLength === 'string') {
    const fl = safePatch.fragmentLength.trim();
    if (validFragmentRange(fl, 1, 16384)) next.fragmentLength = fl;
  }
  if (typeof safePatch.fragmentInterval === 'string') {
    const fi = safePatch.fragmentInterval.trim();
    if (validFragmentRange(fi, 0, 1000)) next.fragmentInterval = fi;
  }

  if (typeof safePatch.upstreamDoH === 'string') {
    const u = safePatch.upstreamDoH.trim();
    try {
      const parsed = new URL(u);
      if (parsed.protocol === 'https:') next.upstreamDoH = parsed.toString().slice(0, 200);
    } catch {}
  }
  if (typeof safePatch.remoteDnsMode === 'string' && ['private-doh', 'cloudflare', 'google', 'quad9'].includes(safePatch.remoteDnsMode)) {
    next.remoteDnsMode = safePatch.remoteDnsMode;
  }
  if (typeof safePatch.directDns === 'string' && safePatch.directDns.trim()) {
    next.directDns = safePatch.directDns.trim().slice(0, 64);
  }

  if (safePatch.subUpdateHours !== undefined) {
    next.subUpdateHours = Math.max(1, Math.min(72, Number(safePatch.subUpdateHours) || 6));
  }

  if (typeof safePatch.rootMode === 'string' && ['panel', 'gazette', 'fallback'].includes(safePatch.rootMode)) {
    next.rootMode = safePatch.rootMode;
  }
  if (typeof safePatch.fallbackDomain === 'string') {
    next.fallbackDomain = safePatch.fallbackDomain
      .trim()
      .replace(/^https?:\/\//i, '')
      .replace(/\/.*$/, '')
      .replace(/[^a-zA-Z0-9.:_-]/g, '')
      .slice(0, 120);
  }

  return next;
}

export function normalizeUser(raw = {}, trustExistingHash = false) {
  const safe = raw && typeof raw === 'object' ? raw : {};
  const now = Date.now();
  const uuid = isValidUUID(safe.uuid) ? String(safe.uuid).trim().toLowerCase() : randomUUID();
  const rawPass = String(safe.trojanPassword || '').trim();
  const trojanPassword = rawPass || randomPassword(14);
  const trojanHash =
    trustExistingHash && rawPass && typeof safe.trojanHash === 'string' && /^[0-9a-f]{56}$/.test(safe.trojanHash)
      ? safe.trojanHash
      : sha224Hex(trojanPassword);

  const subToken = String(safe.subToken || '').trim().replace(/[^a-zA-Z0-9_-]/g, '') || randomToken(16);
  const expiryDays = Math.max(0, Number.isFinite(Number(safe.expiryDays)) ? Number(safe.expiryDays) : 0);
  const startOnFirstUse = Boolean(safe.startOnFirstUse);
  let expiresAt = Math.max(0, Number.isFinite(Number(safe.expiresAt)) ? Number(safe.expiresAt) : 0);

  if (expiryDays > 0 && !startOnFirstUse && !expiresAt) {
    expiresAt = now + expiryDays * 86_400_000;
  } else if (expiryDays === 0 && safe.clearExpiry) {
    expiresAt = 0;
  }

  const safeNum = (v) => Math.max(0, Number.isFinite(Number(v)) ? Number(v) : 0);

  return {
    id: String(safe.id || '').trim() || `u_${randomToken(10)}`,
    name: String(safe.name || '').trim().slice(0, 48) || 'مشترک اول',
    note: String(safe.note || '').trim().slice(0, 160),
    enabled: safe.enabled !== false,
    uuid,
    trojanPassword,
    trojanHash,
    subToken,
    trafficLimitGB: safeNum(safe.trafficLimitGB),
    usedUpBytes: safeNum(safe.usedUpBytes),
    usedDownBytes: safeNum(safe.usedDownBytes),
    connections: safeNum(safe.connections),
    expiryDays,
    expiresAt,
    startOnFirstUse,
    firstConnectedAt: safeNum(safe.firstConnectedAt),
    lastSeenAt: safeNum(safe.lastSeenAt),
    createdAt: safeNum(safe.createdAt) || now,
  };
}

export function evaluateUserStatus(user, now = Date.now()) {
  if (!user || typeof user !== 'object') {
    return {
      ok: false,
      status: 'paused',
      usedUpBytes: 0,
      usedDownBytes: 0,
      usedBytes: 0,
      limitBytes: 0,
      connections: 0,
      lastSeenAt: 0,
      firstConnectedAt: 0,
      effectiveExpiresAt: 0,
      remainingDays: 0,
    };
  }

  const pend = pendingUsage.get(user.id);
  const usedUp = (user.usedUpBytes || 0) + (pend ? pend.up : 0);
  const usedDown = (user.usedDownBytes || 0) + (pend ? pend.down : 0);
  const usedBytes = usedUp + usedDown;
  const limitBytes = user.trafficLimitGB > 0 ? Math.round(user.trafficLimitGB * 1073741824) : 0;
  const firstConn = user.firstConnectedAt || (pend ? pend.firstSeen : 0) || 0;

  let effectiveExpiresAt = user.expiresAt || 0;
  if (user.startOnFirstUse && user.expiryDays > 0) {
    effectiveExpiresAt = firstConn > 0 ? firstConn + user.expiryDays * 86_400_000 : 0;
  }

  let status = 'active';
  if (!user.enabled) status = 'paused';
  else if (effectiveExpiresAt > 0 && now > effectiveExpiresAt) status = 'expired';
  else if (limitBytes > 0 && usedBytes >= limitBytes) status = 'quota';

  const remainingDays = effectiveExpiresAt > 0
    ? Math.max(0, Math.ceil((effectiveExpiresAt - now) / 86_400_000))
    : (user.startOnFirstUse && user.expiryDays > 0 ? user.expiryDays : null);

  return {
    ok: status === 'active',
    status,
    usedUpBytes: usedUp,
    usedDownBytes: usedDown,
    usedBytes,
    limitBytes,
    connections: (user.connections || 0) + (pend ? pend.conns : 0),
    lastSeenAt: Math.max(user.lastSeenAt || 0, pend ? pend.lastSeen : 0),
    firstConnectedAt: firstConn,
    effectiveExpiresAt,
    remainingDays,
  };
}

export function enrichUserView(user, now = Date.now()) {
  const st = evaluateUserStatus(user, now);
  return {
    ...(user || {}),
    usedUpBytes: st.usedUpBytes,
    usedDownBytes: st.usedDownBytes,
    usedBytes: st.usedBytes,
    limitBytes: st.limitBytes,
    connections: st.connections,
    lastSeenAt: st.lastSeenAt,
    firstConnectedAt: st.firstConnectedAt,
    effectiveExpiresAt: st.effectiveExpiresAt,
    remainingDays: st.remainingDays,
    status: st.status,
  };
}

function buildEnvFallbackState(env, kvInfo) {
  const envUuid = String(env?.uuid || env?.UUID || '').trim();
  const validEnvUuid = isValidUUID(envUuid) ? envUuid.toLowerCase() : '';
  const proxyRaw = String(env?.PROXYIP || env?.PROXY_IP || '').trim();
  const trojanPass = String(env?.TROJAN_PASSWORD || env?.TR_PASS || '').trim() || (validEnvUuid ? validEnvUuid.slice(0, 14) : '');
  const tgSecretEnv = String(env?.TG_SECRET || '').trim().toLowerCase();
  const deterministicTgSecret = /^[0-9a-f]{32}$/.test(tgSecretEnv)
    ? tgSecretEnv
    : sha224Hex(`nl-tg-stateless:${validEnvUuid || 'nightline'}`).slice(0, 32);

  const settings = sanitizeSettings({
    proxyIPs: proxyRaw ? proxyRaw.split(',').map((s) => s.trim()).filter(Boolean) : DEFAULT_SETTINGS.proxyIPs,
    tgProxySecret: deterministicTgSecret,
  });

  const users = validEnvUuid
    ? [
        normalizeUser({
          id: 'u_env',
          name: 'خط اصلی (ENV)',
          uuid: validEnvUuid,
          trojanPassword: trojanPass,
          subToken: validEnvUuid,
        }),
      ]
    : [];

  return {
    mode: kvInfo ? 'uninitialized' : 'stateless',
    kvBound: Boolean(kvInfo),
    kvName: kvInfo ? kvInfo.name : null,
    initialized: !kvInfo && Boolean(validEnvUuid),
    envUuid: validEnvUuid || null,
    auth: null,
    settings,
    users,
    logs: [],
  };
}

export async function loadRuntimeState(env, forceRefresh = false) {
  const kvInfo = findKV(env);
  if (!kvInfo) {
    return buildEnvFallbackState(env, null);
  }

  const now = Date.now();
  if (!forceRefresh && memCache && now - memCacheAt < CACHE_TTL_MS) {
    return memCache;
  }

  let raw = null;
  try {
    const rawText = await kvInfo.binding.get(STATE_KEY, 'text');
    raw = safeJsonParse(rawText, null);
  } catch {
    if (memCache) return memCache;
    return buildEnvFallbackState(env, kvInfo);
  }

  if (!raw || typeof raw !== 'object' || !raw.auth || typeof raw.auth !== 'object') {
    const fallback = buildEnvFallbackState(env, kvInfo);
    memCache = fallback;
    memCacheAt = now;
    return fallback;
  }

  const rawSettings = raw.settings && typeof raw.settings === 'object' ? { ...raw.settings } : {};
  if (!rawSettings.tgProxySecret) {
    rawSettings.tgProxySecret = sha224Hex(`nl-tg-kv:${raw.auth.secret || 'nightline'}`).slice(0, 32);
  }
  if ((Number(raw.version) || 2) < 3) {
    if (!Array.isArray(rawSettings.proxyIPs) || rawSettings.proxyIPs.length === 0) {
      rawSettings.proxyIPs = [...DEFAULT_PROXY_IPS];
    }
    if (!rawSettings.enableNAT64) {
      rawSettings.enableNAT64 = true;
    }
    if (!Array.isArray(rawSettings.nat64Prefixes) || rawSettings.nat64Prefixes.includes('64:ff9b::')) {
      rawSettings.nat64Prefixes = [...DEFAULT_NAT64_PREFIXES];
    }
  }

  const rawCfApi = raw.cfApi && typeof raw.cfApi === 'object' ? raw.cfApi : {};
  const cfApi = {
    token: String(rawCfApi.token || '').trim(),
    accountId: String(rawCfApi.accountId || '').trim(),
    accountName: String(rawCfApi.accountName || '').trim(),
    scriptName: String(rawCfApi.scriptName || '').trim(),
    dailyLimit: Math.max(1000, Number(rawCfApi.dailyLimit) || 100000),
    canEditWorkers: Boolean(rawCfApi.canEditWorkers),
    updatedAt: Number(rawCfApi.updatedAt) || 0,
  };

  const state = {
    mode: 'kv',
    kvBound: true,
    kvName: kvInfo.name,
    initialized: true,
    envUuid: null,
    auth: raw.auth,
    cfApi,
    settings: sanitizeSettings(rawSettings, DEFAULT_SETTINGS),
    users: Array.isArray(raw.users) ? raw.users.map((u) => normalizeUser(u, true)) : [],
    logs: Array.isArray(raw.logs) ? raw.logs.slice(0, 40) : [],
  };

  memCache = state;
  memCacheAt = now;
  return state;
}

export function clearPendingUsage(userId) {
  if (!userId) return;
  const pend = pendingUsage.get(userId);
  if (pend) {
    pendingBytesTotal = Math.max(0, pendingBytesTotal - (pend.up || 0) - (pend.down || 0));
    pendingUsage.delete(userId);
  }
}

export async function saveRuntimeState(env, nextState) {
  const kvInfo = findKV(env);
  if (!kvInfo) throw new Error('فضای ذخیره‌سازی KV به ورکر متصل نیست (Settings → Bindings).');

  const mergedUsers = (nextState?.users || []).map((u) => {
    const norm = normalizeUser(u);
    const pend = pendingUsage.get(norm.id);
    if (pend) {
      norm.usedUpBytes += pend.up;
      norm.usedDownBytes += pend.down;
      norm.connections += pend.conns;
      if (!norm.firstConnectedAt && pend.firstSeen) norm.firstConnectedAt = pend.firstSeen;
      if (pend.lastSeen > norm.lastSeenAt) norm.lastSeenAt = pend.lastSeen;
      pendingUsage.delete(norm.id);
    }
    return norm;
  });
  pendingBytesTotal = 0;

  const rawCf = nextState?.cfApi && typeof nextState.cfApi === 'object' ? nextState.cfApi : {};
  const cfApiDoc = {
    token: String(rawCf.token || '').trim(),
    accountId: String(rawCf.accountId || '').trim(),
    accountName: String(rawCf.accountName || '').trim(),
    scriptName: String(rawCf.scriptName || '').trim(),
    dailyLimit: Math.max(1000, Number(rawCf.dailyLimit) || 100000),
    canEditWorkers: Boolean(rawCf.canEditWorkers),
    updatedAt: Number(rawCf.updatedAt) || 0,
  };

  const doc = {
    version: 3,
    updatedAt: Date.now(),
    auth: nextState.auth,
    cfApi: cfApiDoc,
    settings: sanitizeSettings(nextState.settings || {}, DEFAULT_SETTINGS),
    users: mergedUsers,
    logs: Array.isArray(nextState.logs) ? nextState.logs.slice(0, 40) : [],
  };

  try {
    await kvInfo.binding.put(STATE_KEY, JSON.stringify(doc));
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    throw new Error(`ذخیره‌سازی در KV ناموفق بود (احتمال پر شدن سقف نوشتن روزانه): ${msg}`);
  }

  const hydrated = {
    mode: 'kv',
    kvBound: true,
    kvName: kvInfo.name,
    initialized: true,
    envUuid: null,
    auth: doc.auth,
    cfApi: doc.cfApi,
    settings: doc.settings,
    users: doc.users,
    logs: doc.logs,
  };
  memCache = hydrated;
  memCacheAt = Date.now();
  return hydrated;
}

export async function initializePanelState(env, { password, firstUserName, preferredUuid } = {}) {
  const pwRecord = await hashPassword(password);
  const secretBytes = crypto.getRandomValues(new Uint8Array(32));
  const envUuid = String(env?.uuid || env?.UUID || '').trim();
  const initialUuid = isValidUUID(preferredUuid)
    ? String(preferredUuid).toLowerCase()
    : (isValidUUID(envUuid) ? envUuid.toLowerCase() : randomUUID());

  const firstUser = normalizeUser({
    name: firstUserName || 'سردبیر (اصلی)',
    note: 'حسابِ پیش‌فرض مدیریت',
    uuid: initialUuid,
    trojanPassword: randomPassword(14),
    subToken: randomToken(16),
  });

  const proxyRaw = String(env?.PROXYIP || env?.PROXY_IP || '').trim();
  const initialSettings = sanitizeSettings({
    proxyIPs: proxyRaw ? proxyRaw.split(',').map((s) => s.trim()).filter(Boolean) : DEFAULT_SETTINGS.proxyIPs,
  });

  return saveRuntimeState(env, {
    auth: {
      ...pwRecord,
      secret: bytesToHex(secretBytes),
      ver: 1,
      createdAt: Date.now(),
    },
    settings: initialSettings,
    users: [firstUser],
    logs: [{ ts: Date.now(), event: 'setup', detail: 'راه‌اندازی اولیه پنل NIGHTLINE انجام شد' }],
  });
}

export function appendLog(state, event, detail = '') {
  if (!state || typeof state !== 'object') return;
  const entry = { ts: Date.now(), event: String(event || 'info'), detail: String(detail || '').slice(0, 140) };
  state.logs = [entry, ...(Array.isArray(state.logs) ? state.logs : [])].slice(0, 40);
}

export function recordTraffic(env, ctx, user, upBytes, downBytes) {
  try {
    if (!user || !user.id) return;
    if (pendingUsage.size >= MAX_MAP_SIZE && !pendingUsage.has(user.id)) {
      const oldestKey = pendingUsage.keys().next().value;
      if (oldestKey) pendingUsage.delete(oldestKey);
    }

    const now = Date.now();
    const prev = pendingUsage.get(user.id) || {
      up: 0,
      down: 0,
      conns: 0,
      firstSeen: now,
      lastSeen: now,
    };
    const safeUp = Math.max(0, Number(upBytes) || 0);
    const safeDown = Math.max(0, Number(downBytes) || 0);
    prev.up += safeUp;
    prev.down += safeDown;
    prev.conns += 1;
    prev.lastSeen = now;
    pendingUsage.set(user.id, prev);
    pendingBytesTotal += safeUp + safeDown;

    const kvInfo = findKV(env);
    if (!kvInfo || !memCache || !memCache.initialized) return;

    const isFirstEverConn = !user.firstConnectedAt;
    const shouldFlush =
      isFirstEverConn ||
      (pendingBytesTotal >= USAGE_FLUSH_BYTES && now - lastUsageFlushAt >= USAGE_FLUSH_INTERVAL_MS);

    if (shouldFlush && ctx && typeof ctx.waitUntil === 'function') {
      lastUsageFlushAt = now;
      const bgPromise = (async () => {
        try {
          const fresh = await loadRuntimeState(env, true);
          if (fresh && fresh.mode === 'kv') {
            await saveRuntimeState(env, fresh);
          }
        } catch {}
      })();
      try {
        ctx.waitUntil(bgPromise);
      } catch {}
    }
  } catch {}
}

function pruneLoginAttempts(now) {
  if (loginAttempts.size < MAX_MAP_SIZE) return;
  for (const [k, rec] of loginAttempts.entries()) {
    if (now - rec.lastAt > 600_000 && (!rec.blockedUntil || now > rec.blockedUntil)) {
      loginAttempts.delete(k);
    }
    if (loginAttempts.size < MAX_MAP_SIZE / 2) break;
  }
  if (loginAttempts.size >= MAX_MAP_SIZE) {
    const first = loginAttempts.keys().next().value;
    if (first) loginAttempts.delete(first);
  }
}

export function checkLoginRateLimit(ip) {
  const key = String(ip || 'unknown');
  const rec = loginAttempts.get(key);
  if (!rec) return { allowed: true };
  const now = Date.now();
  if (rec.blockedUntil && now < rec.blockedUntil) {
    const retrySec = Math.max(1, Math.ceil((rec.blockedUntil - now) / 1000));
    return { allowed: false, retrySec };
  }
  return { allowed: true };
}

export function recordLoginFailure(ip) {
  const key = String(ip || 'unknown');
  const now = Date.now();
  pruneLoginAttempts(now);
  const rec = loginAttempts.get(key) || { fails: 0, lastAt: 0, blockedUntil: 0 };
  if (now - rec.lastAt > 600_000) rec.fails = 0;
  rec.fails += 1;
  rec.lastAt = now;
  if (rec.fails >= 6) {
    rec.blockedUntil = now + 600_000;
  }
  loginAttempts.set(key, rec);
}

export function clearLoginFailures(ip) {
  loginAttempts.delete(String(ip || 'unknown'));
}
