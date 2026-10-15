/**
 * headless Chromium 搜索/抓取。
 *
 * 为什么不用 HTTP 抓取：Bing/Google 对非浏览器请求会做机器人检测（Bing 直接退化成
 * “首词词典页”，Google 只回 consent 空壳）。真浏览器 + 注入 Firefox session cookie 才拿得到真结果。
 *
 * 懒加载契约：本模块顶层不 import playwright-core，也不启动浏览器；只有真正调用工具时才启动。
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { HAS_CJK, readCookies, type SearchResult } from "./search.ts";

const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

function discoverChromium(): string | undefined {
  const base = path.join(os.homedir(), ".cache", "ms-playwright");
  let dirs: string[];
  try {
    dirs = fs.readdirSync(base);
  } catch {
    return undefined;
  }
  const byRev = (a: string, b: string) => parseInt(b.split("-")[1] ?? "0", 10) - parseInt(a.split("-")[1] ?? "0", 10);
  for (const d of dirs.filter((x) => /^chromium_headless_shell-\d+$/.test(x)).sort(byRev)) {
    const p = path.join(base, d, "chrome-headless-shell-linux64", "chrome-headless-shell");
    if (fs.existsSync(p)) return p;
  }
  for (const d of dirs.filter((x) => /^chromium-\d+$/.test(x)).sort(byRev)) {
    const p = path.join(base, d, "chrome-linux64", "chrome");
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

let browserPromise: Promise<any> | undefined;

async function getBrowser(): Promise<any> {
  if (!browserPromise) {
    browserPromise = (async () => {
      const pw: any = await import("playwright-core");
      const chromium = pw.chromium ?? pw.default?.chromium;
      return chromium.launch({
        executablePath: discoverChromium(),
        headless: true,
        args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
      });
    })();
    browserPromise.catch(() => {
      browserPromise = undefined; // 允许下次重试
    });
  }
  return browserPromise;
}

/** 进程退出时收掉浏览器，避免残留。 */
let hookInstalled = false;
function installExitHook(): void {
  if (hookInstalled) return;
  hookInstalled = true;
  const kill = () => {
    browserPromise?.then((b) => b?.close?.()).catch(() => {});
  };
  process.once("exit", kill);
}

export type Engine = "bing" | "sogou";

async function newContext(browser: any, domain: string, useCookies: boolean): Promise<any> {
  const ctx = await browser.newContext({ locale: "zh-CN", userAgent: UA });
  if (useCookies) {
    const cookies = await readCookies(domain);
    if (cookies.length) {
      const normalized = cookies.map((c) => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        expires: c.expires || -1,
        secure: c.secure,
        httpOnly: c.httpOnly,
      }));
      try {
        await ctx.addCookies(normalized);
      } catch {
        /* 个别 cookie 非法时忽略 */
      }
    }
  }
  return ctx;
}

async function extractBing(page: any, count: number): Promise<SearchResult[]> {
  await page.waitForSelector("li.b_algo", { timeout: 12000 }).catch(() => {});
  return page.$$eval(
    "li.b_algo",
    (els: any[], n: number) =>
      els.slice(0, n).map((el) => {
        const a = el.querySelector("h2 a");
        const sn = el.querySelector(".b_caption p, .b_lineclamp, p");
        return { title: (a?.innerText || "").trim(), url: a?.href || "", snippet: (sn?.innerText || "").trim() };
      }),
    count,
  );
}

async function extractSogou(page: any, count: number): Promise<SearchResult[]> {
  await page.waitForSelector(".vrwrap h3 a, h3.vr-title a", { timeout: 12000 }).catch(() => {});
  return page.$$eval(
    ".vrwrap",
    (els: any[], n: number) =>
      els
        .slice(0, n)
        .map((el) => {
          const a = el.querySelector("h3.vr-title a, h3 a");
          const sn = el.querySelector(".star-wiki, .space-txt, .text-layout");
          return { title: (a?.innerText || "").trim(), url: a?.href || "", snippet: (sn?.innerText || "").trim() };
        })
        .filter((r) => r.title && r.url),
    count,
  );
}

const BAD_URL = /bing\.com\/dict|iciba\.com|dictionary\.cambridge\.org|baike\.baidu\.com|hanyuguoxue\.com|zdic\.net/i;

/** 用真浏览器搜索；auto 时中文走 Sogou、其余走 Bing，失败互相兜底。 */
export async function browserSearch(
  query: string,
  opts: { count?: number; site?: string; cookies?: boolean; engine?: "auto" | Engine } = {},
): Promise<{ results: SearchResult[]; engine?: string; error?: string }> {
  const count = Math.max(1, Math.min(20, Math.floor(opts.count ?? 8)));
  const q = opts.site ? `${query} site:${opts.site}` : query;
  const engine = opts.engine ?? "auto";
  const preferSogou = engine === "sogou" || (engine === "auto" && HAS_CJK.test(query));
  let order: Engine[] = preferSogou ? ["sogou", "bing"] : ["bing", "sogou"];
  if (engine === "bing") order = ["bing"];
  if (engine === "sogou") order = ["sogou"];
  const useCookies = opts.cookies !== false;

  let browser: any;
  try {
    browser = await getBrowser();
    installExitHook();
  } catch (e) {
    return { results: [], error: `浏览器启动失败: ${String(e).slice(0, 160)}` };
  }

  let lastErr = "";
  for (const e of order) {
    const domain = e === "bing" ? "bing.com" : "sogou.com";
    let ctx: any;
    try {
      ctx = await newContext(browser, domain, useCookies);
      const page = await ctx.newPage();
      const url =
        e === "bing"
          ? `https://cn.bing.com/search?q=${encodeURIComponent(q)}&form=QBLH`
          : `https://www.sogou.com/web?query=${encodeURIComponent(q)}`;
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 25000 });
      await page.waitForTimeout(700);
      const raw: SearchResult[] = e === "bing" ? await extractBing(page, count) : await extractSogou(page, count);
      const clean = raw.filter((r) => r.title && r.url && !BAD_URL.test(r.url));
      if (clean.length) return { results: clean.slice(0, count), engine: e };
      lastErr = `${e} 无有效结果`;
    } catch (err) {
      lastErr = `${e} 失败: ${String(err).slice(0, 140)}`;
    } finally {
      await ctx?.close?.().catch?.(() => {});
    }
  }
  return { results: [], error: lastErr };
}

/** 用真浏览器抓页面（渲染 JS）并返回正文文本。 */
export async function browserFetch(
  url: string,
  opts: { maxChars?: number; cookies?: boolean } = {},
): Promise<{ ok: boolean; text: string; title?: string; error?: string }> {
  const maxChars = Math.max(500, Math.min(50_000, opts.maxChars ?? 8000));
  const useCookies = opts.cookies !== false;
  let browser: any;
  try {
    browser = await getBrowser();
    installExitHook();
  } catch (e) {
    return { ok: false, text: "", error: `浏览器启动失败: ${String(e).slice(0, 160)}` };
  }
  let ctx: any;
  try {
    let domain = "";
    try {
      domain = new URL(url).hostname.split(".").slice(-2).join(".");
    } catch {
      /* ignore */
    }
    ctx = await newContext(browser, domain, useCookies);
    const page = await ctx.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 25000 });
    await page.waitForTimeout(500);
    const title = await page.title();
    const text: string = await page.evaluate(() => (document.body ? document.body.innerText : ""));
    return { ok: true, text: (text || "").slice(0, maxChars), title };
  } catch (e) {
    return { ok: false, text: "", error: String(e).slice(0, 160) };
  } finally {
    await ctx?.close?.().catch?.(() => {});
  }
}
