/**
 * NIGHTLINE — Hardened Multi-Protocol Relay Engine (VLESS + Trojan + DoH + ProxyIP/NAT64/Chain).
 *
 * Every socket (`opened`/`closed`), WebSocket (`send`/`close`), ReadableStream controller,
 * writer lock, timeout timer, and background promise is explicitly guarded against
 * unhandled rejections and race conditions.
 */

import { DEFAULT_NAT64_PREFIXES, DEFAULT_PROXY_IPS, PANEL_VERSION, evaluateUserStatus, recordTraffic } from './storage.js';
import { DEC, ENC, safeDecodeURIComponent, utf8ToBase64 } from './crypto.js';

const WS_OPEN = 1;
const WS_CLOSING = 2;

let cachedConnectFn = null;
async function getSocketConnect() {
  if (cachedConnectFn) return cachedConnectFn;
  try {
    const spec = ['cloudflare', 'sockets'].join(':');
    const mod = await import(spec);
    if (!mod || typeof mod.connect !== 'function') {
      throw new Error('socket connect unavailable');
    }
    cachedConnectFn = mod.connect;
    return cachedConnectFn;
  } catch (err) {
    throw new Error(
      `ماژول شبکه فعال نیست (Compatibility Date را در Settings روی 2024-09-23 یا جدیدتر بگذارید): ${err?.message || err}`
    );
  }
}

function isValidSocketHost(host) {
  if (!host || host.length > 255) return false;
  if (host.startsWith('[') && host.endsWith(']')) {
    return /^\[[0-9a-fA-F:.]+\]$/.test(host);
  }
  return /^[a-zA-Z0-9._-]+$/.test(host);
}

/**
 * Safely open a Cloudflare TCP socket via lazy `cloudflare:sockets` import and
 * disarm asynchronous `.opened`/`.closed` unhandled promise rejections.
 */
async function safeConnect(hostname, port) {
  const cleanHost = String(hostname || '').trim();
  const cleanPort = Number(port);
  if (!isValidSocketHost(cleanHost) || !Number.isInteger(cleanPort) || cleanPort < 1 || cleanPort > 65535) {
    throw new Error(`Invalid target endpoint: ${cleanHost}:${port}`);
  }
  const connectFn = await getSocketConnect();
  const sock = connectFn({ hostname: cleanHost, port: cleanPort });
  if (sock && sock.opened && typeof sock.opened.catch === 'function') {
    sock.opened.catch(() => {});
  }
  if (sock && sock.closed && typeof sock.closed.catch === 'function') {
    sock.closed.catch(() => {});
  }
  return sock;
}

function safeCloseSocket(sock) {
  if (!sock) return;
  try {
    const p = sock.close();
    if (p && typeof p.catch === 'function') {
      p.catch(() => {});
    }
  } catch {
    /* ignore already-closed socket */
  }
}

function safeCloseWS(ws) {
  if (!ws) return;
  try {
    if (ws.readyState === WS_OPEN || ws.readyState === WS_CLOSING) {
      ws.close(1000, 'closed');
    }
  } catch {
    /* ignore */
  }
}

function safeSendWS(ws, payload) {
  if (!ws || ws.readyState !== WS_OPEN) return false;
  try {
    ws.send(payload);
    return true;
  } catch {
    safeCloseWS(ws);
    return false;
  }
}

async function safeWriteSocket(sock, bytes) {
  if (!sock || !sock.writable || !bytes || bytes.byteLength === 0) return;
  let writer = null;
  try {
    if (sock.opened) await sock.opened;
    writer = sock.writable.getWriter();
    if (writer.closed && typeof writer.closed.catch === 'function') writer.closed.catch(() => {});
    if (writer.ready && typeof writer.ready.catch === 'function') writer.ready.catch(() => {});
    await writer.write(bytes);
  } finally {
    if (writer) {
      try {
        writer.releaseLock();
      } catch {
        /* ignore */
      }
    }
  }
}

async function fetchWithTimeout(input, init = {}, timeoutMs = 8000) {
  const ctrl = new AbortController();
  const tid = setTimeout(() => {
    try { ctrl.abort(); } catch {}
  }, timeoutMs);
  try {
    return await fetch(input, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(tid);
  }
}

export function parseEndpoint(raw, fallbackPort = 443) {
  const clean = String(raw || '').split('#')[0].trim();
  if (!clean) return null;

  const v6Match = clean.match(/^\[([^\]]+)\](?::(\d+))?$/);
  if (v6Match) {
    const p = v6Match[2] ? Number(v6Match[2]) : fallbackPort;
    if (!Number.isInteger(p) || p < 1 || p > 65535) return null;
    const host = `[${v6Match[1]}]`;
    if (!isValidSocketHost(host)) return null;
    return {
      host,
      port: p,
      explicitPort: Boolean(v6Match[2]),
    };
  }

  const colonCount = (clean.match(/:/g) || []).length;
  if (colonCount > 1) {
    const host = `[${clean}]`;
    if (!isValidSocketHost(host)) return null;
    return { host, port: fallbackPort, explicitPort: false };
  }

  const idx = clean.lastIndexOf(':');
  if (idx > 0) {
    const portNum = Number(clean.slice(idx + 1));
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) {
      return null;
    }
    const host = clean.slice(0, idx);
    if (!isValidSocketHost(host)) return null;
    return { host, port: portNum, explicitPort: true };
  }

  if (!isValidSocketHost(clean)) return null;
  return { host: clean, port: fallbackPort, explicitPort: false };
}

function extractPathProxyOverride(url) {
  try {
    const qProxy = url.searchParams.get('proxyip');
    if (qProxy) return qProxy.trim();
    const m = url.pathname.match(/\/proxyip=([^/]+)/i);
    if (m) return safeDecodeURIComponent(m[1], '').trim();
  } catch {
    /* ignore malformed query/path */
  }
  return '';
}

