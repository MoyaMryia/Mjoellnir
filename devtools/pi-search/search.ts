/**
 * web 搜索/抓取核心（纯 node，无 pi 依赖，可直接单测）。
 *
 * 搜索走 Bing 直连（cn.bing.com → www.bing.com 兜底）；Google/DDG 反爬不可用。
 * 可选带本地 Firefox 的 session cookie（从 cookies.sqlite 读，node:sqlite）。
 */

import * as fs from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";

const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0";
const HOSTS = ["https://cn.bing.com", "https://www.bing.com"];

export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

export function decodeEntities(s: string): string {
  return s
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#0?39;|&apos;/g, "'")
    .replace(/&ensp;|&#8195;|&#0183;|&nbsp;/g, " ")
    .replace(/&#x([0-9a-f]+);/gi, (_m, h: string) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_m, d: string) => String.fromCodePoint(Number(d)))
    .replace(/\s+/g, " ")
    .trim();
}

export function parseBing(html: string, limit: number): SearchResult[] {
  const blocks = html.split(/<li class="b_algo"/).slice(1);
  const out: SearchResult[] = [];
  for (const b of blocks) {
    const a = /<h2>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/.exec(b);
    if (!a) continue;
    const p =
      /<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>([\s\S]*?)<\/p>/.exec(b) ||
      /<div[^>]*class="[^"]*b_caption[^"]*"[^>]*>[\s\S]*?<p[^>]*>([\s\S]*?)<\/p>/.exec(b) ||
      /<p[^>]*>([\s\S]*?)<\/p>/.exec(b);
    out.push({
      title: decodeEntities(a[2]),
      url: a[1].replace(/&amp;/g, "&"),
      snippet: p ? decodeEntities(p[1]) : "",
    });
    if (out.length >= limit) break;
  }
  return out;
}

/** 解析 Sogou 搜索结果（中文可用；Baidu 有验证码）。相对跳转链接补成绝对地址。 */
export function parseSogou(html: string, limit: number): SearchResult[] {
  const parts = html.split(/<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>/i).slice(1);
  const out: SearchResult[] = [];
  for (const seg of parts) {
    const a = /<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/i.exec(seg);
    if (!a) continue;
    let url = a[1].replace(/&amp;/g, "&");
    if (url.startsWith("/")) url = `https://www.sogou.com${url}`;
    const title = decodeEntities(a[2]);
    if (!title) continue;
    const s =
      /<p[^>]*class="[^"]*(?:star-wiki|space-txt)[^"]*"[^>]*>([\s\S]*?)<\/p>/i.exec(seg) ||
      /<div[^>]*class="[^"]*text-layout[^"]*"[^>]*>([\s\S]*?)<\/div>/i.exec(seg);
    out.push({ title, url, snippet: s ? decodeEntities(s[1]) : "" });
    if (out.length >= limit) break;
  }
  return out;
}

/** 找到 Firefox 的 cookies.sqlite（snap / 原生 / flatpak 三种路径），取最新的一个。 */
export function findCookieDb(): string | undefined {
  const home = homedir();
  const bases = [
    path.join(home, "snap/firefox/common/.mozilla/firefox"),
    path.join(home, ".mozilla/firefox"),
    path.join(home, ".var/app/org.mozilla.firefox/.mozilla/firefox"),
  ];
  let best: { file: string; mtime: number } | undefined;
  for (const base of bases) {
    let profiles: string[];
    try {
      profiles = fs.readdirSync(base);
    } catch {
      continue;
    }
    for (const pr of profiles) {
      const f = path.join(base, pr, "cookies.sqlite");
      try {
        const st = fs.statSync(f);
        if (!best || st.mtimeMs > best.mtime) best = { file: f, mtime: st.mtimeMs };
      } catch {
        /* not a profile */
      }
    }
  }
  return best?.file;
}

export interface FirefoxCookie {
  name: string;
  value: string;
  domain: string;
  path: string;
  expires: number;
  secure: boolean;
  httpOnly: boolean;
}

/** 读出指定域名的 Firefox cookies（对象形式，供浏览器注入）。 */
export async function readCookies(domain: string): Promise<FirefoxCookie[]> {
  const file = findCookieDb();
  if (!file) return [];
  try {
    const { DatabaseSync } = (await import("node:sqlite")) as {
      DatabaseSync: new (p: string, o?: { readOnly?: boolean }) => {
        prepare: (sql: string) => {
          all: (...a: unknown[]) => Array<{ name: string; value: string; host: string; path: string; expiry: number; isSecure: number; isHttpOnly: number }>;
        };
        close: () => void;
      };
    };
    const db = new DatabaseSync(file, { readOnly: true });
    const rows = db
      .prepare("select name, value, host, path, expiry, isSecure, isHttpOnly from moz_cookies where host like ? and (expiry = 0 or expiry > ?)")
      .all(`%${domain}%`, Math.floor(Date.now() / 1000));
    db.close();
    return rows.map((r) => ({
      name: r.name,
      value: r.value,
      domain: r.host,
      path: r.path || "/",
      expires: r.expiry || 0,
      secure: !!r.isSecure,
      httpOnly: !!r.isHttpOnly,
    }));
  } catch {
    return [];
  }
}

/** 从 Firefox cookie 库拼出指定域名的 Cookie 头（失败返回 undefined，不影响搜索）。 */
export async function cookieHeaderFor(domain: string): Promise<string | undefined> {
  const rows = await readCookies(domain);
  if (!rows.length) return undefined;
  return rows.map((r) => `${r.name}=${r.value}`).join("; ");
}

export async function searchBing(
  query: string,
  opts: { count?: number; site?: string; cookies?: boolean } = {},
): Promise<{ results: SearchResult[]; error?: string; host?: string }> {
  const count = Math.max(1, Math.min(20, Math.floor(opts.count ?? 8)));
  const q = opts.site ? `${query} site:${opts.site}` : query;
  const headers: Record<string, string> = {
    "User-Agent": UA,
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
  };
  if (opts.cookies !== false) {
    const c = await cookieHeaderFor("bing.com");
    if (c) headers.Cookie = c;
  }

  let lastErr = "";
  for (const host of HOSTS) {
    const url = `${host}/search?q=${encodeURIComponent(q)}&count=${count}&setlang=zh-CN`;
    try {
      const res = await fetch(url, { headers, redirect: "follow" });
      if (!res.ok) {
        lastErr = `HTTP ${res.status} @ ${host}`;
        continue;
      }
      const html = await res.text();
      const results = parseBing(html, count);
      if (results.length) return { results, host };
      lastErr = `未解析到结果 @ ${host}（可能被反爬或改版）`;
    } catch (e) {
      lastErr = `fetch 失败 @ ${host}: ${String(e)}`;
    }
  }
  return { results: [], error: lastErr };
}

/** Sogou 搜索（中文可用）。 */
export async function searchSogou(
  query: string,
  opts: { count?: number; site?: string; cookies?: boolean } = {},
): Promise<{ results: SearchResult[]; error?: string; host?: string }> {
  const count = Math.max(1, Math.min(20, Math.floor(opts.count ?? 8)));
  const q = opts.site ? `${query} site:${opts.site}` : query;
  const headers: Record<string, string> = {
    "User-Agent": UA,
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
  };
  if (opts.cookies !== false) {
    const c = await cookieHeaderFor("sogou.com");
    if (c) headers.Cookie = c;
  }
  const host = "https://www.sogou.com";
  try {
    const res = await fetch(`${host}/web?query=${encodeURIComponent(q)}`, { headers, redirect: "follow" });
    if (!res.ok) return { results: [], error: `HTTP ${res.status} @ sogou`, host };
    const html = await res.text();
    const results = parseSogou(html, count);
    if (results.length) return { results, host };
    return { results: [], error: "Sogou 未解析到结果（可能触发验证）", host };
  } catch (e) {
    return { results: [], error: `fetch 失败 @ sogou: ${String(e)}`, host };
  }
}

export const HAS_CJK = /[\u3400-\u9fff\uf900-\ufaff]/;

/** Bing 找不到结果时会退化成"首个词的词典页"；识别并当作不可用。 */
const DICT_HOSTS = /(iciba\.com|baike\.baidu\.com|dictionary\.cambridge\.org|bing\.com\/dict|hanyuguoxue\.com|zdic\.net|cidian)/i;
const DICT_TITLE = /是什么意思|的翻译|释义|词典|读音|翻译_/;
function looksDegraded(results: SearchResult[]): boolean {
  if (!results.length) return true;
  const dict = results.filter((r) => DICT_HOSTS.test(r.url) || DICT_TITLE.test(r.title)).length;
  return dict >= Math.ceil(results.length / 2);
}

/**
 * 自动选引擎：中文 → Sogou（Bing 对 CJK 会退化），其余 → Bing；
 * 结果退化或为空时用另一个引擎兜底。
 */
export async function searchWeb(
  query: string,
  opts: { count?: number; site?: string; cookies?: boolean; engine?: "auto" | "bing" | "sogou" } = {},
): Promise<{ results: SearchResult[]; error?: string; engine?: string }> {
  const engine = opts.engine ?? "auto";
  const preferSogou = engine === "sogou" || (engine === "auto" && HAS_CJK.test(query));
  let order: Array<"bing" | "sogou"> = preferSogou ? ["sogou", "bing"] : ["bing", "sogou"];
  if (engine === "bing") order = ["bing"];
  if (engine === "sogou") order = ["sogou"];

  let fallback: { results: SearchResult[]; engine: string } | undefined;
  let lastErr = "";
  for (const e of order) {
    const r = e === "sogou" ? await searchSogou(query, opts) : await searchBing(query, opts);
    if (r.results.length) {
      if (!(e === "bing" && looksDegraded(r.results))) return { results: r.results, engine: e };
      fallback = fallback ?? { results: r.results, engine: e };
    }
    lastErr = r.error ?? lastErr;
  }
  if (fallback) return fallback;
  return { results: [], error: lastErr };
}

/** 抓取一个页面并转成纯文本（用于读搜索结果）。 */
export async function fetchPage(
  url: string,
  opts: { maxChars?: number; cookies?: boolean } = {},
): Promise<{ ok: boolean; text: string; title?: string; error?: string }> {
  const maxChars = Math.max(500, Math.min(50_000, opts.maxChars ?? 8000));
  const headers: Record<string, string> = {
    "User-Agent": UA,
    Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
  };
  if (opts.cookies !== false) {
    try {
      const dom = new URL(url).hostname.split(".").slice(-2).join(".");
      const c = await cookieHeaderFor(dom);
      if (c) headers.Cookie = c;
    } catch {
      /* ignore */
    }
  }
  try {
    const res = await fetch(url, { headers, redirect: "follow" });
    if (!res.ok) return { ok: false, text: "", error: `HTTP ${res.status}` };
    const html = await res.text();
    const t = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
    const text = decodeEntities(
      html.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " "),
    );
    return { ok: true, text: text.slice(0, maxChars), title: t ? decodeEntities(t[1]) : undefined };
  } catch (e) {
    return { ok: false, text: "", error: String(e) };
  }
}
