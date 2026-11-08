/**
 * NIGHTLINE — Native Telegram Official Web Proxy Engine (`tg://webproxy` / `tproxy-lane-v1`).
 *
 * Implements Telegram Desktop & Telegram Android's official Web Proxy bridge (`td web-proxy`):
 *   1. `GET /?bridge=<43-char-HMAC>` -> Strict-CSP bridge script (Desktop MessagePort + Android WebView)
 *   2. `POST|DELETE /api/v1/session` -> HMAC-SHA256 bootstrap & session lease management
 *   3. `GET /api/v1/ws` (`tproxy-lane-v1.<token>.<sid>`) -> MTProto transport obfuscation re-signing
 *      relayed over Telegram's official WebSocket KWS endpoints (`wss://kws{1..5}[-1].web.telegram.org/apiws`).
 *
 * End-to-end MTProto encryption remains untouched between the Telegram client and Telegram servers.
 */

import { DEC, ENC } from './crypto.js';

const DN_PACK = 32 * 1024;
const UP_PACK = 20 * 1024;
const DN_MS = 1;
const EMPTY_U8 = new Uint8Array(0);
const BAD_HEADS = new Set(['HEAD', 'POST', 'GET ', 'OPTI']);

const macKeyCache = new Map();
const sessionLeases = new Map();

function toU8(v) {
  if (v instanceof Uint8Array && v.constructor === Uint8Array) return v;
  if (v instanceof ArrayBuffer) return new Uint8Array(v);
  if (ArrayBuffer.isView(v)) return new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
  throw new TypeError('Expected binary buffer');
}

function concatU8(...parts) {
  const valid = parts.filter((p) => p && p.byteLength).map(toU8);
  const total = valid.reduce((sum, p) => sum + p.byteLength, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of valid) {
    out.set(p, off);
    off += p.byteLength;
  }
  return out;
}

function hexToU8(hex) {
  return Uint8Array.from({ length: hex.length / 2 }, (_, i) =>
    Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16)
  );
}