export async function handleTunnelWS(request, env, ctx, runtime) {
  const settings = runtime?.settings || {};
  const users = Array.isArray(runtime?.users) ? runtime.users : [];

  if (settings.killSwitch) {
    return new Response('NIGHTLINE: Kill Switch active — tunnel paused.', { status: 503 });
  }

  const now = Date.now();
  const vlessMap = new Map();
  const trojanMap = new Map();

  for (const u of users) {
    if (!u) continue;
    const st = evaluateUserStatus(u, now);
    if (!st.ok) continue;
    if (settings.enableVless && u.uuid) {
      vlessMap.set(String(u.uuid).toLowerCase(), u);
    }
    if (settings.enableTrojan && u.trojanHash) {
      trojanMap.set(String(u.trojanHash).toLowerCase(), u);
    }
  }

  let pathProxy = '';
  try {
    pathProxy = extractPathProxyOverride(new URL(request.url));
  } catch {
    pathProxy = '';
  }

  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();

  const earlyDataHeader = request.headers.get('sec-websocket-protocol') || '';
  const readable = makeReadableWS(server, earlyDataHeader);

  const MAX_REPLAY_BYTES = 65536;
  const remote = {
    socket: null,
    activeSocket: null,
    confirmed: false,
    canFallback: true,
    dead: false,
    replayChunks: [],
    replayBytes: 0,
  };
  let headerProcessed = false;
  let udpWrite = null;
  let matchedUser = null;
  let upBytes = 0;
  let downBytes = 0;
  let accounted = false;

  const finalizeAccounting = () => {
    if (accounted || !matchedUser) return;
    accounted = true;
    recordTraffic(env, ctx, matchedUser, upBytes, downBytes);
  };

  readable
    .pipeTo(
      new WritableStream({
        async write(chunk) {
          const bytes = await toBytes(chunk);
          if (!bytes || bytes.byteLength === 0) {
            if (!headerProcessed) throw new Error('empty initial frame');
            return;
          }
          upBytes += bytes.byteLength;

          if (udpWrite) {
            await udpWrite(bytes);
            return;
          }

          if (headerProcessed) {
            if (remote.dead) {
              throw new Error('Outbound TCP socket unavailable');
            }
            if (!remote.confirmed) {
              if (remote.replayBytes + bytes.byteLength <= MAX_REPLAY_BYTES) {
                remote.replayChunks.push(bytes.slice());
                remote.replayBytes += bytes.byteLength;
              } else {
                remote.confirmed = true;
                remote.replayChunks.length = 0;
                remote.replayBytes = 0;
              }
            }
            if (!remote.activeSocket) {
              // Currently dialing or switching to ProxyIP/NAT64 fallback; buffered in replayChunks
              return;
            }
            try {
              await safeWriteSocket(remote.activeSocket, bytes);
            } catch (writeErr) {
              if (remote.confirmed || !remote.canFallback) {
                throw writeErr;
              }
            }
            return;
          }

          headerProcessed = true;
          const parsed = parseTunnelHeader(bytes, vlessMap, trojanMap);
          if (parsed.hasError) {
            throw new Error(parsed.message);
          }

          matchedUser = parsed.user;
          const {
            protocol,
            addressRemote,
            portRemote,
            rawClientData,
            responseHeader,
            isUDP,
          } = parsed;

          if (isUDP) {
            if (protocol === 'vless' && portRemote !== 53) {
              throw new Error('UDP relay is supported for DNS (port 53) only');
            }
            const dnsHandler = createDnsRelay(
              server,
              responseHeader,
              protocol,
              settings.upstreamDoH,
              (n) => { downBytes += n; }
            );
            udpWrite = dnsHandler.write;
            if (rawClientData && rawClientData.byteLength > 0) {
              await udpWrite(rawClientData);
            }
            return;
          }

          if (rawClientData && rawClientData.byteLength > 0) {
            remote.replayChunks.push(rawClientData.slice());
            remote.replayBytes += rawClientData.byteLength;
          }

          startTcpSession({
            remote,
            addressRemote,
            portRemote,
            ws: server,
            responseHeader,
            settings,
            pathProxy,
            onDownBytes: (n) => { downBytes += n; },
            onClose: finalizeAccounting,
          }).catch(() => {
            remote.dead = true;
            remote.activeSocket = null;
            finalizeAccounting();
            safeCloseSocket(remote.socket);
            safeCloseWS(server);
          });
        },
        close() {
          remote.dead = true;
          remote.activeSocket = null;
          finalizeAccounting();
          safeCloseSocket(remote.socket);
        },
        abort() {
          remote.dead = true;
          remote.activeSocket = null;
          finalizeAccounting();
          safeCloseSocket(remote.socket);
        },
      })
    )
    .catch(() => {
      remote.dead = true;
      remote.activeSocket = null;
      finalizeAccounting();
      safeCloseSocket(remote.socket);
      safeCloseWS(server);
    });

  return new Response(null, { status: 101, webSocket: client });
}

/* ----------------------------------------------------- protocol auto-parse */

