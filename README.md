# 自动筛选可用的 M3U 直播源

一个并发探测脚本，自动从本地/远程 M3U 或 URL 列表中筛选可用的直播源，并输出一个新的 `working.m3u`。

## 快速开始

- 创建虚拟环境（可选）：
  - `python -m venv .venv`
  - `.venv\Scripts\activate`
- 安装依赖：`pip install -r requirements.txt`
- 运行示例（包含公共列表）：
  - `python find_m3u_sources.py -i seeds/urls.txt --discover cn -o output/working.m3u -c 50 -t 6`

## 使用说明

- 输入支持：
  - 本地 `.m3u` 文件（解析其中条目）
  - 本地 `.txt` 文件（每行一个URL）
  - 直接传入单个或多个 URL
- 可选 `--discover` 自动加入公共M3U：`all`, `cn`, `us`, `sports`, `news`
  - GitHub 搜索：`--github-search [关键词...]`（可选 `--github-token` 提升速率限制）
  - 站点抓取：`--crawl [页面URL...]` 自动解析页内 `.m3u/.m3u8` 链接
  - 定时循环：`--interval-minutes <分钟数>`（例如 `60` 表示每小时运行一次，Ctrl+C 退出）
  - 稳定性增强：`--retries <次数>`（默认2）、`--per-host-limit <并发>`（默认8，防止单站点限流）、`--max-items <数量>`（限制最大探测数量）
  - 仅直播：`--require-live` 只接受直播 HLS（自动过滤 VOD 与直链视频）

### 严格过滤（默认启用）

- 默认仅写入“可实际播放”的源：对于 `m3u8` 必须能成功请求到至少一个分片（返回 200/206）。
- 如需放宽为“清单有效即可写入”（即没有分片也写入），加上：`--allow-playlist-only`。

### 示例命令

- 仅使用公共CN列表：
  - `python find_m3u_sources.py --discover cn -o output/working.m3u`
- 使用自有种子列表 + 公共列表：
  - `python find_m3u_sources.py -i seeds/urls.txt --discover all -c 100 -t 5 -o output/working.m3u`
- 解析本地M3U并筛选：
  - `python find_m3u_sources.py -i mylist.m3u -o output/working.m3u`
 - 放宽为清单有效即可：
   - `python find_m3u_sources.py -i seeds/urls.txt --discover cn --allow-playlist-only -o output/working.m3u`
 - GitHub 搜索并筛选：
   - `python find_m3u_sources.py --github-search iptv m3u --discover cn -o output/working.m3u`
   - 搭配令牌（提高API速率）：`python find_m3u_sources.py --github-search iptv --github-token <你的token>`
 - 抓取站点页面的m3u/m3u8链接并筛选：
   - `python find_m3u_sources.py --crawl seeds/sites.txt -o output/working.m3u`
- 每小时自动运行一次（循环模式）：
   - `python find_m3u_sources.py --discover cn --interval-minutes 60 -o output/working.m3u`
- 加强稳定性（重试+主机并发限制）：
   - `python find_m3u_sources.py --discover cn --retries 2 --per-host-limit 8 -o output/working.m3u`
 - 仅保留中国直播源：
   - `python find_m3u_sources.py --discover cn --require-live -o output/working.m3u`
   - 如需扩大来源，可配合 GitHub 搜索中文关键字：
     - `python find_m3u_sources.py --github-search "iptv china 直播 m3u8" --require-live -o output/working.m3u`

## 参数

- `--input, -i`  输入源（本地M3U/TXT或URL），可多个
- `--out, -o`    输出M3U路径，默认 `output/working.m3u`
- `--timeout, -t` 每个源的超时时间（秒），默认 `6`
- `--concurrency, -c` 并发数量，默认 `50`
- `--discover`   可选公共M3U：`all`, `cn`, `us`, `sports`, `news`

## 工作原理（简述）

- 对 `m3u8`：拉取播放清单，优先解析变体清单并尝试获取一个分片（200/206即判定可用）；若无分片但清单有效，保守判定为可用。
- 对直链（如 `.ts/.mp4`）：尝试 `HEAD` 与部分字节 `GET Range`，返回 200/206 判定可用。
- 并发执行探测，超时与并发可调；将可用源写入 `#EXTM3U` 格式输出。

## 注意事项

- 不同源的合法性与版权归属各异，请自行遵循当地法律法规与平台条款。
- 公共列表的可用性波动较大；合理设置 `--timeout` 与 `--concurrency` 能提升效率。
- 如需代理（公司网络/地区限制），可在环境变量设置 `HTTP_PROXY`/`HTTPS_PROXY`。
- 输出仅做可用性筛选，不保证长期稳定；建议定期重新筛选。
- GitHub API搜索在未设置令牌时有较低速率限制；建议使用 `--github-token` 或设置环境变量 `GITHUB_TOKEN`。
 - 若希望系统级定时（无需脚本前台循环），可使用 Windows 任务计划程序：
   - 在“任务计划程序”创建基本任务，操作选择“启动程序”，程序/脚本填 `python`，参数填 `find_m3u_sources.py --discover cn -o output/working.m3u`，触发器选“每1小时”。

## 输出

- 成功后在 `output/working.m3u` 生成过滤后的可用源清单。
- 额外输出：`output/unavailable.csv`（不可用源与原因）、`output/report.json`（统计报告：总数、可用数、不可用数、时间戳）。