function u8ToB64Url(v) {
  const b = v instanceof Uint8Array ? v : new Uint8Array(v);
  let bin = '';
  for (let i = 0; i < b.byteLength; i++) bin += String.fromCharCode(b[i]);
  return btoa(bin).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function b64UrlToU8(str) {
  const s = str.replaceAll('-', '+').replaceAll('_', '/');
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function randBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

export function normalizeTgSecret(raw) {
  const clean = String(raw || '').trim().toLowerCase();
  if (/^[0-9a-f]{32}$/.test(clean)) return clean;
  if (/^(?:dd|ee|ef)[0-9a-f]{32}$/.test(clean)) return clean.slice(-32);
  return '';
}

export function generateTgSecret() {
  const b = randBytes(16);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

export function buildTgProxyLinks(host, secret, overrideDomain = '') {
  const targetHost = String(overrideDomain || host || '').trim().split(':')[0];
  const cleanSecret = normalizeTgSecret(secret);
  if (!targetHost || !cleanSecret) {
    return { tgUrl: '', tmeUrl: '', server: targetHost, secret: cleanSecret };
  }
  const qs = `server=${encodeURIComponent(targetHost)}&secret=${cleanSecret}`;
  return {
    tgUrl: `tg://webproxy?${qs}`,
    tmeUrl: `https://t.me/webproxy?${qs}`,
    server: targetHost,
    secret: cleanSecret,
  };
}

function parseSecretKey(v, keepDdPrefix = false) {
  const str = String(v || '').trim().toLowerCase();
  if (!/^(?:[0-9a-f]{32}|(?:dd|ee|ef)[0-9a-f]{32})$/.test(str)) {
    throw new Error('Invalid Telegram proxy secret');
  }
  return hexToU8(keepDdPrefix && str.length === 34 && str.startsWith('dd') ? str : str.slice(-32));
}

function validateHostName(v) {
  const host = String(v || '').trim().toLowerCase().replace(/\.$/, '');
  const labels = host.split('.');
  if (
    !host.includes('.') ||
    host.length > 253 ||
    host.includes(':') ||
    labels.some((lbl) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(lbl))
  ) {
    throw new Error('Invalid host');
  }
  return host;
}

function getHmacKey(rawBytes, cacheId) {
  let k = macKeyCache.get(cacheId);
  if (!k) {
    if (macKeyCache.size > 128) macKeyCache.clear();
    k = crypto.subtle.importKey('raw', toU8(rawBytes), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    macKeyCache.set(cacheId, k);
  }
  return k;
}

async function hmacSign(keyPromise, data) {
  const key = await keyPromise;
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, toU8(data)));
}

async function deriveBootSeed(secret) {
  // Deterministic per-secret boot seed so multi-isolate Cloudflare Workers share valid session tokens
  const digest = await crypto.subtle.digest('SHA-256', ENC.encode(`nl-tgproxy-boot-v1:${secret}`));
  return u8ToB64Url(new Uint8Array(digest));
}

async function computeTokenMac(bootSeed, kind, body) {
  const prefix = ENC.encode(`${kind}\n`);
  const msg = concatU8(prefix, body);
  const sig = await hmacSign(getHmacKey(ENC.encode(bootSeed), `tok:${bootSeed}`), msg);
  return sig.slice(0, 16);
}

async function mintToken(bootSeed, kind, expAtMs) {
  const body = new Uint8Array(16);
  new DataView(body.buffer).setUint32(0, Math.ceil(expAtMs / 1000), false);
  body.set(randBytes(12), 4);
  const full = new Uint8Array(32);
  full.set(body, 0);
  full.set(await computeTokenMac(bootSeed, kind, body), 16);
  return u8ToB64Url(full);
}

async function verifyToken(tokenStr, bootSeed, kind) {
  if (typeof tokenStr !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(tokenStr)) return false;
  let raw;
  try {
    raw = b64UrlToU8(tokenStr);
  } catch {
    return false;
  }
  if (raw.byteLength !== 32 || u8ToB64Url(raw) !== tokenStr) return false;
  const body = raw.slice(0, 16);
  const sig = raw.slice(16);
  const exp = new DataView(body.buffer, body.byteOffset, body.byteLength).getUint32(0, false);
  if (exp <= Math.floor(Date.now() / 1000)) return false;
  const want = await computeTokenMac(bootSeed, kind, body);
  let diff = sig.byteLength ^ want.byteLength;
  for (let i = 0; i < want.length; i++) diff |= sig[i] ^ want[i];
  return diff === 0;
}

export async function computeBridgeCapability(secret, host) {
  const key = getHmacKey(parseSecretKey(secret, true), `bridge:${secret}`);
  const sig = await hmacSign(key, ENC.encode(`tdesktop-web-proxy-bridge-v1\n${validateHostName(host)}`));
  return u8ToB64Url(sig);
}

async function verifyBridgeQuery(url, secret) {
  if (url.pathname !== '/' || !url.search.startsWith('?bridge=')) return null;
  const cand = url.search.slice(8);
  if (url.search !== `?bridge=${cand}` || !/^[A-Za-z0-9_-]{43}$/.test(cand)) return null;
  const host = validateHostName(url.hostname);
  const want = await computeBridgeCapability(secret, host);
  if (cand.length !== want.length) return null;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= cand.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0 ? host : null;
}

/* ------------------------------------------- Frame & MTProto Obfuscation */

function isValidFrameType(t) {
  return t === 1 || t === 2 || t === 3 || t === 4 || t === 17;
}

function encodeLaneFrame(type, sid, payload = EMPTY_U8) {
  const body = toU8(payload);
  if (!isValidFrameType(type) || !Number.isInteger(sid) || sid < 0 || sid > 0xffffff || body.byteLength > 1048576) {
    throw new RangeError('Invalid lane frame');
  }
  const frame = new Uint8Array(8 + body.byteLength);
  frame[0] = type;
  frame[1] = sid >>> 16;
  frame[2] = sid >>> 8;
  frame[3] = sid & 0xff;
  new DataView(frame.buffer).setUint32(4, body.byteLength, false);
  frame.set(body, 8);
  return frame;
}

function decodeLaneFrames(buf) {
  const src = toU8(buf);
  const out = [];
  let o = 0;
  while (o < src.byteLength) {
    if (src.byteLength - o < 8) throw new Error('Truncated lane header');
    const type = src[o];
    if (!isValidFrameType(type)) throw new Error('Invalid frame type');
    const sid = (src[o + 1] << 16) | (src[o + 2] << 8) | src[o + 3];
    const len = new DataView(src.buffer, src.byteOffset + o, src.byteLength - o).getUint32(4, false);
    if (len > 1048576 || src.byteLength - o < 8 + len) throw new Error('Invalid frame length');
    if (out.length >= 4096) throw new Error('Too many frames');
    out.push({ type, sid, payload: src.subarray(o + 8, o + 8 + len) });
    o += 8 + len;
  }
  return out;
}

function validateClientFrame(frame) {
  const { type, payload } = frame;
  if (type === 1 || type === 3) {
    if (payload.byteLength !== 0) throw new Error('Unexpected payload');
    return 0;
  }
  if (type === 2) {
    if (!payload.byteLength) throw new Error('Empty data frame');
    return 0;
  }
  if (type === 4) {
    if (payload.byteLength !== 4) throw new Error('Invalid window frame');
    const delta = new DataView(payload.buffer, payload.byteOffset, 4).getUint32(0, false);
    if (!delta) throw new Error('Zero window grant');
    return delta;
  }
  throw new Error('Unsupported frame');
}

function parseObfuscatedHeader(plain64) {
  const tag = new DataView(plain64.buffer, plain64.byteOffset, plain64.byteLength).getUint32(56, true);
  const mode = tag === 0xefefefef ? 0xef : tag === 0xeeeeeeee ? 0xee : tag === 0xdddddddd ? 0xdd : 0;
  if (!mode) throw new Error('Unsupported MTProto transport tag');
  const rawDc = ((plain64[60] | (plain64[61] << 8)) << 16) >> 16;
  const target = Math.abs(rawDc) - 1;
  if (target < 0 || target >= 5) throw new Error('Invalid Telegram DC');
  return { mode, target, media: rawDc < 0 };
}

function mintKwsHeader(mode, target, media) {
  const head = new Uint8Array(64);
  do {
    crypto.getRandomValues(head);
  } while (
    head[0] === 0xef ||
    BAD_HEADS.has(DEC.decode(head.subarray(0, 4))) ||
    head.subarray(4, 8).every((v) => v === 0)
  );
  head.fill(mode, 56, 60);
  new DataView(head.buffer).setInt16(60, (target + 1) * (media ? -1 : 1), true);
  return head;
}

function stepCounter(ctr, blocks) {
  let carry = blocks;
  for (let i = ctr.length - 1; i >= 0 && carry; i--) {
    const next = ctr[i] + (carry & 0xff);
    ctr[i] = next & 0xff;
    carry = Math.floor(carry / 256) + Math.floor(next / 256);
  }
}

function createAesCtrStream(keyBytes, initialCtr) {
  const iv = initialCtr.slice();
  let tail = EMPTY_U8;
  const keyPromise = crypto.subtle.importKey('raw', toU8(keyBytes), { name: 'AES-CTR' }, false, ['encrypt']);

  const crypt = async (input) => {
    const b = toU8(input);
    if (!b.byteLength) return new Uint8Array(0);
    const key = await keyPromise;
    if (!tail.byteLength && b.byteLength % 16 === 0) {
      const out = new Uint8Array(
        await crypto.subtle.encrypt({ name: 'AES-CTR', counter: iv.slice(), length: 128 }, key, b)
      );
      stepCounter(iv, b.byteLength / 16);
      return out;
    }
    if (b.byteLength <= tail.byteLength) {
      const out = new Uint8Array(b.byteLength);
      for (let i = 0; i < b.byteLength; i++) out[i] = b[i] ^ tail[i];
      tail = tail.slice(b.byteLength);
      return out;
    }
    const skip = tail.byteLength ? 16 - tail.byteLength : 0;
    const ctr = iv.slice();
    if (skip) {
      for (let i = ctr.length - 1; i >= 0; i--) {
        if (ctr[i]--) break;
      }
    }
    const end = skip + b.byteLength;
    const padded = new Uint8Array(Math.ceil(end / 16) * 16);
    padded.set(b, skip);
    const raw = new Uint8Array(
      await crypto.subtle.encrypt({ name: 'AES-CTR', counter: ctr, length: 128 }, key, padded)
    );
    tail = raw.slice(end);
    stepCounter(iv, padded.byteLength / 16 - (skip ? 1 : 0));
    return raw.subarray(skip, end);
  };

  return { crypt };
}

async function createMtprotoCipher(head64, secretBytes, reverse) {
  const mat = toU8(head64).subarray(8, 56).slice();
  if (reverse) mat.reverse();
  const key = secretBytes.byteLength
    ? new Uint8Array(await crypto.subtle.digest('SHA-256', concatU8(mat.subarray(0, 32), secretBytes)))
    : mat.subarray(0, 32);
  return createAesCtrStream(key, mat.subarray(32, 48));
}

async function buildKwsWireHeader(head64) {
  const ctr = await createMtprotoCipher(head64, EMPTY_U8, false);
  const enc = await ctr.crypt(head64);
  enc.set(toU8(head64).subarray(0, 56));
  return { wire: enc, cipher: ctr };
}

function kwsCandidates(dc, media) {
  return [
    `kws${dc}${media ? '-1' : ''}.web.telegram.org`,
    `kws${dc}${media ? '' : '-1'}.web.telegram.org`,
  ];
}

function createKwsWire(ws, timeoutMs, signal) {
  const q = [];
  const waiters = [];
  let h = 0;
  let qBytes = 0;
  let closed = false;
  let dead = false;
  let up = false;
  let fault = null;
  let pWake = null;
  let pNeed = 0;
  let openTimer = null;
  let readyOk;
  let readyBad;

  const ready = new Promise((ok, bad) => {
    readyOk = ok;
    readyBad = bad;
  });
  ready.catch(() => {});

  const trim = () => {
    if (h === q.length) {
      q.length = 0;
      h = 0;
    } else if (h >= 256 && h * 2 >= q.length) {
      q.splice(0, h);
      h = 0;
    }
  };

  const end = (err = null) => {
    if (closed) return;
    closed = true;
    fault = err;
    if (openTimer) clearTimeout(openTimer);
    signal?.removeEventListener('abort', onAbort);
    if (!up) readyBad(err || new Error('KWS closed'));
    if (pWake) pWake();
    for (const w of waiters.splice(0)) {
      if (err) w.reject(err);
      else w.resolve(null);
    }
  };

  const close = () => {
    if (dead) return;
    dead = true;
    if (!closed) end();
    q.length = 0;
    h = 0;
    qBytes = 0;
    if (ws && ws.readyState === 1) {
      try { ws.close(1000); } catch {}
    }
  };

  const onAbort = () => {
    end(new Error('Aborted'));
    close();
  };

  ws.binaryType = 'arraybuffer';
  ws.addEventListener('open', () => {
    if (closed) return;
    if (ws.protocol !== 'binary') {
      end(new Error('KWS subprotocol mismatch'));
      close();
      return;
    }
    up = true;
    if (openTimer) clearTimeout(openTimer);
    readyOk(api);
  });
  ws.addEventListener('message', (ev) => {
    if (closed) return;
    const b = ev.data instanceof Uint8Array ? ev.data : new Uint8Array(ev.data);
    if (!b.byteLength) return;
    const w = waiters.shift();
    if (w) w.resolve(b);
    else {
      q.push(b);
      qBytes += b.byteLength;
      if (pWake && (qBytes >= pNeed || b.byteLength >= DN_PACK)) pWake();
    }
  });
  ws.addEventListener('error', () => end(new Error('KWS error')));
  ws.addEventListener('close', (ev) => end(ev?.code && ev.code !== 1000 ? new Error('KWS closed') : null));

  openTimer = setTimeout(() => {
    end(new Error('KWS connect timeout'));
    close();
  }, timeoutMs);

  signal?.addEventListener('abort', onAbort, { once: true });
  if (signal?.aborted) onAbort();

  const take = async () => {
    await ready;
    if (h < q.length) {
      const msg = q[h];
      q[h++] = null;
      qBytes -= msg.byteLength;
      trim();
      return msg;
    }
    if (fault) throw fault;
    if (closed) return null;
    return new Promise((ok, bad) => waiters.push({ resolve: ok, reject: bad }));
  };

  const api = {
    ready,
    async write(v) {
      await ready;
      if (closed) throw fault || new Error('Closed');
      ws.send(toU8(v));
    },
    async readPack() {
      const first = await take();
      if (dead) return null;
      if (!first || first.byteLength >= DN_PACK) return first;
      if (!closed && qBytes < DN_PACK - first.byteLength) {
        await new Promise((ok) => {
          pNeed = DN_PACK - first.byteLength;
          const timer = setTimeout(() => { if (pWake) pWake(); }, DN_MS);
          pWake = () => {
            clearTimeout(timer);
            pWake = null;
            ok();
          };
        });
      }
      if (dead) return null;
      let n = first.byteLength;
      let endIdx = h;
      while (endIdx < q.length && n + q[endIdx].byteLength <= DN_PACK) {
        n += q[endIdx++].byteLength;
      }
      if (endIdx === h) return first;
      const merged = new Uint8Array(n);
      merged.set(first, 0);
      let off = first.byteLength;
      while (h < endIdx) {
        const part = q[h];
        q[h++] = null;
        merged.set(part, off);
        off += part.byteLength;
        qBytes -= part.byteLength;
      }
      trim();
      return merged;
    },
    close,
    get closed() {
      return closed;
    },
  };

  return api;
}

async function dialTelegramKws({ target, media, signal }) {
  for (const host of kwsCandidates(target + 1, media)) {
    if (signal?.aborted) throw new Error('Aborted');
    let kws = null;
    try {
      kws = createKwsWire(new WebSocket(`wss://${host}/apiws`, 'binary'), 7500, signal);
      await kws.ready;
      if (signal?.aborted) throw new Error('Aborted');
      return kws;
    } catch (err) {
      if (kws) kws.close();
      if (signal?.aborted) throw err;
    }
  }
  throw new Error('Unable to reach Telegram KWS');
}

function createPacketSplitter(mode) {
  let tail = EMPTY_U8;
  return {
    push(v) {
      const b = toU8(v);
      if (b.byteLength) tail = tail.byteLength ? concatU8(tail, b) : b;
      const pkts = [];
      let o = 0;
      for (;;) {
        const avail = tail.byteLength - o;
        let hLen;
        let pLen;
        if (mode === 0xef) {
          if (avail < 1) break;
          let words = tail[o] & 0x7f;
          if (words === 0x7f) {
            if (avail < 4) break;
            words = tail[o + 1] | (tail[o + 2] << 8) | (tail[o + 3] << 16);
            hLen = 4;
          } else {
            hLen = 1;
          }
          pLen = words * 4;
        } else {
          if (avail < 4) break;
          pLen = new DataView(tail.buffer, tail.byteOffset + o, 4).getUint32(0, true) & 0x7fffffff;
          hLen = 4;
        }
        if (!pLen || pLen > 8 * 1024 * 1024) throw new Error('Invalid MTProto packet size');
        const total = hLen + pLen;
        if (avail < total) break;
        pkts.push(tail.subarray(o, o + total));
        o += total;
      }
      if (o) tail = o === tail.byteLength ? EMPTY_U8 : tail.slice(o);
      if (tail.byteLength > 8 * 1024 * 1024 + 4) throw new Error('Packet buffer overflow');
      return pkts;
    },
    clear() {
      tail = EMPTY_U8;
    },
  };
}

function createMtprotoStream({ secretBytes, emit, onClose }) {
  let hdr = EMPTY_U8;
  let cIn = null;
  let cOut = null;
  let kIn = null;
  let kOut = null;
  let peer = null;
  let pkt = null;
  let initialized = false;
  let closed = false;
  let chain = Promise.resolve();
  const ac = new AbortController();
  const uq = new Set();

  const close = () => {
    if (closed) return;
    closed = true;
    try { ac.abort(); } catch {}
    try { peer?.close(); } catch {}
    for (const job of uq) job.bytes = null;
    uq.clear();
    hdr = EMPTY_U8;
    pkt?.clear();
    cIn = cOut = kIn = kOut = peer = null;
    try { onClose(); } catch {}
  };

  const pumpDownstream = async () => {
    while (!closed) {
      const b = await peer.readPack();
      if (closed) return;
      if (!b) {
        close();
        return;
      }
      const plain = await kIn.crypt(b);
      if (closed) return;
      const raw = await cOut.crypt(plain);
      if (closed) return;
      await emit(raw);
    }
  };

  const writeUpstream = async (raw) => {
    if (closed || peer.closed) return false;
    const plain = await cIn.crypt(raw);
    if (closed || peer.closed) return false;
    const pkts = pkt.push(plain);
    for (let s = 0; s < pkts.length; ) {
      let e = s + 1;
      let n = pkts[s].byteLength;
      while (e < pkts.length && n + pkts[e].byteLength <= UP_PACK) {
        n += pkts[e++].byteLength;
      }
      const first = pkts[s];
      const batch = e === s + 1 ? first : new Uint8Array(first.buffer, first.byteOffset, n);
      if (closed || peer.closed) return false;
      const wire = await kOut.crypt(batch);
      for (let off = 0; s < e; s++) {
        if (closed || peer.closed) return false;
        const pLen = pkts[s].byteLength;
        await peer.write(wire.subarray(off, off + pLen));
        off += pLen;
      }
    }
    return true;
  };

  const startHandshake = async () => {
    const raw = hdr;
    const cr = await createMtprotoCipher(raw, secretBytes, false);
    const plain = await cr.crypt(raw);
    if (closed) return;
    const info = parseObfuscatedHeader(plain);
    const newHead = mintKwsHeader(info.mode, info.target, info.media);
    const [cw, outHead, kr] = await Promise.all([
      createMtprotoCipher(raw, secretBytes, true),
      buildKwsWireHeader(newHead),
      createMtprotoCipher(newHead, EMPTY_U8, true),
    ]);
    if (closed) return;
    const kws = await dialTelegramKws({ target: info.target, media: info.media, signal: ac.signal });
    if (closed) {
      kws.close();
      return;
    }
    peer = kws;
    cIn = cr;
    cOut = cw;
    kOut = outHead.cipher;
    kIn = kr;
    pkt = createPacketSplitter(info.mode);
    await kws.write(outHead.wire);
    if (closed) return;
    hdr = EMPTY_U8;
    initialized = true;
    pumpDownstream().catch(() => close());
  };

  const feed = async (b) => {
    if (closed) throw new Error('Stream closed');
    if (initialized && peer.closed) return false;
    if (!b.byteLength) return true;
    if (!initialized) {
      const need = 64 - hdr.byteLength;
      if (b.byteLength < need) {
        hdr = concatU8(hdr, b);
        return true;
      }
      hdr = concatU8(hdr, b.subarray(0, need));
      await startHandshake();
      const rest = b.subarray(need);
      if (rest.byteLength) return writeUpstream(rest);
      return true;
    }
    return writeUpstream(b);
  };

  return {
    push(v) {
      if (closed) return Promise.reject(new Error('Closed'));
      const job = { bytes: toU8(v), result: true };
      uq.add(job);
      chain = chain.then(async () => {
        if (closed) throw new Error('Closed');
        if (!job.bytes) return job.result;
        const jobs = [job];
        let n = job.bytes.byteLength;
        for (const next of uq) {
          if (next === job) continue;
          if (n >= UP_PACK || n + next.bytes.byteLength > UP_PACK) break;
          n += next.bytes.byteLength;
          jobs.push(next);
        }
        const merged = jobs.length === 1 ? job.bytes : concatU8(...jobs.map((x) => x.bytes));
        for (const item of jobs) {
          uq.delete(item);
          item.bytes = null;
        }
        const res = await feed(merged);
        for (const item of jobs) item.result = res;
        return res;
      });
      return chain;
    },
    close,
  };
}

function createLaneSession({ secretBytes, sid, lease, onClose }) {
  let sock = null;
  let closed = false;
  let tx = Promise.resolve();
  let cur = null;

  const wake = (st) => {
    const fn = st.wake;
    st.wake = null;
    if (fn) fn();
  };

  const stop = (st) => {
    st.closed = true;
    st.pItems = 0;
    wake(st);
    st.proxy.close();
  };

  const close = (code = 1000, reason = '', fromPeerEvent = false) => {
    if (closed) return;
    closed = true;
    if (cur) stop(cur);
    cur = null;
    if (!fromPeerEvent && sock && sock.readyState === 1) {
      try { sock.close(code, reason); } catch {}
    }
    if (onClose) {
      const fn = onClose;
      onClose = null;
      try { fn(); } catch {}
    }
  };

  const send = (type, data = EMPTY_U8) => {
    const out = encodeLaneFrame(type, sid, data);
    const st = type === 2 || type === 4 ? cur : null;
    tx = tx.then(() => {
      if (closed) throw new Error('Closed');
      if ((type === 2 || type === 4) && (!st || st.closed)) return;
      sock.send(out);
    });
    return tx;
  };

  const end = (notify) => {
    if (closed || cur?.closed) return;
    const st = cur;
    if (!st) throw new Error('No stream');
    stop(st);
    if (notify) send(3).then(() => close(), () => close());
    else close();
  };

  const emitDn = async (st, b) => {
    for (let o = 0; o < b.byteLength; ) {
      while (!closed && !st.closed && st.out === 0) {
        await new Promise((ok) => { st.wake = ok; });
      }
      if (closed || st.closed) throw new Error('Closed');
      const n = Math.min(65536, b.byteLength - o, st.out);
      st.out -= n;
      await send(2, b.subarray(o, o + n));
      o += n;
    }
  };

  const open = () => {
    if (closed || cur) throw new Error('Already open');
    const st = {
      in: 4 * 1024 * 1024,
      out: 4 * 1024 * 1024,
      wake: null,
      closed: false,
      proxy: null,
      pItems: 0,
    };
    st.proxy = createMtprotoStream({
      secretBytes,
      emit: (b) => emitDn(st, b),
      onClose: () => { if (!closed && !st.closed) end(true); },
    });
    cur = st;
  };

  const write = (payload) => {
    if (closed || cur?.closed) return;
    const st = cur;
    if (!st) throw new Error('Not open');
    const n = payload.byteLength;
    if (n > st.in || st.pItems >= 16384) throw new Error('Window exceeded');
    st.in -= n;
    st.pItems++;
    st.proxy
      .push(payload)
      .then(
        (accepted) => {
          if (!st.closed) st.pItems--;
          if (closed || st.closed || accepted === false) return;
          st.in += n;
          const delta = new Uint8Array(4);
          new DataView(delta.buffer).setUint32(0, n, false);
          send(4, delta).catch(() => close());
        },
        () => {
          if (!st.closed) st.pItems--;
          if (!closed && !st.closed) st.proxy.close();
        }
      )
      .catch(() => close());
  };

  const grant = (delta) => {
    if (closed || cur?.closed) return;
    if (!cur) throw new Error('Not open');
    cur.out = Math.min(0xffffffff, cur.out + delta);
    wake(cur);
  };

  return {
    attach(ws) {
      if (sock || closed) throw new Error('Invalid attach');
      sock = ws;
      ws.accept();
      ws.binaryType = 'arraybuffer';
      ws.addEventListener('message', (ev) => {
        if (closed) return;
        try {
          if (lease?.deleted || typeof ev.data === 'string') throw new Error('Invalid frame');
          const b = ev.data instanceof Uint8Array ? ev.data : new Uint8Array(ev.data);
          if (!b.byteLength || b.byteLength > 2 * 1024 * 1024) throw new Error('Frame size');
          for (const frame of decodeLaneFrames(b)) {
            if (closed) return;
            const delta = validateClientFrame(frame);
            if (frame.sid !== sid || (!cur && frame.type !== 1)) throw new Error('Invalid sid');
            if (frame.type !== 1 && cur?.closed) continue;
            if (frame.type === 1) open();
            else if (frame.type === 2) write(frame.payload);
            else if (frame.type === 4) grant(delta);
            else if (frame.type === 3) end(false);
          }
        } catch {
          close(1008);
        }
      });
      ws.addEventListener('close', () => close(1000, '', true));
      ws.addEventListener('error', () => close(1000, '', true));
    },
    close,
  };
}

function buildBridgeHtml(host, bootToken) {
  const nonce = u8ToB64Url(randBytes(18));
  const body = `<script nonce="${nonce}">
(()=>{'use strict';const origin=${JSON.stringify(`https://${host}`)},boot=${JSON.stringify(String(bootToken))};
const aNonce=/^#android=([A-Za-z0-9_-]{43})$/.exec(location.hash)?.[1]??'';history.replaceState(null,'',location.pathname);
let init=false,closed=false,port=null,token='',started=false,delSent=false,qBytes=0,qItems=0;
const lanes=new Map(),used=new Set(),qMax=33554432,iMax=16384,msgMax=2097152,laneMax=8388608,laneItems=1024;
const state=st=>port&&!closed&&port.postMessage({t:'status',state:st});
const opts=(method,tok,b,ka)=>({method,body:b,keepalive:!!ka,mode:'same-origin',credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',headers:{...(tok&&{Authorization:'Bearer '+tok}),...(b&&{'Content-Type':'application/octet-stream'})}});
function check(v){if(!(v instanceof ArrayBuffer)||!v.byteLength||v.byteLength>msgMax)throw Error();const view=new DataView(v);let o=0,f=0;while(o<v.byteLength){if(v.byteLength-o<8||f++>=4096)throw Error();const t=view.getUint8(o),n=view.getUint32(o+4),end=o+8+n;if(n>1048576||end>v.byteLength||(t===2&&!n))throw Error();o=end;}return v;}
function split(v){check(v);const view=new DataView(v),out=[];let o=0;while(o<v.byteLength){const end=o+8+view.getUint32(o+4);out.push(o===0&&end===v.byteLength?v:v.slice(o,end));o=end;}return out;}
function sid(v){const b=new Uint8Array(v);return(b[1]<<16)|(b[2]<<8)|b[3];}
function hold(lane,v){let buf=0;for(const{ws}of lanes.values())buf+=ws?.bufferedAmount??0;const own=lane.ws?.bufferedAmount??0;if(qBytes+buf>qMax-v.byteLength||qItems>=iMax||lane.bytes+own>laneMax-v.byteLength||lane.items>=laneItems)return false;qBytes+=v.byteLength;qItems++;lane.bytes+=v.byteLength;lane.items++;return true;}
function free(lane,v){qBytes-=v.byteLength;qItems--;lane.bytes-=v.byteLength;lane.items--;}
function fail(){if(closed)return;state('failed');port?.postMessage({t:'close'});close(true);}
function drop(){if(delSent||!token)return;delSent=true;fetch(origin+'/api/v1/session',opts('DELETE',token,null,true)).catch(()=>{});}
function pump(lane){const ws=lane.ws;if(closed||lane.done||!ws||ws.readyState!==WebSocket.OPEN||!lane.pending.length)return;if(ws.bufferedAmount>=msgMax){lane.timer||=setTimeout(()=>{lane.timer=0;pump(lane);},10);return;}try{const v=lane.pending.shift();ws.send(v);free(lane,v);port.postMessage({t:'traffic',up:v.byteLength,down:0});if(lane.pending.length)queueMicrotask(()=>pump(lane));}catch{fail();}}
function push(v){try{for(const frame of split(v))pushLane(frame);}catch{fail();}}
function end(lane,notify){if(lane.done)return;lane.done=true;if(lane.timer)clearTimeout(lane.timer);qBytes-=lane.bytes;qItems-=lane.items;lane.bytes=lane.items=0;lane.pending.length=0;lanes.delete(lane.id);try{lane.ws?.close();}catch{}if(notify&&port&&!closed){const frame=new ArrayBuffer(8),b=new Uint8Array(frame);b[0]=3;b[1]=lane.id>>>16;b[2]=lane.id>>>8;b[3]=lane.id;port.postMessage(frame,[frame]);}}
function open(lane){const target=origin.replace(/^https:/,'wss:')+'/api/v1/ws';const ws=lane.ws=new WebSocket(target,'tproxy-lane-v1.'+token+'.'+lane.id);ws.binaryType='arraybuffer';ws.onopen=()=>{if(closed||lane.done){ws.close();return;}lane.opened=true;state('connected');pump(lane);};ws.onmessage=e=>{if(closed||lane.done)return;try{const frs=split(e.data);if(frs.some(f=>sid(f)!==lane.id))throw Error();const ended=frs.some(f=>new Uint8Array(f)[0]===3);if(ended)lane.rClosed=true;port.postMessage({t:'traffic',up:0,down:e.data.byteLength});port.postMessage(e.data,[e.data]);state('connected');if(ended)end(lane,false);}catch{fail();}};ws.onerror=()=>{};ws.onclose=()=>{if(closed||lane.done)return;if(!lane.opened){fail();return;}end(lane,!lane.lClosed&&!lane.rClosed);};}
function pushLane(v){const id=sid(v),type=new Uint8Array(v)[0];let lane=lanes.get(id);if(!id)throw Error();if(!lane&&(type===2||type===3||type===4))return;if(!lane){if(type!==1||used.has(id)||lanes.size>=128||used.size>=4096)throw Error();used.add(id);lane={id,ws:null,pending:[],bytes:0,items:0,timer:0,opened:false,done:false,lClosed:false,rClosed:false};lanes.set(id,lane);}if(lane.lClosed)return;if(type===3&&!lane.opened){end(lane,false);return;}if(!hold(lane,v))throw Error();lane.pending.push(v);if(type===3)lane.lClosed=true;if(token){if(!lane.ws)open(lane);else pump(lane);}}
async function create(first){try{state('connecting');const res=await fetch(origin+'/api/v1/session',opts('POST',boot,first));if(res.status!==200||res.headers.get('X-Carrier-Mode')!=='websocket-lanes')throw Error();token=res.headers.get('X-Session-Token')??'';if(!token)throw Error();const welcome=await res.arrayBuffer();check(welcome);if(closed){drop();return;}port.postMessage(welcome,[welcome]);state('connected');for(const lane of lanes.values())if(!lane.ws)open(lane);}catch{fail();}}
function close(notify){if(closed)return;closed=true;for(const lane of[...lanes.values()])end(lane,false);used.clear();if(notify)drop();port?.close();}
function start(next){init=true;port=next;port.onmessage=msg=>{const data=msg.data;if(data instanceof ArrayBuffer){if(!started){try{check(data);}catch{fail();return;}started=true;create(data);}else push(data);}else if(data&&data.t==='close')close(true);};port.start();state('connecting');}
addEventListener('message',e=>{if(init||e.source!==parent||e.data===null||typeof e.data!=='object')return;const keys=Object.keys(e.data).sort();if(keys.length!==2||keys[0]!=='t'||keys[1]!=='v'||e.data.t!=='tproxy-init'||e.data.v!==1||e.ports.length!==1)return;let src;try{src=new URL(e.origin);}catch{return;}if(src.protocol!=='http:'||src.hostname!=='127.0.0.1'||!src.port||src.origin!==e.origin)return;start(e.ports[0]);});
const android=globalThis.TelegramWebProxy;
if(!init&&aNonce&&android&&typeof android.postMessage==='function'){const aPort={onmessage:null,start(){},close(){android.onmessage=null;},postMessage(v){if(v instanceof ArrayBuffer){for(const frame of split(v))android.postMessage(frame);}else android.postMessage(JSON.stringify(v));}};android.onmessage=e=>{let data=e.data;if(typeof data==='string'){try{data=JSON.parse(data);}catch{return;}}aPort.onmessage?.({data});};start(aPort);android.postMessage(JSON.stringify({t:'tproxy-android-init',v:1,nonce:aNonce}));}
addEventListener('pagehide',()=>close(true),{once:true});})();
</script>`;
  const csp = [
    "default-src 'none'",
    "base-uri 'none'",
    "child-src 'none'",
    `connect-src 'self' wss://${host}`,
    "font-src 'none'",
    "form-action 'none'",
    'frame-ancestors http://127.0.0.1:*',
    "frame-src 'none'",
    "img-src 'none'",
    "manifest-src 'none'",
    "media-src 'none'",
    "object-src 'none'",
    `script-src 'nonce-${nonce}'`,
    "style-src 'none'",
    "worker-src 'none'",
    'sandbox allow-same-origin allow-scripts',
  ].join('; ');
  return { body, csp };
}

/* ------------------------------------------- Public Worker Route Handlers */

export async function maybeHandleTgBridgePage(request, url, settings) {
  if (!settings || settings.enableTgProxy === false) return null;
  const secret = normalizeTgSecret(settings.tgProxySecret);
  if (!secret) return null;
  if (url.pathname !== '/' || !url.search.startsWith('?bridge=')) return null;
  if (request.method !== 'GET' && request.method !== 'HEAD') return null;

  let host = null;
  if (request.method === 'GET') {
    try {
      host = await verifyBridgeQuery(url, secret);
    } catch {
      host = null;
    }
  }
  if (!host) return new Response(null, { status: 404 });

  const bootSeed = await deriveBootSeed(secret);
  const bootToken = await mintToken(bootSeed, 'bootstrap', Date.now() + 2 * 60 * 1000);
  const page = buildBridgeHtml(host, bootToken);
  return new Response(page.body, {
    status: 200,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'content-security-policy': page.csp,
      'cache-control': 'no-store',
      'referrer-policy': 'no-referrer',
      'x-content-type-options': 'nosniff',
    },
  });
}