export function parseTunnelHeader(bytes, vlessMap, trojanMap) {
  try {
    if (!bytes || bytes.byteLength < 24) {
      return { hasError: true, message: 'frame too short' };
    }

    // 1) Trojan: [56 hex chars][0x0d 0x0a][CMD][ATYP][DST.ADDR][DST.PORT][0x0d 0x0a][PAYLOAD]
    if (bytes.byteLength >= 64 && bytes[56] === 0x0d && bytes[57] === 0x0a) {
      let isHex = true;
      for (let i = 0; i < 56; i++) {
        const c = bytes[i];
        if (!((c >= 0x30 && c <= 0x39) || (c >= 0x61 && c <= 0x66) || (c >= 0x41 && c <= 0x46))) {
          isHex = false;
          break;
        }
      }
      if (isHex) {
        const hashHex = DEC.decode(bytes.subarray(0, 56)).toLowerCase();
        const user = trojanMap.get(hashHex);
        if (!user) return { hasError: true, message: 'invalid trojan user' };

        const cmd = bytes[58];
        if (cmd !== 1 && cmd !== 3) {
          return { hasError: true, message: 'unsupported trojan command' };
        }
        const isUDP = cmd === 3;
        const atyp = bytes[59];
        let addrIdx = 60;
        let addrLen = 0;
        let addressRemote = '';

        if (atyp === 1) {
          addrLen = 4;
          if (bytes.byteLength < addrIdx + addrLen + 4) return { hasError: true, message: 'truncated trojan ipv4' };
          addressRemote = `${bytes[addrIdx]}.${bytes[addrIdx + 1]}.${bytes[addrIdx + 2]}.${bytes[addrIdx + 3]}`;
        } else if (atyp === 3) {
          if (bytes.byteLength < addrIdx + 1) return { hasError: true, message: 'truncated trojan domain len' };
          addrLen = bytes[addrIdx];
          addrIdx += 1;
          if (bytes.byteLength < addrIdx + addrLen + 4) return { hasError: true, message: 'truncated trojan domain' };
          addressRemote = DEC.decode(bytes.subarray(addrIdx, addrIdx + addrLen));
        } else if (atyp === 4) {
          addrLen = 16;
          if (bytes.byteLength < addrIdx + addrLen + 4) return { hasError: true, message: 'truncated trojan ipv6' };
          addressRemote = `[${formatIPv6(bytes.subarray(addrIdx, addrIdx + 16))}]`;
        } else {
          return { hasError: true, message: 'invalid trojan atyp' };
        }

        const portIdx = addrIdx + addrLen;
        const portRemote = (bytes[portIdx] << 8) | bytes[portIdx + 1];
        if (portRemote < 1 || portRemote > 65535) {
          return { hasError: true, message: 'invalid trojan port' };
        }
        if (bytes[portIdx + 2] !== 0x0d || bytes[portIdx + 3] !== 0x0a) {
          return { hasError: true, message: 'missing trojan crlf' };
        }

        return {
          hasError: false,
          protocol: 'trojan',
          user,
          addressRemote,
          portRemote,
          rawClientData: bytes.subarray(portIdx + 4),
          responseHeader: null,
          isUDP,
        };
      }
    }

    // 2) VLESS: [ver(1)][uuid(16)][optLen(1)][cmd(1)][port(2)][atyp(1)][addr...][payload]
    const version = bytes[0];
    const uuid = stringifyUUID(bytes.subarray(1, 17));
    const user = vlessMap.get(uuid);
    if (!user) {
      return { hasError: true, message: 'invalid vless user' };
    }

    const optLength = bytes[17];
    const cmdIdx = 18 + optLength;
    // Note: in VLESS v0, byte 17 is optLength, followed by [optBytes (optLength)][cmd(1)][port(2)][atyp(1)]!
    if (bytes.byteLength < cmdIdx + 4) {
      return { hasError: true, message: 'truncated vless header' };
    }
    const command = bytes[cmdIdx];
    if (command !== 1 && command !== 2) {
      return { hasError: true, message: 'unsupported vless command' };
    }
    const isUDP = command === 2;

    const portIndex = cmdIdx + 1;
    const portRemote = (bytes[portIndex] << 8) | bytes[portIndex + 1];
    if (portRemote < 1 || portRemote > 65535) {
      return { hasError: true, message: 'invalid vless port' };
    }

    const addressIndex = portIndex + 2;
    const addressType = bytes[addressIndex];
    let addressValueIndex = addressIndex + 1;
    let addressLength = 0;
    let addressRemote = '';

    if (addressType === 1) {
      addressLength = 4;
      if (bytes.byteLength < addressValueIndex + 4) return { hasError: true, message: 'truncated ipv4' };
      addressRemote = `${bytes[addressValueIndex]}.${bytes[addressValueIndex + 1]}.${bytes[addressValueIndex + 2]}.${bytes[addressValueIndex + 3]}`;
    } else if (addressType === 2) {
      if (bytes.byteLength < addressValueIndex + 1) return { hasError: true, message: 'truncated domain len' };
      addressLength = bytes[addressValueIndex];
      addressValueIndex += 1;
      if (bytes.byteLength < addressValueIndex + addressLength) return { hasError: true, message: 'truncated domain' };
      addressRemote = DEC.decode(bytes.subarray(addressValueIndex, addressValueIndex + addressLength));
    } else if (addressType === 3) {
      addressLength = 16;
      if (bytes.byteLength < addressValueIndex + 16) return { hasError: true, message: 'truncated ipv6' };
      addressRemote = `[${formatIPv6(bytes.subarray(addressValueIndex, addressValueIndex + 16))}]`;
    } else {
      return { hasError: true, message: 'unsupported vless address type' };
    }

    if (!addressRemote) {
      return { hasError: true, message: 'empty target address' };
    }

    return {
      hasError: false,
      protocol: 'vless',
      user,
      addressRemote,
      portRemote,
      rawClientData: bytes.subarray(addressValueIndex + addressLength),
      responseHeader: new Uint8Array([version, 0]),
      isUDP,
    };
  } catch (err) {
    return { hasError: true, message: err && err.message ? err.message : 'header parse failed' };
  }
}

function formatIPv6(slice16) {
  const groups = [];
  for (let i = 0; i < 16; i += 2) {
    groups.push(((slice16[i] << 8) | slice16[i + 1]).toString(16));
  }
  return groups.join(':');
}

