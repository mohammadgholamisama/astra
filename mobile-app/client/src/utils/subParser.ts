import { NativeModules, Platform } from 'react-native';
import type { ParsedConfig, Protocol } from '../types';

const encoder = new TextEncoder();

/** SHA-256 hex — native first (fast), then WebCrypto, then pure JS. */
async function sha256Hex(input: string): Promise<string> {
  const native = NativeModules.XrayPing as
    | { sha256?: (s: string) => Promise<string> }
    | undefined;
  if (native?.sha256) {
    try {
      return await native.sha256(input);
    } catch {
      /* fall through */
    }
  }

  const subtle = (globalThis as { crypto?: Crypto }).crypto?.subtle;
  if (subtle) {
    const digest = await subtle.digest('SHA-256', encoder.encode(input));
    return bufferToHex(digest);
  }

  return sha256Pure(input);
}

function bufferToHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Compact pure SHA-256 (for environments without WebCrypto). */
function sha256Pure(message: string): string {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  const bytes = utf8Bytes(message);
  const bitLen = bytes.length * 8;
  const withOne = bytes.concat([0x80]);
  while (withOne.length % 64 !== 56) withOne.push(0);
  const lenHi = Math.floor(bitLen / 0x100000000);
  const lenLo = bitLen >>> 0;
  for (let i = 3; i >= 0; i--) withOne.push((lenHi >>> (i * 8)) & 0xff);
  for (let i = 3; i >= 0; i--) withOne.push((lenLo >>> (i * 8)) & 0xff);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;

  const W = new Uint32Array(64);
  for (let i = 0; i < withOne.length; i += 64) {
    for (let t = 0; t < 16; t++) {
      const j = i + t * 4;
      W[t] =
        ((withOne[j]! << 24) | (withOne[j + 1]! << 16) | (withOne[j + 2]! << 8) | withOne[j + 3]!) >>> 0;
    }
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(W[t - 15]!, 7) ^ rotr(W[t - 15]!, 18) ^ (W[t - 15]! >>> 3);
      const s1 = rotr(W[t - 2]!, 17) ^ rotr(W[t - 2]!, 19) ^ (W[t - 2]! >>> 10);
      W[t] = (W[t - 16]! + s0 + W[t - 7]! + s1) >>> 0;
    }
    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;
    for (let t = 0; t < 64; t++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[t]! + W[t]!) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7].map((x) => x.toString(16).padStart(8, '0')).join('');
}

function rotr(n: number, x: number): number {
  return ((n >>> x) | (n << (32 - x))) >>> 0;
}

function utf8Bytes(s: string): number[] {
  const out: number[] = [];
  for (let i = 0; i < s.length; i++) {
    let c = s.charCodeAt(i);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) {
      out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
    } else if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(++i);
      c = 0x10000 + ((c & 0x3ff) << 10) + (next & 0x3ff);
      out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 0x3f), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    } else {
      out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
    }
  }
  return out;
}

function decodeBase64Loose(raw: string): string {
  let s = raw.replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4;
  if (pad) s += '='.repeat(4 - pad);

  const table =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const bytes: number[] = [];
  for (let i = 0; i < s.length; i += 4) {
    const c1 = table.indexOf(s[i]!);
    const c2 = table.indexOf(s[i + 1]!);
    const c3 = s[i + 2] === '=' ? -1 : table.indexOf(s[i + 2]!);
    const c4 = s[i + 3] === '=' ? -1 : table.indexOf(s[i + 3]!);
    if (c1 < 0 || c2 < 0) continue;
    bytes.push((c1 << 2) | (c2 >> 4));
    if (c3 >= 0) bytes.push(((c2 & 15) << 4) | (c3 >> 2));
    if (c4 >= 0) bytes.push(((c3 & 3) << 6) | c4);
  }

  try {
    if (typeof TextDecoder !== 'undefined') {
      return new TextDecoder('utf-8').decode(Uint8Array.from(bytes));
    }
  } catch {
    /* fall through */
  }
  let out = '';
  for (const b of bytes) out += String.fromCharCode(b);
  try {
    return decodeURIComponent(escape(out));
  } catch {
    return out;
  }
}

function safeDecodeURIComponent(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

function parseQuery(qs: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!qs) return out;
  for (const part of qs.split('&')) {
    if (!part) continue;
    const [k, ...rest] = part.split('=');
    if (!k) continue;
    out[safeDecodeURIComponent(k)] = safeDecodeURIComponent(rest.join('='));
  }
  return out;
}

