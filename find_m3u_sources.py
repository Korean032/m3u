#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import argparse
import os
import sys
import time
import urllib.parse
import re
import json

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'


def is_url(s: str) -> bool:
    return s.startswith('http://') or s.startswith('https://')


def parse_m3u_entries(text: str):
    """简单解析M3U内容，返回 [{meta, url}] 列表"""
    entries = []
    current_meta = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF'):
            current_meta = line
            continue
        if line.startswith('#'):
            # 跳过其它注释行或标签
            continue
        url = line
        entries.append({'meta': current_meta, 'url': url})
        current_meta = None
    return entries


def normalize_url(url: str) -> str:
    u = url.strip()
    if not u:
        return u
    u = u.replace('\u00a0', ' ')
    u = u.strip()
    parsed = urllib.parse.urlsplit(u)
    scheme = parsed.scheme.lower() or 'http'
    netloc = parsed.netloc.lower()
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe='/:._-')
    query = urllib.parse.quote_plus(urllib.parse.unquote_plus(parsed.query), safe='=&:_-.%')
    frag = ''
    return urllib.parse.urlunsplit((scheme, netloc, path, query, frag))


async def fetch_text(session, url: str, timeout: float, retries: int = 2):
    """GET文本（带重试），返回((text, final_url), None) 或 (None, error)"""
    attempt = 0
    last_err = None
    while attempt <= retries:
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    last_err = f'HTTP {resp.status}'
                else:
                    text = await resp.text(errors='ignore')
                    final_url = str(resp.url)
                    return (text, final_url), None
        except Exception as e:
            last_err = str(e)
        attempt += 1
        await asyncio.sleep(min(0.5 * attempt, 2.0))
    return None, (last_err or 'unknown error')


async def check_m3u8(session, url: str, timeout: float, strict_segment: bool, retries: int = 2):
    """校验m3u8可用性。
    严格模式：必须能拿到至少一个分片(200/206)才视为可用；
    非严格：仅清单有效也可视为可用。
    """
    result, err = await fetch_text(session, url, timeout, retries)
    if err or not result:
        return False, f'fetch fail: {err}'
    text, base_url = result
    if not text.strip().startswith('#EXTM3U'):
        return False, 'not m3u8 header'
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    candidate_segment = None
    candidate_nested = None
    for i, l in enumerate(lines):
        if l.startswith('#EXT-X-STREAM-INF'):
            # 变体清单：下一行通常是子playlist路径
            for j in range(i + 1, len(lines)):
                if not lines[j].startswith('#'):
                    candidate_nested = urllib.parse.urljoin(base_url, lines[j])
                    break
            if candidate_nested:
                break
        elif not l.startswith('#'):
            # 直接有分片或媒体文件
            if l.endswith('.ts') or '.ts?' in l or l.endswith('.aac') or l.endswith('.mp4') or '.mp4?' in l:
                candidate_segment = urllib.parse.urljoin(base_url, l)
                break
    if candidate_nested:
        try:
            async with session.get(candidate_nested, timeout=timeout) as resp2:
                if resp2.status != 200:
                    return False, f'nested HTTP {resp2.status}'
                text2 = await resp2.text(errors='ignore')
                base2 = str(resp2.url)
        except Exception as e:
            return False, f'nested fetch error: {e}'
        lines2 = [s.strip() for s in text2.splitlines() if s.strip()]
        seg = None
        for s in lines2:
            if s.startswith('#'):
                continue
            seg = urllib.parse.urljoin(base2, s)
            break
        if not seg:
            return (False if strict_segment else True), ('nested no segment' if strict_segment else 'playlist only')
        for attempt in range(retries + 1):
            try:
                async with session.get(seg, timeout=timeout, headers={'Range': 'bytes=0-2048'}) as seg_resp:
                    if seg_resp.status in (200, 206):
                        return True, 'ok'
                    else:
                        reason = f'segment HTTP {seg_resp.status}'
            except Exception as e:
                reason = f'segment error: {e}'
            await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))
        return False, reason
    if candidate_segment:
        for attempt in range(retries + 1):
            try:
                async with session.get(candidate_segment, timeout=timeout, headers={'Range': 'bytes=0-2048'}) as seg_resp:
                    if seg_resp.status in (200, 206):
                        return True, 'ok'
                    else:
                        reason = f'segment HTTP {seg_resp.status}'
            except Exception as e:
                reason = f'segment error: {e}'
            await asyncio.sleep(min(0.5 * (attempt + 1), 2.0))
        return False, reason
    # 没找到分片
    return (False if strict_segment else True), ('no segment' if strict_segment else 'playlist only')