function stringifyUUID(bytes) {
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function concatChunksSlice(chunks, startIdx) {
  let total = 0;
  for (let i = startIdx; i < chunks.length; i++) {
    total += chunks[i].byteLength;
  }
  if (total === 0) return new Uint8Array(0);
  const out = new Uint8Array(total);
  let off = 0;
  for (let i = startIdx; i < chunks.length; i++) {
    out.set(chunks[i], off);
    off += chunks[i].byteLength;
  }
  return out;
}

/* ---------------------------------------------------- outbound TCP dialing */

async function startTcpSession({
  remote,
  addressRemote,
  portRemote,
  ws,
  responseHeader,
  settings,
  pathProxy,
  onDownBytes,
  onClose,
}) {
  // Build deduplicated fallback relay queue: path override -> admin ProxyIPs -> built-in verified relays
  const rawRelays = pathProxy
    ? [pathProxy, ...(Array.isArray(settings.proxyIPs) ? settings.proxyIPs : []), ...DEFAULT_PROXY_IPS]
    : [...(Array.isArray(settings.proxyIPs) ? settings.proxyIPs : []), ...DEFAULT_PROXY_IPS];
  const relayQueue = [];
  const seenRelays = new Set();
  for (const r of rawRelays) {
    const key = String(r || '').trim();
    if (!key || seenRelays.has(key.toLowerCase())) continue;
    seenRelays.add(key.toLowerCase());
    relayQueue.push(key);
    if (relayQueue.length >= 6) break;
  }
  let relayCursor = 0;
  let nat64Tried = false;

  const terminateSession = () => {
    remote.dead = true;
    remote.canFallback = false;
    remote.activeSocket = null;
    remote.replayChunks.length = 0;
    remote.replayBytes = 0;
    onClose();
    safeCloseWS(ws);
  };

  const attachAndPipe = async (socket, initialPreRead = null, allowRetry = false) => {
    remote.activeSocket = null;
    remote.canFallback = allowRetry;

    try {
      if (socket.opened) await socket.opened;
    } catch {
      safeCloseSocket(socket);
      if (allowRetry && !remote.dead && ws.readyState === WS_OPEN) return fallbackDial();
      terminateSession();
      return;
    }

    remote.socket = socket;
    try {
      let syncedIdx = 0;
      while (syncedIdx < remote.replayChunks.length) {
        const batch = concatChunksSlice(remote.replayChunks, syncedIdx);
        syncedIdx = remote.replayChunks.length;
        if (batch.byteLength > 0) {
          await safeWriteSocket(socket, batch);
        }
      }
      remote.activeSocket = socket;
    } catch {
      remote.activeSocket = null;
      safeCloseSocket(socket);
      if (allowRetry && !remote.dead && ws.readyState === WS_OPEN) return fallbackDial();
      terminateSession();
      return;
    }

    await pipeRemoteToWS({
      remote,
      remoteSocket: socket,
      ws,
      responseHeader,
      initialPreRead,
      onDownBytes,
      onClose: terminateSession,
      retryFn: allowRetry ? fallbackDial : null,
    });
  };

  const fallbackDial = async () => {
    remote.activeSocket = null;
    safeCloseSocket(remote.socket);
    remote.socket = null;

    if (remote.dead || ws.readyState !== WS_OPEN) {
      terminateSession();
      return;
    }

    // 1) Try ProxyIP relays sequentially (essential for Cloudflare-hosted targets like speedtest.net)
    while (relayCursor < relayQueue.length && relayCursor < 3) {
      const pick = relayQueue[relayCursor++];
      const ep = parseEndpoint(pick, portRemote);
      if (!ep) continue;
      const hasMoreFallbacks = relayCursor < Math.min(relayQueue.length, 3) || Boolean(settings.enableNAT64);
      try {
        const sock = await safeConnect(ep.host, ep.explicitPort ? ep.port : portRemote);
        await attachAndPipe(sock, null, hasMoreFallbacks);
        return;
      } catch {
        /* try next relay or NAT64 */
      }
    }

    // 2) Try NAT64 IPv6 synthesis (works on Cloudflare's dual-stack global network)
    if (!nat64Tried && settings.enableNAT64 !== false) {
      nat64Tried = true;
      const prefixes =
        Array.isArray(settings.nat64Prefixes) && settings.nat64Prefixes.length > 0
          ? settings.nat64Prefixes
          : DEFAULT_NAT64_PREFIXES;
      try {
        const natHost = await resolveNat64Target(addressRemote, prefixes, settings.upstreamDoH);
        if (natHost) {
          const sock = await safeConnect(natHost, portRemote);
          await attachAndPipe(sock, null, false);
          return;
        }
      } catch {
        /* ignore */
      }
    }

    terminateSession();
  };

  try {
    if (settings.chainProxy) {
      const chained = await dialChainProxy(settings.chainProxy, addressRemote, portRemote);
      await attachAndPipe(chained.socket, chained.leftover, true);
      return;
    }

    const directSocket = await safeConnect(addressRemote, portRemote);
    await attachAndPipe(directSocket, null, true);
  } catch {
    await fallbackDial();
  }
}

async function pipeRemoteToWS({
  remote,
  remoteSocket,
  ws,
  responseHeader,
  initialPreRead,
  onDownBytes,
  onClose,
  retryFn,
}) {
  let header = responseHeader;
  let received = false;

  const emitChunk = (chunkBytes) => {
    if (!chunkBytes || chunkBytes.byteLength === 0) return true;
    if (!received) {
      received = true;
      if (remote) {
        remote.confirmed = true;
        remote.canFallback = false;
        remote.replayChunks.length = 0;
        remote.replayBytes = 0;
      }
    }
    onDownBytes(chunkBytes.byteLength);
    if (header) {
      const merged = new Uint8Array(header.byteLength + chunkBytes.byteLength);
      merged.set(header, 0);
      merged.set(chunkBytes, header.byteLength);
      header = null;
      return safeSendWS(ws, merged);
    }
    return safeSendWS(ws, chunkBytes);
  };

  let reader = null;
  try {
    if (initialPreRead && initialPreRead.byteLength > 0) {
      if (!emitChunk(initialPreRead)) {
        onClose();
        return;
      }
    }

    reader = remoteSocket.readable.getReader();
    if (reader.closed && typeof reader.closed.catch === 'function') {
      reader.closed.catch(() => {});
    }
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const bytes = await toBytes(value);
      if (!emitChunk(bytes)) break;
    }
  } catch {
    /* expected when remote or client closes */
  } finally {
    if (reader) {
      try {
        const cp = reader.cancel();
        if (cp && typeof cp.catch === 'function') await cp.catch(() => {});
      } catch {}
      try { reader.releaseLock(); } catch {}
    }
    safeCloseSocket(remoteSocket);
  }

  if (!received && retryFn && ws.readyState === WS_OPEN) {
    await retryFn();
    return;
  }

  onClose();
}

/* -------------------------------------------------- NAT64 & Chain Outbound */

export function ipv4ToNat64(ipv4, prefix = '2602:fc59:b0:64::') {
  const parts = String(ipv4 || '').trim().split('.').map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
    return null;
  }
  const hi = ((parts[0] << 8) | parts[1]).toString(16).padStart(4, '0');
  const lo = ((parts[2] << 8) | parts[3]).toString(16).padStart(4, '0');
  let cleanPrefix = String(prefix || '2602:fc59:b0:64::').trim().replace(/^\[|\]$/g, '');
  if (!cleanPrefix.endsWith(':')) cleanPrefix += ':';
  return `[${cleanPrefix}${hi}:${lo}]`;
}

async function resolveNat64Target(address, prefixes, upstreamDoH) {
  try {
    const prefix = prefixes[Math.floor(Math.random() * prefixes.length)];
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(address)) {
      return ipv4ToNat64(address, prefix);
    }
    if (!address || address.startsWith('[')) return null;

    const baseJsonDoh =
      !upstreamDoH || String(upstreamDoH).includes('1.1.1.1')
        ? 'https://cloudflare-dns.com/dns-query'
        : upstreamDoH;
    const dohUrl = new URL(baseJsonDoh);
    dohUrl.searchParams.set('name', address);
    dohUrl.searchParams.set('type', 'A');
    const res = await fetchWithTimeout(
      dohUrl.toString(),
      { headers: { accept: 'application/dns-json' } },
      4500
    );
    if (!res.ok) return null;
    const data = await res.json();
    const aRec = (data?.Answer || []).find((r) => r && r.type === 1 && /^\d+\.\d+\.\d+\.\d+$/.test(r.data));
    return aRec ? ipv4ToNat64(aRec.data, prefix) : null;
  } catch {
    return null;
  }
}

