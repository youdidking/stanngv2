/**
 * NIGHTLINE — Cryptographic & safe-decoding primitives (Cloudflare Free-Tier CPU optimized).
 */

export const ENC = new TextEncoder();
export const DEC = new TextDecoder();
export const PBKDF2_DEFAULT_ITER = 10000;

const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr(x, n) {
  return (x >>> n) | (x << (32 - n));
}

export function safeDecodeURIComponent(str, fallback = '') {
  if (typeof str !== 'string') return fallback;
  try {
    return decodeURIComponent(str);
  } catch {
    return str || fallback;
  }
}

export function safeJsonParse(raw, fallback = null) {
  if (typeof raw !== 'string' || !raw.trim()) return fallback;
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

/**
 * Compute 56-character lowercase hex SHA-224 digest of a UTF-8 string (Trojan auth format).
 */
export function sha224Hex(input) {
  const msg = ENC.encode(String(input ?? ''));
  const bitLenHi = Math.floor((msg.length * 8) / 0x100000000);
  const bitLenLo = (msg.length * 8) >>> 0;

  const padLen = (msg.length + 9 + 63) & ~63;
  const buf = new Uint8Array(padLen);
  buf.set(msg);
  buf[msg.length] = 0x80;
  const view = new DataView(buf.buffer);
  view.setUint32(padLen - 8, bitLenHi, false);
  view.setUint32(padLen - 4, bitLenLo, false);

  let h0 = 0xc1059ed8, h1 = 0x367cd507, h2 = 0x3070dd17, h3 = 0xf70e5939;
  let h4 = 0xffc00b31, h5 = 0x68581511, h6 = 0x64f98fa7, h7 = 0xbefa4fa4;
  const w = new Uint32Array(64);

  for (let off = 0; off < padLen; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + SHA256_K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;

      h = g; g = f; f = e;
      e = (d + temp1) >>> 0;
      d = c; c = b; b = a;
      a = (temp1 + temp2) >>> 0;
    }

    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6]
    .map((word) => (word >>> 0).toString(16).padStart(8, '0'))
    .join('');
}

const HEX_TABLE = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, '0'));

export function bytesToHex(bytes) {
  if (!bytes || !bytes.length) return '';
  let out = '';
  for (let i = 0; i < bytes.length; i++) out += HEX_TABLE[bytes[i]];
  return out;
}

export function hexToBytes(hex) {
  const clean = String(hex || '').replace(/[^0-9a-fA-F]/g, '');
  const out = new Uint8Array(clean.length >> 1);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

export function timingSafeEqualStr(a, b) {
  const s1 = String(a || '');
  const s2 = String(b || '');
  if (s1.length !== s2.length) return false;
  let diff = 0;
  for (let i = 0; i < s1.length; i++) {
    diff |= s1.charCodeAt(i) ^ s2.charCodeAt(i);
  }
  return diff === 0;
}

export function randomUUID() {
  try {
    if (crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch {}
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = bytesToHex(b);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function randomToken(len = 16) {
  const safeLen = Math.max(4, Math.min(64, Number(len) || 16));
  const alphabet = 'abcdefghjkmnpqrstuvwxyz23456789';
  const b = new Uint8Array(safeLen);
  crypto.getRandomValues(b);
  let out = '';
  for (let i = 0; i < safeLen; i++) out += alphabet[b[i] % alphabet.length];
  return out;
}

export function randomPassword(len = 14) {
  const safeLen = Math.max(6, Math.min(64, Number(len) || 14));
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const b = new Uint8Array(safeLen);
  crypto.getRandomValues(b);
  let out = '';
  for (let i = 0; i < safeLen; i++) out += alphabet[b[i] % alphabet.length];
  return out;
}

export function isValidUUID(uuid) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(uuid || '').trim());
}

/**
 * PBKDF2-SHA256 password hashing (10,000 iterations = ~1.2ms CPU, safely below
 * Cloudflare Workers Free Plan's 10ms per-request CPU limit & 100k WebCrypto ceiling).
 */
export async function hashPassword(password, saltHex = null, iterations = PBKDF2_DEFAULT_ITER) {
  const safeIter = Math.max(1000, Math.min(100000, Number(iterations) || PBKDF2_DEFAULT_ITER));
  const parsedSalt = saltHex ? hexToBytes(saltHex) : null;
  const salt = parsedSalt && parsedSalt.length >= 8 ? parsedSalt : crypto.getRandomValues(new Uint8Array(16));
  const keyMaterial = await crypto.subtle.importKey(
    'raw',
    ENC.encode(String(password ?? '')),
    'PBKDF2',
    false,
    ['deriveBits']
  );
  const bits = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', salt, iterations: safeIter, hash: 'SHA-256' },
    keyMaterial,
    256
  );
  return {
    salt: bytesToHex(salt),
    hash: bytesToHex(new Uint8Array(bits)),
    iter: safeIter,
  };
}

export async function verifyPassword(password, authRecord) {
  try {
    if (!authRecord || typeof authRecord !== 'object' || !authRecord.salt || !authRecord.hash) {
      return false;
    }
    const derived = await hashPassword(password, authRecord.salt, authRecord.iter || PBKDF2_DEFAULT_ITER);
    return timingSafeEqualStr(derived.hash, authRecord.hash);
  } catch {
    return false;
  }
}

function toBase64Url(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fromBase64Url(str) {
  const pad = String(str || '').replace(/-/g, '+').replace(/_/g, '/');
  const padded = pad + '==='.slice((pad.length + 3) % 4);
  const bin = atob(padded);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function utf8ToBase64(str) {
  try {
    const bytes = ENC.encode(String(str ?? ''));
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  } catch {
    return '';
  }
}

async function hmacSha256(dataStr, secretHex) {
  const keyBytes = hexToBytes(secretHex);
  if (!keyBytes.length) throw new Error('Empty HMAC secret');
  const key = await crypto.subtle.importKey(
    'raw',
    keyBytes,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, ENC.encode(String(dataStr ?? '')));
  return toBase64Url(new Uint8Array(sig));
}

export async function signSession(payload, secretHex) {
  const body = toBase64Url(ENC.encode(JSON.stringify(payload || {})));
  const sig = await hmacSha256(body, secretHex);
  return `${body}.${sig}`;
}

export async function verifySession(token, secretHex, expectedVer = 1) {
  try {
    if (!token || !secretHex || typeof token !== 'string') return null;
    const parts = token.split('.');
    if (parts.length !== 2 || !parts[0] || !parts[1]) return null;
    const [body, sig] = parts;
    const expectedSig = await hmacSha256(body, secretHex);
    if (!timingSafeEqualStr(sig, expectedSig)) return null;
    const payload = safeJsonParse(DEC.decode(fromBase64Url(body)), null);
    if (!payload || typeof payload.exp !== 'number' || Date.now() > payload.exp) return null;
    if (expectedVer && payload.ver !== expectedVer) return null;
    return payload;
  } catch {
    return null;
  }
}