async def check_direct(session, url: str, timeout: float):
    """校验直链流，可尝试HEAD后GET部分字节"""
    try:
        async with session.head(url, timeout=timeout) as resp:
            # 只要能响应就继续
            pass
    except Exception:
        pass
    try:
        async with session.get(url, timeout=timeout, headers={'Range': 'bytes=0-2048'}) as resp:
            if resp.status in (200, 206):
                try:
                    await resp.content.read(256)
                except Exception:
                    pass
                return True, 'ok'
            return False, f'HTTP {resp.status}'
    except Exception as e:
        return False, str(e)


async def probe_url(session, url: str, timeout: float, strict_segment: bool, retries: int):
    lower = url.lower()
    if lower.endswith('.m3u8') or 'm3u8' in lower:
        return await check_m3u8(session, url, timeout, strict_segment, retries)
    else:
        # 直链重试在 check_direct 内处理，保留接口一致性
        return await check_direct(session, url, timeout)


async def worker(global_sem, host_sems, session, entry, timeout: float, strict_segment: bool, retries: int):
    host = urllib.parse.urlsplit(entry['url']).netloc.lower()
    host_sem = host_sems.get(host)
    if host_sem:
        async with global_sem:
            async with host_sem:
                ok, reason = await probe_url(session, entry['url'], timeout, strict_segment, retries)
                return ok, reason
    else:
        async with global_sem:
            ok, reason = await probe_url(session, entry['url'], timeout, strict_segment, retries)
            return ok, reason