async function dialChainProxy(chainSpec, targetHost, targetPort) {
  const raw = /^https?:\/\/|^socks5?:\/\//i.test(String(chainSpec || '').trim())
    ? String(chainSpec).trim()
    : `socks5://${String(chainSpec || '').trim()}`;

  let u;
  try {
    u = new URL(raw);
  } catch {
    throw new Error('Invalid chain proxy URL');
  }

  const proto = u.protocol.replace(':', '').toLowerCase();
  const proxyHost = u.hostname;
  const proxyPort = Number(u.port) || (proto.startsWith('socks') ? 1080 : 8080);
  const username = u.username ? safeDecodeURIComponent(u.username) : '';
  const password = u.password ? safeDecodeURIComponent(u.password) : '';

  const socket = await safeConnect(proxyHost, proxyPort);
  try {
    if (socket.opened) await socket.opened;
  } catch (err) {
    safeCloseSocket(socket);
    throw err;
  }
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  if (writer.closed && typeof writer.closed.catch === 'function') writer.closed.catch(() => {});
  if (writer.ready && typeof writer.ready.catch === 'function') writer.ready.catch(() => {});
  if (reader.closed && typeof reader.closed.catch === 'function') reader.closed.catch(() => {});

  let rxBuf = new Uint8Array(0);
  const readAtLeast = async (n) => {
    while (rxBuf.byteLength < n) {
      const { value, done } = await reader.read();
      if (done) throw new Error('Chain proxy closed prematurely');
      const chunk = await toBytes(value);
      const merged = new Uint8Array(rxBuf.byteLength + chunk.byteLength);
      merged.set(rxBuf, 0);
      merged.set(chunk, rxBuf.byteLength);
      rxBuf = merged;
    }
    const slice = rxBuf.subarray(0, n);
    rxBuf = rxBuf.subarray(n);
    return slice;
  };

  let succeeded = false;
  try {
    if (proto === 'socks5' || proto === 'socks') {
      const methods = username ? new Uint8Array([0x05, 0x02, 0x00, 0x02]) : new Uint8Array([0x05, 0x01, 0x00]);
      await writer.write(methods);
      const greet = await readAtLeast(2);
      if (greet[0] !== 0x05 || greet[1] === 0xff) throw new Error('SOCKS5 auth method rejected');

      if (greet[1] === 0x02) {
        const uBytes = ENC.encode(username);
        const pBytes = ENC.encode(password);
        if (uBytes.length > 255 || pBytes.length > 255) throw new Error('SOCKS5 credentials too long');
        const authFrame = new Uint8Array(3 + uBytes.length + pBytes.length);
        authFrame[0] = 0x01;
        authFrame[1] = uBytes.length;
        authFrame.set(uBytes, 2);
        authFrame[2 + uBytes.length] = pBytes.length;
        authFrame.set(pBytes, 3 + uBytes.length);
        await writer.write(authFrame);
        const authRes = await readAtLeast(2);
        if (authRes[1] !== 0x00) throw new Error('SOCKS5 credentials rejected');
      }

      const cleanTarget = String(targetHost || '').replace(/^\[|\]$/g, '');
      const hBytes = ENC.encode(cleanTarget);
      if (!hBytes.length || hBytes.length > 255) throw new Error('Target domain length invalid for SOCKS5');
      const req = new Uint8Array(7 + hBytes.length);
      req[0] = 0x05;
      req[1] = 0x01;
      req[2] = 0x00;
      req[3] = 0x03;
      req[4] = hBytes.length;
      req.set(hBytes, 5);
      req[5 + hBytes.length] = (targetPort >> 8) & 0xff;
      req[6 + hBytes.length] = targetPort & 0xff;
      await writer.write(req);

      const repHead = await readAtLeast(4);
      if (repHead[1] !== 0x00) throw new Error(`SOCKS5 connect failed (code ${repHead[1]})`);
      const atyp = repHead[3];
      if (atyp === 1) await readAtLeast(6);
      else if (atyp === 4) await readAtLeast(18);
      else if (atyp === 3) {
        const dlen = (await readAtLeast(1))[0];
        await readAtLeast(dlen + 2);
      }
    } else if (proto === 'http') {
      const authHeader = username
        ? `Proxy-Authorization: Basic ${utf8ToBase64(`${username}:${password}`)}\r\n`
        : '';
      const connectReq =
        `CONNECT ${targetHost}:${targetPort} HTTP/1.1\r\n` +
        `Host: ${targetHost}:${targetPort}\r\n` +
        authHeader +
        `Proxy-Connection: Keep-Alive\r\n\r\n`;
      await writer.write(ENC.encode(connectReq));

      let headerText = '';
      while (!headerText.includes('\r\n\r\n')) {
        const b = await readAtLeast(1);
        headerText += String.fromCharCode(b[0]);
        if (headerText.length > 8192) throw new Error('HTTP proxy header overflow');
      }
      if (!/^HTTP\/1\.[01]\s+200\b/i.test(headerText)) {
        throw new Error('HTTP CONNECT rejected');
      }
    } else {
      throw new Error(`Unsupported chain protocol: ${proto}`);
    }
    succeeded = true;
  } finally {
    try { writer.releaseLock(); } catch {}
    try { reader.releaseLock(); } catch {}
    if (!succeeded) {
      safeCloseSocket(socket);
    }
  }

  return { socket, leftover: rxBuf };
}

/* -------------------------------------------------------- UDP DNS over DoH */