export async function handleTgSessionApi(request, settings) {
  if (!settings || settings.enableTgProxy === false) return new Response(null, { status: 404 });
  const secret = normalizeTgSecret(settings.tgProxySecret);
  if (!secret) return new Response(null, { status: 404 });

  const bootSeed = await deriveBootSeed(secret);
  const authHeader = request.headers.get('authorization') || '';
  const bearer = /^Bearer ([^\s]+)$/.exec(authHeader)?.[1] ?? null;

  if (request.method === 'DELETE') {
    if (!bearer || !(await verifyToken(bearer, bootSeed, 'session'))) {
      return new Response(null, { status: 404 });
    }
    let lease = sessionLeases.get(bearer);
    if (!lease) {
      lease = {
        expires: new DataView(b64UrlToU8(bearer).buffer).getUint32(0, false) * 1000,
        active: new Set(),
        used: new Set(),
        deleted: false,
      };
      sessionLeases.set(bearer, lease);
    }
    lease.deleted = true;
    return new Response(null, { status: 204 });
  }

  const ct = (request.headers.get('content-type') || '').trim().toLowerCase();
  if (request.method !== 'POST' || !bearer || ct !== 'application/octet-stream') {
    return new Response(null, { status: 404 });
  }
  if (bearer !== bootSeed && !(await verifyToken(bearer, bootSeed, 'bootstrap'))) {
    return new Response(null, { status: 404 });
  }

  try {
    const body = new Uint8Array(await request.arrayBuffer());
    if (
      body.byteLength !== 9 ||
      body[0] !== 0x10 ||
      body[1] !== 0 ||
      body[2] !== 0 ||
      body[3] !== 0 ||
      new DataView(body.buffer, body.byteOffset, body.byteLength).getUint32(4, false) !== 1 ||
      body[8] !== 1
    ) {
      return new Response(null, { status: 404 });
    }
    const sessToken = await mintToken(bootSeed, 'session', Date.now() + 5 * 60 * 1000);
    return new Response(encodeLaneFrame(0x11, 0), {
      status: 200,
      headers: {
        'content-type': 'application/octet-stream',
        'cache-control': 'no-store',
        'x-session-token': sessToken,
        'x-carrier-mode': 'websocket-lanes',
      },
    });
  } catch {
    return new Response(null, { status: 404 });
  }
}