def read_inputs(paths_or_urls):
    """读取输入，既支持本地m3u/TXT，也支持单个URL"""
    candidates = []
    for item in paths_or_urls:
        if os.path.isfile(item):
            try:
                with open(item, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(item, 'r', encoding='latin-1') as f:
                    content = f.read()
            if item.lower().endswith('.m3u'):
                entries = parse_m3u_entries(content)
                candidates.extend(entries)
            else:
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    candidates.append({'meta': None, 'url': line})
        else:
            if is_url(item):
                candidates.append({'meta': None, 'url': item})
    return candidates


async def process(inputs, out_path: str, timeout: float, concurrency: int, strict_segment: bool, retries: int = 2, per_host_limit: int = 8, max_items: int = 0):
    import aiohttp  # 延迟导入以便未安装时给出提示
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    timeout_conf = aiohttp.ClientTimeout(total=timeout + 2, sock_connect=timeout, sock_read=timeout)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout_conf, connector=connector) as session:
        # 额外发现：GitHub搜索与站点抓取
        discovered = []
        # GitHub 搜索
        if DISCOVER_CONF.get('github_search'):
            qs = DISCOVER_CONF['github_search']
            token = DISCOVER_CONF.get('github_token')
            max_items = DISCOVER_CONF.get('github_max', 30)
            discovered.extend(await discover_github_m3u(session, qs, timeout, max_items, token))
        # 站点抓取
        if DISCOVER_CONF.get('crawl_urls'):
            discovered.extend(await crawl_pages_for_m3u_links(session, DISCOVER_CONF['crawl_urls'], timeout))

        # 展开：如果输入项本身是m3u URL，先拉取其中的条目
        expanded = []
        for candidate in inputs + [{'meta': None, 'url': u} for u in discovered]:
            url = candidate['url']
            if is_url(url) and url.lower().endswith('.m3u'):
                res, err = await fetch_text(session, url, timeout, retries=retries)
                if res:
                    text, _ = res
                    expanded.extend(parse_m3u_entries(text))
                else:
                    expanded.append(candidate)
            else:
                expanded.append(candidate)

        # 规范化URL并去重
        seen = set()
        normalized = []
        for e in expanded:
            nu = normalize_url(e['url'])
            if not nu:
                continue
            if nu in seen:
                continue
            seen.add(nu)
            normalized.append({'meta': e['meta'], 'url': nu})

        to_check = normalized[:max_items] if max_items and max_items > 0 else normalized
        # 并发控制（全局 + 每主机）
        global_sem = asyncio.Semaphore(concurrency)
        host_sems = {}
        # 每主机限制：若未指定或大于全局并发，则取全局并发
        per_host_limit = per_host_limit if per_host_limit > 0 else concurrency
        per_host_limit = min(per_host_limit, concurrency)
        hosts = {urllib.parse.urlsplit(e['url']).netloc.lower() for e in to_check}
        for h in hosts:
            host_sems[h] = asyncio.Semaphore(per_host_limit)

        tasks = [asyncio.create_task(worker(global_sem, host_sems, session, e, timeout, strict_segment, retries=retries)) for e in to_check]
        results = await asyncio.gather(*tasks)

    # 汇总并输出（稳定排序）
    ok_entries = []
    bad_entries = []
    for e, (ok, reason) in zip(to_check, results):
        if ok:
            ok_entries.append(e)
        else:
            bad_entries.append({'url': e['url'], 'reason': reason})

    def display_name(meta):
        if not meta:
            return 'Unknown'
        m = re.search(r',\s*(.+)$', meta)
        return m.group(1) if m else 'Unknown'

    ok_entries.sort(key=lambda e: (display_name(e['meta']), e['url']))

    out_dir = os.path.dirname(out_path) or '.'
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for e in ok_entries:
            meta = e['meta'] or '#EXTINF:-1,Unknown'
            f.write(f'{meta}\n{e["url"]}\n')

    # 不可用清单与统计报告
    with open(os.path.join(out_dir, 'unavailable.csv'), 'w', encoding='utf-8') as f:
        f.write('url,reason\n')
        for b in bad_entries:
            u = b['url'].replace('"', '""')
            r = (b['reason'] or '').replace('"', '""')
            f.write(f'"{u}","{r}"\n')

    report = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'checked': len(to_check),
        'available': len(ok_entries),
        'unavailable': len(bad_entries),
    }
    with open(os.path.join(out_dir, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return len(ok_entries), len(to_check)


def build_discovery(discover_flags):
    urls = []
    mapping = {
        'all': 'https://iptv-org.github.io/iptv/index.m3u',
        'cn': 'https://iptv-org.github.io/iptv/countries/cn.m3u',
        'us': 'https://iptv-org.github.io/iptv/countries/us.m3u',
        'sports': 'https://iptv-org.github.io/iptv/categories/sports.m3u',
        'news': 'https://iptv-org.github.io/iptv/categories/news.m3u',
    }
    for flag in discover_flags or []:
        if flag in mapping:
            urls.append(mapping[flag])
    return urls


async def discover_github_m3u(session, queries, timeout, max_items, token=None):
    """使用GitHub搜索API发现m3u原始文件链接，返回URL列表"""
    if isinstance(queries, str):
        queries = [queries]
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    urls = []
    per_page = 50
    for q in queries:
        q2 = f'(extension:m3u OR extension:m3u8) {q}'.strip()
        page = 1
        while len(urls) < max_items and page <= 5:
            api = f'https://api.github.com/search/code?q={urllib.parse.quote(q2)}&per_page={per_page}&page={page}'
            try:
                async with session.get(api, timeout=timeout, headers=headers) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
            except Exception:
                break
            items = data.get('items') or []
            if not items:
                break
            for it in items:
                if len(urls) >= max_items:
                    break
                # it['url'] -> contents API，包含 download_url
                content_api = it.get('url')
                if not content_api:
                    continue
                try:
                    async with session.get(content_api, timeout=timeout, headers=headers) as c:
                        if c.status != 200:
                            continue
                        content_meta = await c.json()
                except Exception:
                    continue
                dl = content_meta.get('download_url')
                if dl and dl.lower().endswith('.m3u'):
                    urls.append(dl)
            page += 1
    return urls


async def crawl_pages_for_m3u_links(session, pages, timeout):
    """抓取网页并提取 .m3u/.m3u8 链接，返回URL列表"""
    if isinstance(pages, str):
        pages = [pages]
    found = []
    for pg in pages:
        try:
            async with session.get(pg, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                html = await resp.text(errors='ignore')
                base = str(resp.url)
        except Exception:
            continue
        # 简单正则提取href/src中的链接
        for m in re.finditer(r'(href|src)=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
            link = m.group(2).strip()
            if not link:
                continue
            abs_link = urllib.parse.urljoin(base, link)
            low = abs_link.lower()
            if low.endswith('.m3u') or low.endswith('.m3u8') or '.m3u8?' in low or '.m3u?' in low:
                found.append(abs_link)
        # 额外扫描页面文本中的URL
        for m in re.finditer(r'https?://[^\s"\']+', html, flags=re.IGNORECASE):
            abs_link = m.group(0).strip()
            low = abs_link.lower()
            if low.endswith('.m3u') or low.endswith('.m3u8') or '.m3u8?' in low or '.m3u?' in low:
                found.append(abs_link)
    # 去重
    seen = set()
    result = []
    for u in found:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def main():
    parser = argparse.ArgumentParser(description='自动筛选可用的M3U直播源')
    parser.add_argument('--input', '-i', nargs='*', default=[], help='输入：本地M3U文件、URL或包含URL的TXT')
    parser.add_argument('--out', '-o', default='output/working.m3u', help='输出M3U路径')
    parser.add_argument('--timeout', '-t', type=float, default=6.0, help='每个源的超时时间(秒)')
    parser.add_argument('--concurrency', '-c', type=int, default=50, help='并发数量')
    parser.add_argument('--discover', nargs='*', default=[], help='可选：自动加入公共M3U（all, cn, us, sports, news）')
    parser.add_argument('--allow-playlist-only', action='store_true', help='放宽校验：允许仅清单有效（无分片）也写入')
    parser.add_argument('--github-search', nargs='*', default=[], help='GitHub搜索关键字，自动发现m3u文件（例如：iptv m3u）')
    parser.add_argument('--github-token', default=os.environ.get('GITHUB_TOKEN', ''), help='GitHub访问令牌（提升速率限制）')
    parser.add_argument('--crawl', nargs='*', default=[], help='站点种子页面URL列表，自动抓取页面内的 .m3u/.m3u8 链接')
    parser.add_argument('--interval-minutes', type=int, default=0, help='循环执行的间隔分钟数（例如 60 表示每小时运行一次）')
    parser.add_argument('--retries', type=int, default=2, help='HTTP请求重试次数')
    parser.add_argument('--per-host-limit', type=int, default=8, help='每主机并发限制（默认8，如果并发更小则取并发值）')
    parser.add_argument('--max-items', type=int, default=0, help='最大探测条目数（0表示不限制）')
    args = parser.parse_args()

    inputs = read_inputs(args.input)
    discover_urls = build_discovery(args.discover)
    for u in discover_urls:
        inputs.append({'meta': None, 'url': u})

    # 保存发现配置（在会话中使用）
    global DISCOVER_CONF
    DISCOVER_CONF = {
        'github_search': args.github_search if args.github_search else None,
        'github_token': args.github_token or None,
        'github_max': 30,
        'crawl_urls': args.crawl if args.crawl else None,
    }

    if not inputs:
        print('未提供输入源。示例：python find_m3u_sources.py -i seeds/urls.txt --discover cn')
        sys.exit(1)

    strict_segment = not args.allow_playlist_only

    def run_once():
        start = time.time()
        try:
            ok, total = asyncio.run(process(
                inputs,
                args.out,
                args.timeout,
                args.concurrency,
                strict_segment,
                retries=args.retries,
                per_host_limit=args.per_host_limit,
                max_items=args.max_items,
            ))
        except ModuleNotFoundError:
            print('缺少依赖：请先安装 aiohttp，例如：pip install -r requirements.txt')
            sys.exit(1)
        elapsed = time.time() - start
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f'[{ts}] 完成筛选：可用 {ok}/{total}，输出到 {args.out}，耗时 {elapsed:.1f}s')

    if args.interval_minutes and args.interval_minutes > 0:
        interval_sec = args.interval_minutes * 60
        print(f'进入循环模式：每 {args.interval_minutes} 分钟运行一次，按 Ctrl+C 退出')
        try:
            while True:
                run_once()
                time.sleep(interval_sec)
        except KeyboardInterrupt:
            print('已退出循环模式')
    else:
        run_once()


if __name__ == '__main__':
    main()