function protocolOf(link: string): Protocol {
  const lower = link.toLowerCase();
  if (lower.startsWith('vless://')) return 'vless';
  if (lower.startsWith('vmess://')) return 'vmess';
  if (lower.startsWith('trojan://')) return 'trojan';
  if (lower.startsWith('ss://')) return 'ss';
  return 'unknown';
}

function buildTags(protocol: Protocol, transport: string, security: string): string[] {
  const tags: string[] = [];
  if (protocol !== 'unknown') tags.push(protocol.toUpperCase());
  if (security === 'reality') tags.push('Reality');
  else if (security === 'tls') tags.push('TLS');
  if (transport && transport !== 'tcp') tags.push(transport.toUpperCase());
  return tags;
}

function parseVmess(link: string): Omit<ParsedConfig, 'configHash' | 'tags'> | null {
  const payload = link.slice('vmess://'.length).trim();
  try {
    const json = JSON.parse(decodeBase64Loose(payload)) as Record<string, unknown>;
    const address = String(json.add ?? json.host ?? '').trim();
    const port = Number(json.port ?? 0);
    const uuid = String(json.id ?? '').trim();
    const path = String(json.path ?? '').trim();
    const sni = String(json.sni ?? json.host ?? json.peer ?? '').trim();
    const transport = String(json.net ?? json.type ?? 'tcp').toLowerCase();
    const security = String(json.tls ?? json.security ?? '').toLowerCase() || 'none';
    const remark = String(json.ps ?? json.remark ?? address).trim() || address;
    if (!address || !port || !uuid) return null;
    // Astra fingerprint: explicit header/SNI only (empty when absent — not server addr)
    const headerHost = String(
      json.host ?? json.sni ?? json.peer ?? json.authority ?? '',
    ).trim();
    return {
      raw: link,
      remark,
      protocol: 'vmess',
      transport,
      security: security === '1' || security === 'tls' ? 'tls' : security,
      address,
      port,
      uuid,
      path,
      sni: sni || headerHost,
      matchHost: headerHost,
    };
  } catch {
    return null;
  }
}

function parseUriStyle(link: string, protocol: Protocol): Omit<ParsedConfig, 'configHash' | 'tags'> | null {
  // vless://uuid@host:port?query#remark
  // trojan://password@host:port?query#remark
  // ss://base64 or ss://method:pass@host:port#remark
  try {
    if (protocol === 'ss' && !link.includes('@')) {
      const body = link.slice(5).split('#')[0] ?? '';
      const decoded = decodeBase64Loose(body);
      const rebuilt = `ss://${decoded}${link.includes('#') ? link.slice(link.indexOf('#')) : ''}`;
      return parseUriStyle(rebuilt, 'ss');
    }

    const hashIdx = link.indexOf('#');
    const remark = hashIdx >= 0 ? safeDecodeURIComponent(link.slice(hashIdx + 1)) : '';
    const noHash = hashIdx >= 0 ? link.slice(0, hashIdx) : link;
    const schemeIdx = noHash.indexOf('://');
    const rest = noHash.slice(schemeIdx + 3);
    const qIdx = rest.indexOf('?');
    const main = qIdx >= 0 ? rest.slice(0, qIdx) : rest;
    const query = qIdx >= 0 ? parseQuery(rest.slice(qIdx + 1)) : {};

    const at = main.lastIndexOf('@');
    if (at < 0) return null;
    const userInfo = main.slice(0, at);
    const hostPort = main.slice(at + 1);
    const colon = hostPort.lastIndexOf(':');
    if (colon < 0) return null;
    const address = hostPort.slice(0, colon).replace(/^\[|\]$/g, '');
    const port = Number(hostPort.slice(colon + 1));
    if (!address || !port) return null;

    const uuid = protocol === 'ss' ? userInfo : userInfo.split(':')[0] ?? userInfo;
    const path = (query.path || query.spx || '').trim();
    // Display SNI: prefer real sni/peer; Host header may differ (ws)
    const sni = (query.sni || query.peer || query.host || query.authority || '').trim();
    const transport = (query.type || query.net || 'tcp').toLowerCase();
    let security = (query.security || query.tls || '').toLowerCase();
    if (!security && (query.sni || query.fp)) security = 'tls';
    if (query.pbk || query.sid) security = 'reality';
    // Must match Astra extract_link_match_host order: host → sni → peer → authority
    const matchHost = (
      query.host ||
      query.sni ||
      query.peer ||
      query.authority ||
      ''
    ).trim();

    return {
      raw: link,
      remark: remark || address,
      protocol,
      transport,
      security: security || 'none',
      address,
      port,
      uuid,
      path,
      sni,
      matchHost,
    };
  } catch {
    return null;
  }
}

