# pi-search

给 pi 的上网工具：`web_search` + `web_fetch`。软链到 `~/.pi/agent/extensions/search`。

## 为什么是"真浏览器"

纯 HTTP 抓搜索页会撞机器人检测，实测：

| 入口 | 纯 HTTP | 真浏览器 (headless Chromium) |
|---|---|---|
| Google | ❌ JS/consent 空壳 | ❌ 仍要求 consent（暂不支持） |
| Bing | ⚠️ 只对"热门簇"有效；多词 query 退化成首词词典页 | ✅ 正常 |
| Sogou | ⚠️ 间歇反爬（空壳） | ✅ 正常（中文） |
| DuckDuckGo / Baidu / Mojeek / Ecosia | ❌ anomaly / 验证 / captcha | — |
| SearXNG / Qwant / Dogpile | ❌ JSON 关 / JS 渲染 | — |
| 官方 API (Google CSE / Bing) | ❌ 需要 key（环境无 key） | — |

结论：**Bing 的"多词退化"就是机器人检测，真浏览器即可解决**。所以搜索走 headless Chromium。

## 实现要点

- `browser.ts`：懒加载。**顶层不 import `playwright-core`、不启动浏览器**，只有真正调用工具时才启动（所以不影响 pi 冷启动）。Chromium 用 `~/.cache/ms-playwright/` 里已缓存的 `chrome-headless-shell`，`playwright-core` 从本目录 `node_modules` 解析。
- `search.ts`：纯 node 的 HTTP 兜底 + 解析 + Firefox cookie 读取（`node:sqlite` 读 `cookies.sqlite`，snap/原生/flatpak 三种路径）。
- `web_search`：真浏览器优先（中文 → Sogou，其余 → Bing；失败互兜底），再退 HTTP。
- `web_fetch`：真浏览器渲染（`render:false` 可走快速 HTTP）。
- cookie：默认注入本地 Firefox session（`cookies:false` 关闭）。

## 用法

```
web_search(query, count?, site?, engine?="auto"|"bing"|"sogou", cookies?=true)
web_fetch(url, maxChars?, render?=true, cookies?=true)
```

## 已知限制

- Google 需要 consent，暂未接。
- `web_fetch` 对少数强反爬站点（如知乎）可能失败，会退 HTTP。
- Sogou 的 URL 是 `/link?url=` 跳转链接。
