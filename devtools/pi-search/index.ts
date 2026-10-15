/**
 * 上网工具：`web_search`（Bing 直连，可选 Firefox session cookie）+ `web_fetch`（抓页面转文本）。
 *
 * 为什么是 Bing：Google 直连只回 JS/consent 空壳，DuckDuckGo 有反爬，官方 API 需要 key。
 * 轻量契约：工厂阶段只注册，不做任何 IO。
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { browserFetch, browserSearch } from "./browser.ts";
import { fetchPage, searchWeb } from "./search.ts";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    promptSnippet: "Search the web (Bing for English, Sogou for Chinese) and return titles, URLs and snippets.",
    description:
      "Search the web. Auto-selects the engine: Chinese queries → Sogou, otherwise Bing (Bing degrades on CJK). " +
      "Returns up to `count` results with title, URL and snippet. Optional `site` restricts to one domain. " +
      "Uses the local Firefox session cookies by default (set cookies:false to disable).",
    parameters: Type.Object({
      query: Type.String({ description: "Search query" }),
      count: Type.Optional(Type.Number({ description: "Max results (default 8, max 20)" })),
      site: Type.Optional(Type.String({ description: "Restrict to a domain, e.g. arxiv.org" })),
      engine: Type.Optional(
        Type.Union([Type.Literal("auto"), Type.Literal("bing"), Type.Literal("sogou")], {
          description: "Force an engine (default auto)",
        }),
      ),
      cookies: Type.Optional(Type.Boolean({ description: "Use Firefox session cookies (default true)" })),
    }),
    async execute(_id, params, _signal, onUpdate, _ctx) {
      const query = String(params.query ?? "").trim();
      if (!query) return { content: [{ type: "text", text: "query 不能为空。" }], isError: true };
      try {
        onUpdate?.({ content: [{ type: "text", text: `搜索：${query}（真浏览器）…` }] });
      } catch {
        /* ignore */
      }
      const opts = {
        count: params.count,
        site: params.site ? String(params.site) : undefined,
        engine: params.engine as "auto" | "bing" | "sogou" | undefined,
        cookies: params.cookies !== false,
      };
      let { results, engine, error } = await browserSearch(query, opts);
      if (!results.length) {
        const h = await searchWeb(query, opts); // HTTP 兜底
        if (h.results.length) {
          results = h.results;
          engine = h.engine;
          error = undefined;
        }
      }
      if (!results.length) {
        return { content: [{ type: "text", text: `没有结果。${error ?? ""}` }], isError: true };
      }
      const body = results
        .map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}${r.snippet ? `\n   ${r.snippet}` : ""}`)
        .join("\n\n");
      const tag = engine === "sogou" ? "Sogou" : "Bing";
      return {
        content: [{ type: "text", text: `${tag}「${query}」${results.length} 条结果：\n\n${body}` }],
      };
    },
  });

  pi.registerTool({
    name: "web_fetch",
    label: "Web Fetch",
    promptSnippet: "Fetch a URL and return its readable text (stripped HTML).",
    description:
      "Fetch a URL over HTTP and return its readable text (scripts/styles/tags stripped, truncated). Useful to read a page found via web_search.",
    parameters: Type.Object({
      url: Type.String({ description: "Absolute URL to fetch" }),
      maxChars: Type.Optional(Type.Number({ description: "Max characters to return (default 8000)" })),
      render: Type.Optional(Type.Boolean({ description: "Render with a real browser (default true; set false for a fast plain HTTP fetch)" })),
      cookies: Type.Optional(Type.Boolean({ description: "Send matching Firefox session cookies (default true)" })),
    }),
    async execute(_id, params, _signal, onUpdate, _ctx) {
      const url = String(params.url ?? "").trim();
      if (!/^https?:\/\//i.test(url)) {
        return { content: [{ type: "text", text: "url 必须是 http(s) 绝对地址。" }], isError: true };
      }
      try {
        onUpdate?.({ content: [{ type: "text", text: `抓取：${url}${params.render === false ? "（HTTP）" : "（真浏览器）"} …` }] });
      } catch {
        /* ignore */
      }
      const cookies = params.cookies !== false;
      let r: { ok: boolean; text: string; title?: string; error?: string } = { ok: false, text: "", error: "skipped" };
      if (params.render !== false) r = await browserFetch(url, { maxChars: params.maxChars, cookies });
      if (!r.ok) {
        const h = await fetchPage(url, { maxChars: params.maxChars, cookies }); // HTTP 兜底
        if (h.ok) r = h;
      }
      if (!r.ok) return { content: [{ type: "text", text: `抓取失败：${r.error}` }], isError: true };
      const head = r.title ? `# ${r.title}\n${url}\n\n` : `${url}\n\n`;
      return { content: [{ type: "text", text: head + r.text }] };
    },
  });
}