function createDnsRelay(ws, responseHeader, protocol, upstreamDoH, onDownBytes) {
  let sentVlessHeader = false;
  const dohEndpoint = upstreamDoH || 'https://1.1.1.1/dns-query';

  const resolveQuery = async (dnsWireBytes, trojanPrefix = null) => {
    try {
      if (!dnsWireBytes || dnsWireBytes.byteLength === 0) return;
      const res = await fetchWithTimeout(
        dohEndpoint,
        {
          method: 'POST',
          headers: { 'content-type': 'application/dns-message' },
          body: dnsWireBytes,
        },
        7000
      );
      if (!res.ok) return;
      const answer = new Uint8Array(await res.arrayBuffer());
      if (ws.readyState !== WS_OPEN || answer.byteLength === 0) return;

      if (protocol === 'trojan' && trojanPrefix) {
        const frame = new Uint8Array(trojanPrefix.byteLength + 4 + answer.byteLength);
        frame.set(trojanPrefix, 0);
        frame[trojanPrefix.byteLength] = (answer.byteLength >> 8) & 0xff;
        frame[trojanPrefix.byteLength + 1] = answer.byteLength & 0xff;
        frame[trojanPrefix.byteLength + 2] = 0x0d;
        frame[trojanPrefix.byteLength + 3] = 0x0a;
        frame.set(answer, trojanPrefix.byteLength + 4);
        if (safeSendWS(ws, frame)) onDownBytes(frame.byteLength);
      } else {
        const frame = new Uint8Array(2 + answer.byteLength);
        frame[0] = (answer.byteLength >> 8) & 0xff;
        frame[1] = answer.byteLength & 0xff;
        frame.set(answer, 2);
        if (!sentVlessHeader && responseHeader) {
          sentVlessHeader = true;
          const merged = new Uint8Array(responseHeader.byteLength + frame.byteLength);
          merged.set(responseHeader, 0);
          merged.set(frame, responseHeader.byteLength);
          if (safeSendWS(ws, merged)) onDownBytes(merged.byteLength);
        } else if (safeSendWS(ws, frame)) {
          onDownBytes(frame.byteLength);
        }
      }
    } catch {
      /* ignore failed DoH query */
    }
  };

  const write = async (bytes) => {
    try {
      if (!bytes || bytes.byteLength < 2) {
        safeCloseWS(ws);
        return;
      }
      if (protocol === 'trojan') {
        let offset = 0;
        let parsedAny = false;
        while (offset + 8 <= bytes.byteLength) {
          const atyp = bytes[offset];
          let addrEnd = offset + 1;
          if (atyp === 1) addrEnd += 4;
          else if (atyp === 3) {
            if (offset + 2 > bytes.byteLength) break;
            addrEnd += 1 + bytes[offset + 1];
          } else if (atyp === 4) addrEnd += 16;
          else break;
          const prefixEnd = addrEnd + 2;
          if (prefixEnd + 4 > bytes.byteLength) break;
          const len = (bytes[prefixEnd] << 8) | bytes[prefixEnd + 1];
          const payloadStart = prefixEnd + 4;
          if (payloadStart + len > bytes.byteLength) break;
          const prefix = bytes.subarray(offset, prefixEnd);
          const query = bytes.subarray(payloadStart, payloadStart + len);
          offset = payloadStart + len;
          parsedAny = true;
          resolveQuery(query, prefix).catch(() => {});
        }
        if (!parsedAny) safeCloseWS(ws);
        return;
      }

      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      let parsedAny = false;
      for (let i = 0; i + 2 <= bytes.byteLength; ) {
        const len = view.getUint16(i, false);
        if (len === 0 || i + 2 + len > bytes.byteLength) break;
        const query = bytes.subarray(i + 2, i + 2 + len);
        i += 2 + len;
        parsedAny = true;
        resolveQuery(query, null).catch(() => {});
      }
      if (!parsedAny) safeCloseWS(ws);
    } catch {
      safeCloseWS(ws);
    }
  };

  return { write };
}

/* --------------------------------------------------- Private DoH HTTP route */

