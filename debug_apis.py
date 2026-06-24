#!/usr/bin/env python3
"""
API 字段诊断工具 —— 打印各平台原始 JSON，并测试二次解析端点
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

SEARCH_APIS = {
    "咪咕":   f"https://api.xcvts.cn/api/music/migu?gm={urllib.parse.quote(KEYWORD)}&n=&num=3&type=json",
    "网易云":  f"https://api.vkeys.cn/v2/music/netease?word={urllib.parse.quote(KEYWORD)}&page=1&num=3",
    "QQ音乐": f"https://tang.api.s01s.cn/music_open_api.php?msg={urllib.parse.quote(KEYWORD)}&type=json",
    "酷我":   f"https://kw-api.cenguigui.cn/?name={urllib.parse.quote(KEYWORD)}&page=1&limit=3",
}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def looks_like_audio_url(v):
    if not isinstance(v, str) or not v.startswith("http"):
        return False
    lo = v.lower()
    if any(lo.endswith(x) or x in lo for x in (".jpg", ".jpeg", ".png", ".gif", ".webp", "img.", "type=lyr")):
        return False
    return any(x in lo for x in (".mp3", ".flac", ".m4a", "type=song", "format=mp3",
                                   "music.126.net", "freetyst.nf.migu.cn", "dl.stream.qqmusic"))


def find_url_fields(obj, path=""):
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{path}.{k}" if path else k
            if isinstance(v, str) and looks_like_audio_url(v):
                results.append((cur, v))
            elif isinstance(v, (dict, list)):
                results.extend(find_url_fields(v, cur))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            results.extend(find_url_fields(item, f"{path}[{i}]"))
    return results


def get_song_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "list", "songs", "result", "music"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for k2 in ("list", "data", "songs"):
                    v2 = v.get(k2)
                    if isinstance(v2, list):
                        return v2
    return []


# ── 第一步：搜索端点 ───────────────────────────────────────────────────────────
first_song_ids = {}   # platform → (first_song_name, first_song_id)
first_songs    = {}   # platform → first song dict

print(f"\n{'='*64}")
print(f"  搜索关键词: {KEYWORD}")
print(f"{'='*64}")

for name, url in SEARCH_APIS.items():
    print(f"\n{'='*60}")
    print(f"  平台: {name}")
    print(f"  URL:  {url}")
    print(f"{'='*60}")
    try:
        data = fetch(url)

        if isinstance(data, dict):
            print(f"  顶层 keys: {list(data.keys())}")

        items = get_song_list(data)
        if items:
            print(f"  歌曲数量: {len(items)}")
            first = items[0]
            print(f"\n  第一首所有字段:")
            for k, v in first.items():
                disp = str(v)[:100] if not isinstance(v, (dict, list)) else json.dumps(v)[:100]
                print(f"    {k:25s} = {disp}")
            first_songs[name] = first

            url_fields = find_url_fields({"items": items})
            if url_fields:
                seen = set()
                print(f"\n  ★ 音频 URL 字段:")
                for path, val in url_fields:
                    key = path.split(".")[-1].split("[")[0]
                    if key not in seen:
                        seen.add(key)
                        print(f"    {key:25s} = {val[:80]}")
            else:
                print(f"\n  ✗ 无音频 URL 字段")
        else:
            print("  ✗ 无歌曲列表")
            print("  原始:", json.dumps(data, ensure_ascii=False)[:300])

    except Exception as e:
        print(f"  ✗ 失败: {e}")


# ── 第二步：测试各平台的 URL 解析端点 ────────────────────────────────────────────
print(f"\n\n{'='*64}")
print("  二次解析端点探测")
print(f"{'='*64}")


def test_endpoints(label, endpoints):
    print(f"\n[{label}] 测试以下端点:")
    for ep in endpoints:
        print(f"  → {ep}")
        try:
            data = fetch(ep)
            top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            url_fields = find_url_fields(data)
            if url_fields:
                print(f"    ★ 找到音频字段:")
                for path, val in url_fields:
                    print(f"      {path} = {val[:80]}")
            else:
                print(f"    顶层 keys: {top_keys}")
                # 打印所有非空字段帮助判断
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, str) and v:
                            print(f"    {k:20s} = {str(v)[:100]}")
                        elif isinstance(v, dict):
                            print(f"    {k:20s} = {{dict: {list(v.keys())}}}")
                        elif isinstance(v, list) and v:
                            inner = v[0]
                            if isinstance(inner, dict):
                                print(f"    {k:20s} = [list[0]: {list(inner.keys())}]")
                print(f"    ✗ 无音频 URL")
        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code}: {e.reason}")
        except Exception as e:
            print(f"    错误: {e}")


# 咪咕：用歌名+歌手搜索（二次解析方式）
if "咪咕" in first_songs:
    s = first_songs["咪咕"]
    title  = s.get("title", "")
    singer = s.get("singer", "")
    kw_q   = urllib.parse.quote(KEYWORD)
    t_q    = urllib.parse.quote(title)
    sg_q   = urllib.parse.quote(singer)
    test_endpoints("咪咕 二次解析", [
        f"https://api.xcvts.cn/api/music/migu?gm={sg_q}&n={t_q}&num=1&type=json",
        f"https://api.xcvts.cn/api/music/migu?n={t_q}&type=json",
        f"https://api.xcvts.cn/api/music/migu?gm={sg_q}&n={t_q}&type=url",
        f"https://api.xcvts.cn/api/music?gm={sg_q}&n={t_q}&type=json",
    ])

# 网易云：用 id 换 URL
if "网易云" in first_songs:
    s   = first_songs["网易云"]
    sid = s.get("id", "")
    word = s.get("song", "")
    w_q  = urllib.parse.quote(word)
    test_endpoints("网易云 二次解析", [
        f"https://api.vkeys.cn/v2/music/netease?id={sid}",
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&type=json",
        f"https://api.vkeys.cn/v2/music/netease/url?id={sid}",
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&num=1",
        f"https://api.vkeys.cn/v1/music/netease?id={sid}",
        f"https://api.vkeys.cn/music/netease?id={sid}",
        f"https://api.vkeys.cn/v2/music/netease?word={w_q}&num=1",
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&quality=standard",
    ])

# QQ音乐：确认成功的端点
if "QQ音乐" in first_songs:
    s   = first_songs["QQ音乐"]
    mid = s.get("song_mid", "")
    test_endpoints("QQ音乐 二次解析（确认）", [
        f"https://tang.api.s01s.cn/music_open_api.php?id={mid}&type=json",
        f"https://tang.api.s01s.cn/music_open_api.php?song_mid={mid}&type=json",
    ])
