#!/usr/bin/env python3
"""
API 字段诊断工具 —— 打印各平台原始 JSON，自动标出 URL 字段
用法: python3 debug_apis.py [关键词]
"""

import json, sys, urllib.request, urllib.parse, urllib.error

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "五月天"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "http://qjjlb.quanjian.com.cn",
    "Referer": "http://qjjlb.quanjian.com.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
}

APIS = {
    "咪咕": f"https://api.xcvts.cn/api/music/migu?gm={urllib.parse.quote(KEYWORD)}&n=&num=3&type=json",
    "网易云": f"https://api.vkeys.cn/v2/music/netease?word={urllib.parse.quote(KEYWORD)}&page=1&num=3",
    "QQ音乐": f"https://tang.api.s01s.cn/music_open_api.php?msg={urllib.parse.quote(KEYWORD)}&type=json",
    "酷我": f"https://kw-api.cenguigui.cn/?name={urllib.parse.quote(KEYWORD)}&page=1&limit=3",
}


def looks_like_audio_url(v):
    if not isinstance(v, str) or not v.startswith("http"):
        return False
    lo = v.lower()
    return any(x in lo for x in (
        ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wav",
        "format=mp3", "format=flac", "type=song", "/music/", "/audio/"
    ))


def find_url_fields(obj, path=""):
    """递归找所有看起来是音频 URL 的字段，返回 [(路径, 值)]"""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else k
            if isinstance(v, str) and looks_like_audio_url(v):
                results.append((cur, v))
            elif isinstance(v, (dict, list)):
                results.extend(find_url_fields(v, cur))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):  # 只看前 3 项
            results.extend(find_url_fields(item, f"{path}[{i}]"))
    return results


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


for name, url in APIS.items():
    print(f"\n{'='*60}")
    print(f"  平台: {name}")
    print(f"  URL:  {url}")
    print(f"{'='*60}")
    try:
        data = fetch(url)

        # 打印顶层 keys
        if isinstance(data, dict):
            print(f"  顶层 keys: {list(data.keys())}")

        # 找 list 字段
        items = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("data", "list", "songs", "result", "music", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    items = v
                    print(f"  歌曲列表字段: data['{k}'] 共 {len(v)} 项")
                    break
                if isinstance(v, dict):
                    for k2 in ("list", "data", "songs"):
                        v2 = v.get(k2)
                        if isinstance(v2, list):
                            items = v2
                            print(f"  歌曲列表字段: data['{k}']['{k2}'] 共 {len(v2)} 项")
                            break
                    if items:
                        break

        # 打印第一首歌的所有字段
        if items:
            first = items[0]
            print(f"\n  第一首歌所有字段:")
            for k, v in first.items():
                disp = str(v)[:100] if not isinstance(v, (dict, list)) else json.dumps(v)[:100]
                print(f"    {k:25s} = {disp}")

            # 自动探测音频 URL 字段
            url_fields = find_url_fields({"items": items})
            if url_fields:
                print(f"\n  ★ 检测到音频 URL 字段:")
                seen = set()
                for path, val in url_fields:
                    key = path.split(".")[-1].split("[")[0]
                    if key not in seen:
                        seen.add(key)
                        print(f"    字段名: {key:25s}  示例: {val[:80]}")
            else:
                print(f"\n  ✗ 未检测到音频 URL 字段！（所有 URL 类字段为空或不存在）")
                print("    非空字符串字段扫描:")
                for k, v in first.items():
                    if isinstance(v, str) and v and not v.startswith("{"):
                        print(f"      {k:25s} = {v[:80]}")
        else:
            print("  ✗ 无法找到歌曲列表")
            print("  原始响应(前500字):", json.dumps(data, ensure_ascii=False)[:500])

    except Exception as e:
        print(f"  ✗ 请求失败: {e}")