function escapeHtmlBasic(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderDoHGuideHtml({ dohUrl, upstream, ok, rttMs, sampleIp }) {
  const safeUrl = escapeHtmlBasic(dohUrl);
  const safeUp = escapeHtmlBasic(upstream);
  const safeIp = escapeHtmlBasic(sampleIp || '—');
  return `<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex,nofollow" />
<title>NIGHTLINE — Private DoH Resolver</title>
<style>
  :root{--bg:#f4efe4;--paper:#fbf8f1;--ink:#111111;--muted:#52504b;--line:#111111;--ok:#14532d;--ok-bg:#dcfce7;--bad:#7f1d1d;--bad-bg:#fee2e2;}
  [data-theme="dark"]{--bg:#0c0c0d;--paper:#151517;--ink:#f1ece1;--muted:#a39e93;--line:#e5dfd2;--ok:#86efac;--ok-bg:#14301f;--bad:#fca5a5;--bad-bg:#3b1616;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--ink);font-family:Tahoma,system-ui,-apple-system,sans-serif;line-height:1.7;padding:24px 14px;}
  .sheet{max-width:760px;margin:0 auto;background:var(--paper);border:2px solid var(--line);box-shadow:6px 6px 0 var(--line);padding:24px;}
  .top{display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:3px double var(--line);padding-bottom:12px;margin-bottom:16px;flex-wrap:wrap;}
  .brand{font-family:Georgia,serif;font-weight:900;font-size:1.5rem;letter-spacing:.08em;}
  .badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border:1.5px solid var(--line);font-weight:700;font-size:.82rem;background:${ok ? 'var(--ok-bg)' : 'var(--bad-bg)'};color:${ok ? 'var(--ok)' : 'var(--bad)'};}
  .url-box{direction:ltr;text-align:left;font-family:ui-monospace,Consolas,monospace;font-size:.86rem;background:var(--bg);border:1.5px solid var(--line);padding:12px;word-break:break-all;margin:12px 0;}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;}
  button,a.btn{cursor:pointer;background:var(--ink);color:var(--paper);border:1.5px solid var(--line);padding:8px 14px;font-weight:700;font-size:.85rem;text-decoration:none;display:inline-flex;align-items:center;gap:6px;--sh-d:3px;box-shadow:var(--sh-d) var(--sh-d) 0 0 var(--line);transform:translate3d(0,0,0);transition:transform .16s cubic-bezier(.22,1,.36,1),box-shadow .16s cubic-bezier(.22,1,.36,1);}
  button:hover,a.btn:hover{transform:translate3d(-1px,-1px,0);box-shadow:calc(var(--sh-d) + 1px) calc(var(--sh-d) + 1px) 0 0 var(--line);}
  button:active,a.btn:active{transform:translate3d(var(--sh-d),var(--sh-d),0);box-shadow:0 0 0 0 var(--line);transition-duration:.06s;}
  button.sec,a.btn.sec{background:var(--paper);color:var(--ink);}
  .ic{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:0 0 auto;}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:14px 0 18px;}
  .kv{border:1px solid var(--line);padding:10px;background:var(--bg);}
  .kv small{display:block;color:var(--muted);font-size:.75rem;}
  .kv b{font-family:ui-monospace,Consolas,monospace;font-size:.92rem;direction:ltr;display:inline-block;}
  h2{font-size:1rem;border-bottom:1px solid var(--line);padding-bottom:5px;margin:16px 0 10px;}
  ul{padding-right:20px;font-size:.9rem;}
  li{margin-bottom:7px;}
  code{font-family:ui-monospace,Consolas,monospace;background:var(--bg);padding:1px 5px;border:1px solid var(--line);direction:ltr;display:inline-block;font-size:.82rem;}
  .foot{margin-top:20px;padding-top:10px;border-top:1px solid var(--line);font-size:.78rem;color:var(--muted);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;}
</style>
</head>
<body>
  <div class="sheet">
    <div class="top">
      <div>
        <div class="brand">NIGHTLINE · PRIVATE DoH</div>
        <div style="font-size:.82rem;color:var(--muted)">سرور اختصاصی DNS-over-HTTPS (ضد تحریم و ضد مسمومیت DNS) · V${PANEL_VERSION}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;">
        <span class="badge">${ok ? `<svg class="ic" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg> فعال و آماده (${rttMs}ms)` : `<svg class="ic" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/></svg> خطای ارتباط بالادستی`}</span>
        <button class="sec" type="button" id="th-btn" title="تغییر تم"><svg class="ic" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>
      </div>
    </div>

    <p style="font-size:.92rem;">این آدرس یک <b>سرور استاندارد RFC 8484 DNS-over-HTTPS</b> است. باز کردن مستقیم آن در مرورگر این برگهٔ راهنما را نمایش می‌دهد؛ برای استفاده، آدرس زیر را در بخش <b>Secure DNS</b> مرورگر یا کلاینت خود قرار دهید:</p>

    <div class="url-box" id="doh-url">${safeUrl}</div>
    <div class="btns">
      <button type="button" id="copy-btn"><svg class="ic" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="1"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/></svg><span id="copy-lbl">کپی آدرس DoH اختصاصی</span></button>
      <a class="btn sec" href="${safeUrl}?name=example.com&type=A" target="_blank" rel="noopener"><span>تست زندهٔ JSON DNS (example.com)</span><svg class="ic" viewBox="0 0 24 24"><path d="M7 17L17 7M7 7h10v10"/></svg></a>
    </div>

    <div class="grid">
      <div class="kv"><small>پروتکل‌های پشتیبانی‌شده</small><b>RFC 8484 (POST / ?dns=) + JSON</b></div>
      <div class="kv"><small>سرور بالادستی</small><b>${safeUp}</b></div>
      <div class="kv"><small>تست رزولوشن زنده</small><b>${safeIp} (${rttMs}ms)</b></div>
    </div>

    <h2>نحوهٔ تنظیم در مرورگرها و کلاینت‌ها</h2>
    <ul>
      <li><b>Chrome / Brave / Edge (ویندوز، مک و اندروید):</b> وارد <code>Settings → Privacy and security → Security → Use secure DNS</code> شوید، گزینهٔ <code>With Custom</code> را انتخاب کنید و لینک بالا را جای‌گذاری کنید.</li>
      <li><b>Firefox:</b> وارد <code>Settings → Privacy & Security → DNS over HTTPS → Max Protection</code> شوید، گزینهٔ <code>Custom</code> را بزنید و لینک بالا را وارد کنید.</li>
      <li><b>v2rayNG / MahsaNG / Hiddify / Streisand:</b> در تنظیمات <code>Remote DNS</code> یا <code>DoH</code> کلاینت، همین لینک کامل <code>https://...</code> را قرار دهید (در اشتراک‌های NIGHTLINE به‌صورت خودکار تزریق شده است).</li>
      <li><b>نکتهٔ مهم اندروید:</b> بخش <i>Private DNS</i> در تنظیمات سیستم‌عامل اندروید فقط پروتکل قدیمی <code>DoT (پورت 853 بدون https://)</code> می‌پذیرد؛ برای اندروید لینک بالا را داخل <b>Chrome / Firefox</b> یا برنامهٔ <b>Intra / RethinkDNS</b> وارد کنید.</li>
    </ul>

    <div class="foot">
      <span style="display:inline-flex;align-items:center;gap:6px"><svg class="ic" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg> سرویس کاملاً رایگان و متن‌باز — غیرقابل فروش (100% Free &amp; Not For Sale)</span>
      <span dir="ltr">NIGHTLINE V${PANEL_VERSION}</span>
    </div>
  </div>
  <script>
    (function(){
      var root=document.documentElement;
      var saved=localStorage.getItem('nl_theme');
      if(saved==='dark')root.setAttribute('data-theme','dark');
      document.getElementById('th-btn').onclick=function(){
        var next=root.getAttribute('data-theme')==='dark'?'light':'dark';
        root.setAttribute('data-theme',next);
        localStorage.setItem('nl_theme',next);
      };
      document.getElementById('copy-btn').onclick=function(){
        var lbl=document.getElementById('copy-lbl');
        navigator.clipboard.writeText(document.getElementById('doh-url').textContent).then(function(){
          lbl.textContent='کپی شد';
          setTimeout(function(){lbl.textContent='کپی آدرس DoH اختصاصی';},1800);
        });
      };
    })();
  </script>
</body>
</html>`;
}

export async function handlePrivateDoH(request, upstreamDoH = 'https://1.1.1.1/dns-query') {
  try {
    const url = new URL(request.url);
    const safeUpstream = /^https:\/\//i.test(String(upstreamDoH || ''))
      ? upstreamDoH
      : 'https://1.1.1.1/dns-query';

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'access-control-allow-origin': '*',
          'access-control-allow-methods': 'GET, POST, OPTIONS',
          'access-control-allow-headers': 'content-type, accept',
        },
      });
    }

    if (request.method === 'POST') {
      const body = await request.arrayBuffer();
      if (!body || body.byteLength === 0 || body.byteLength > 65535) {
        return new Response('Invalid DNS message size', { status: 400 });
      }
      const res = await fetchWithTimeout(
        safeUpstream,
        {
          method: 'POST',
          headers: {
            'content-type': 'application/dns-message',
            accept: 'application/dns-message',
          },
          body,
        },
        8000
      );
      return new Response(res.body, {
        status: res.status,
        headers: {
          'content-type': res.headers.get('content-type') || 'application/dns-message',
          'cache-control': 'no-store',
          'access-control-allow-origin': '*',
        },
      });
    }

    if (request.method === 'GET' || request.method === 'HEAD') {
      // 1) RFC 8484 Wireformat DoH: GET /dns-query/<token>?dns=<base64url>
      if (url.searchParams.has('dns')) {
        const target = new URL(safeUpstream);
        target.searchParams.set('dns', url.searchParams.get('dns') || '');
        const res = await fetchWithTimeout(
          target.toString(),
          { method: 'GET', headers: { accept: 'application/dns-message' } },
          8000
        );
        return new Response(request.method === 'HEAD' ? null : res.body, {
          status: res.status,
          headers: {
            'content-type': res.headers.get('content-type') || 'application/dns-message',
            'cache-control': 'no-store',
            'access-control-allow-origin': '*',
          },
        });
      }

      // 2) JSON DoH API: GET /dns-query/<token>?name=example.com&type=A
      if (url.searchParams.has('name')) {
        const jsonBase = safeUpstream.includes('1.1.1.1')
          ? 'https://cloudflare-dns.com/dns-query'
          : safeUpstream;
        const target = new URL(jsonBase);
        for (const [k, v] of url.searchParams.entries()) {
          target.searchParams.set(k, v);
        }
        const res = await fetchWithTimeout(
          target.toString(),
          { method: 'GET', headers: { accept: 'application/dns-json' } },
          8000
        );
        return new Response(request.method === 'HEAD' ? null : res.body, {
          status: res.status,
          headers: {
            'content-type': 'application/dns-json; charset=utf-8',
            'cache-control': 'no-store',
            'access-control-allow-origin': '*',
          },
        });
      }

      // 3) Direct browser visit (`GET /dns-query/<token>` with no query params):
      //    Run a quick live upstream health check and render the interactive setup guide!
      const t0 = Date.now();
      let ok = false;
      let sampleIp = '';
      try {
        const probeBase = safeUpstream.includes('1.1.1.1')
          ? 'https://cloudflare-dns.com/dns-query'
          : safeUpstream;
        const probeUrl = new URL(probeBase);
        probeUrl.searchParams.set('name', 'example.com');
        probeUrl.searchParams.set('type', 'A');
        const probeRes = await fetchWithTimeout(
          probeUrl.toString(),
          { method: 'GET', headers: { accept: 'application/dns-json' } },
          3500
        );
        if (probeRes.ok) {
          const data = await probeRes.json();
          const aRec = (data?.Answer || []).find((r) => r && r.type === 1);
          ok = Number(data?.Status) === 0;
          sampleIp = aRec ? String(aRec.data) : 'OK';
        }
      } catch {
        ok = false;
      }
      const rttMs = Date.now() - t0;
      const cleanSelfUrl = `${url.origin}${url.pathname}`;
      const html = renderDoHGuideHtml({
        dohUrl: cleanSelfUrl,
        upstream: safeUpstream,
        ok,
        rttMs,
        sampleIp,
      });
      return new Response(request.method === 'HEAD' ? null : html, {
        status: 200,
        headers: {
          'content-type': 'text/html; charset=utf-8',
          'cache-control': 'no-store',
        },
      });
    }

    return new Response('Method Not Allowed', { status: 405 });
  } catch (err) {
    return new Response(
      JSON.stringify({ ok: false, error: 'Upstream DoH unreachable', detail: err?.message || String(err) }),
      { status: 502, headers: { 'content-type': 'application/json; charset=utf-8' } }
    );
  }
}