export async function handleTgWebSocket(request, url, settings) {
  if (!settings || settings.enableTgProxy === false || settings.killSwitch) {
    return new Response(null, { status: 404 });
  }
  const secret = normalizeTgSecret(settings.tgProxySecret);
  if (!secret) return new Response(null, { status: 404 });

  if (request.method !== 'GET') return new Response(null, { status: 404 });
  if (request.headers.has('origin') && request.headers.get('origin') !== url.origin) {
    return new Response(null, { status: 404 });
  }

  const offers = (request.headers.get('sec-websocket-protocol') || '')
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);
  const proto = offers.length === 1 ? offers[0] : null;
  const parts = proto ? proto.split('.') : [];
  const [kind, token = '', idStr = ''] = parts;
  const sid = Number(idStr);

  const bootSeed = await deriveBootSeed(secret);
  if (
    parts.length !== 3 ||
    kind !== 'tproxy-lane-v1' ||
    !/^[1-9][0-9]{0,7}$/.test(idStr) ||
    sid > 0xffffff ||
    !(await verifyToken(token, bootSeed, 'session'))
  ) {
    return new Response(null, { status: 404 });
  }

  const now = Date.now();
  for (const [k, v] of sessionLeases) {
    if (v.expires <= now && !v.active.size) sessionLeases.delete(k);
  }
  let lease = sessionLeases.get(token);
  if (!lease) {
    if (sessionLeases.size >= 4096) return new Response(null, { status: 503 });
    lease = {
      expires: new DataView(b64UrlToU8(token).buffer).getUint32(0, false) * 1000,
      active: new Set(),
      used: new Set(),
      deleted: false,
    };
    sessionLeases.set(token, lease);
  }
  if (lease.deleted || lease.used.has(sid) || lease.active.size >= 128 || lease.used.size >= 4096) {
    return new Response(null, { status: 409 });
  }
  lease.used.add(sid);
  lease.active.add(sid);

  const sess = createLaneSession({
    secretBytes: parseSecretKey(secret, false),
    sid,
    lease,
    onClose: () => {
      lease.active.delete(sid);
    },
  });

  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  try {
    sess.attach(server);
  } catch {
    sess.close();
    return new Response(null, { status: 409 });
  }

  return new Response(null, {
    status: 101,
    webSocket: client,
    headers: {
      'sec-websocket-protocol': proto,
      'sec-websocket-extensions': '',
    },
  });
}