export function extractShareLinks(text: string): string[] {
  const re = /(?:vless|vmess|trojan|ss):\/\/[^\s<>"']+/gi;
  const found = text.match(re) ?? [];
  return [...new Set(found.map((l) => l.replace(/[,\];)]+$/, '')))];
}

function yieldUi(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** Ask Astra for the full probe feed (includes globally-hidden endpoints). */
function withProbeQuery(url: string): string {
  const raw = url.trim();
  try {
    const u = new URL(raw);
    if (!u.searchParams.has('probe')) u.searchParams.set('probe', '1');
    return u.toString();
  } catch {
    if (/[?&]probe=/i.test(raw)) return raw;
    return raw.includes('?') ? `${raw}&probe=1` : `${raw}?probe=1`;
  }
}

async function fetchOnce(url: string, timeoutMs: number, signal?: AbortSignal): Promise<string> {
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  signal?.addEventListener('abort', onAbort);
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(withProbeQuery(url), {
      signal: controller.signal,
      headers: {
        // Probe UA — server includes hidden configs for AstraPing only
        'User-Agent': 'AstraPing/1.2.7',
        Accept: 'text/plain,application/octet-stream,*/*',
        'X-Astra-Probe': '1',
        'Cache-Control': 'no-cache',
      },
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return await res.text();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

/** Large subs can be slow — long timeout + retries. */
export async function fetchSubscription(
  url: string,
  timeoutMs = 90000,
  signal?: AbortSignal,
): Promise<string> {
  let lastErr: unknown;
  for (let attempt = 1; attempt <= 3; attempt++) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    try {
      const body = await fetchOnce(url, timeoutMs, signal);
      if (!body || !body.trim()) {
        throw new Error('پاسخ ساب خالی بود');
      }
      return body;
    } catch (e) {
      lastErr = e;
      if (signal?.aborted) throw e;
      if (attempt < 3) await new Promise((r) => setTimeout(r, 600 * attempt));
    }
  }
  const msg =
    lastErr instanceof Error
      ? lastErr.name === 'AbortError'
        ? 'زمان دریافت ساب تمام شد — دوباره تلاش کنید'
        : lastErr.message
      : 'خطای شبکه';
  throw new Error(`دریافت ساب ناموفق: ${msg}`);
}

export async function parseSubscriptionBody(
  body: string,
  onProgress?: (done: number, total: number) => void,
): Promise<ParsedConfig[]> {
  const trimmed = body.trim().replace(/^\uFEFF/, '');
  let text = trimmed;
  const direct = extractShareLinks(trimmed);
  if (direct.length === 0) {
    try {
      // URL-safe / whitespace-stripped base64 payloads are common
      const compact = trimmed.replace(/\s+/g, '');
      text = decodeBase64Loose(compact);
    } catch {
      text = trimmed;
    }
  }
  const links = extractShareLinks(text);
  const out: ParsedConfig[] = [];
  const seen = new Set<string>();
  const total = links.length;

  for (let i = 0; i < links.length; i++) {
    const link = links[i]!;
    const protocol = protocolOf(link);
    const base =
      protocol === 'vmess'
        ? parseVmess(link)
        : protocol === 'unknown'
          ? null
          : parseUriStyle(link, protocol);
    if (!base) {
      if (i % 25 === 0) {
        onProgress?.(i + 1, total);
        await yieldUi();
      }
      continue;
    }

    const fingerprint = [
      base.address.toLowerCase(),
      String(base.port),
      base.uuid.toLowerCase(),
      base.path,
      base.sni.toLowerCase(),
    ].join(':');
    const configHash = await sha256Hex(fingerprint);
    if (!seen.has(configHash)) {
      seen.add(configHash);
      out.push({
        ...base,
        configHash,
        tags: buildTags(base.protocol, base.transport, base.security),
      });
    }
    if (i % 20 === 0 || i === links.length - 1) {
      onProgress?.(i + 1, total);
      await yieldUi();
    }
  }
  return out;
}

export async function loadConfigsFromSubUrl(
  url: string,
  opts?: {
    signal?: AbortSignal;
    onStatus?: (msg: string) => void;
  },
): Promise<ParsedConfig[]> {
  opts?.onStatus?.('دانلود ساب…');
  const body = await fetchSubscription(url, 90000, opts?.signal);
  opts?.onStatus?.('پارس کانفیگ‌ها…');
  return parseSubscriptionBody(body, (done, total) => {
    opts?.onStatus?.(`پارس ${done}/${total}`);
  });
}

export { sha256Hex };