/* ------------------------------------------- Server-side ProxyIP / Chain probe */

export async function probeOutboundEndpoint(spec) {
  const t0 = Date.now();
  let tid = null;
  let openedSock = null;

  try {
    const raw = String(spec || '').trim();
    if (!raw) return { ok: false, error: 'آدرس خالی است' };

    const timeoutPromise = new Promise((_, rej) => {
      tid = setTimeout(() => rej(new Error('Timeout (5s)')), 5000);
    });

    if (/^(socks5?|http):\/\//i.test(raw)) {
      const chainPromise = dialChainProxy(raw, 'example.com', 80);
      // Disarm late rejection/resolution if timeout wins
      chainPromise
        .then((res) => { if (openedSock !== res.socket) safeCloseSocket(res.socket); })
        .catch(() => {});

      const chained = await Promise.race([chainPromise, timeoutPromise]);
      openedSock = chained.socket;
      safeCloseSocket(chained.socket);
      return { ok: true, target: raw.replace(/\/\/.*@/, '//***@'), latencyMs: Date.now() - t0 };
    }

    const ep = parseEndpoint(raw, 443);
    if (!ep) return { ok: false, error: 'فرمت آدرس نامعتبر است' };

    openedSock = await safeConnect(ep.host, ep.port);
    await Promise.race([openedSock.opened, timeoutPromise]);
    const latencyMs = Date.now() - t0;
    safeCloseSocket(openedSock);
    return { ok: true, target: `${ep.host}:${ep.port}`, latencyMs };
  } catch (err) {
    safeCloseSocket(openedSock);
    return {
      ok: false,
      error: err && err.message ? err.message : String(err),
      latencyMs: Date.now() - t0,
    };
  } finally {
    if (tid) clearTimeout(tid);
  }
}

/* ------------------------------------------------------- WebSocket helpers */

function makeReadableWS(ws, earlyDataHeader) {
  let streamDone = false;

  const safeEnqueue = (controller, data) => {
    if (streamDone || data == null) return;
    try {
      controller.enqueue(data);
    } catch {
      streamDone = true;
    }
  };

  const safeFinish = (controller) => {
    if (streamDone) return;
    streamDone = true;
    try {
      controller.close();
    } catch {
      /* ignore if already closed/errored */
    }
  };

  const safeFail = (controller, err) => {
    if (streamDone) return;
    streamDone = true;
    try {
      controller.error(err);
    } catch {
      /* ignore if already closed/errored */
    }
  };

  return new ReadableStream({
    start(controller) {
      ws.addEventListener('message', (event) => {
        safeEnqueue(controller, event.data);
      });
      ws.addEventListener('close', () => {
        safeCloseWS(ws);
        safeFinish(controller);
      });
      ws.addEventListener('error', (ev) => {
        try { if (ev && typeof ev.preventDefault === 'function') ev.preventDefault(); } catch {}
        safeCloseWS(ws);
        safeFinish(controller);
      });

      const { earlyData, error } = base64ToArrayBuffer(earlyDataHeader);
      if (error) {
        setTimeout(() => {
          safeCloseWS(ws);
          safeFinish(controller);
        }, 0);
      } else if (earlyData) {
        setTimeout(() => safeEnqueue(controller, earlyData), 0);
      }
    },
    cancel() {
      streamDone = true;
      safeCloseWS(ws);
    },
  });
}

async function toBytes(chunk) {
  try {
    if (!chunk) return new Uint8Array(0);
    if (chunk instanceof Uint8Array) return chunk;
    if (chunk instanceof Blob) return new Uint8Array(await chunk.arrayBuffer());
    if (chunk instanceof ArrayBuffer) return new Uint8Array(chunk);
    if (ArrayBuffer.isView(chunk)) return new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
    if (typeof chunk === 'string') return ENC.encode(chunk);
    return new Uint8Array(chunk);
  } catch {
    return new Uint8Array(0);
  }
}

function base64ToArrayBuffer(base64Str) {
  if (!base64Str || typeof base64Str !== 'string') return { earlyData: null, error: null };
  try {
    const trimmed = base64Str.split(',')[0].trim();
    if (!trimmed || /[^A-Za-z0-9+/_=-]/.test(trimmed)) {
      return { earlyData: null, error: null };
    }
    const norm = trimmed.replace(/-/g, '+').replace(/_/g, '/');
    const pad = norm + '==='.slice((norm.length + 3) % 4);
    const decoded = atob(pad);
    const bytes = new Uint8Array(decoded.length);
    for (let i = 0; i < decoded.length; i++) bytes[i] = decoded.charCodeAt(i);
    if (bytes.byteLength < 24) {
      return { earlyData: null, error: null };
    }
    return { earlyData: bytes, error: null };
  } catch {
    return { earlyData: null, error: null };
  }
}
