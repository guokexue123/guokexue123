#!/usr/bin/env python3
"""
多平台音乐下载器 — 皮卡丘音乐站同款 API
支持: 咪咕 / 网易云 / QQ音乐 / 酷我

用法:
  python3 music_downloader.py 五月天               # 搜索五月天，从全部平台下载
  python3 music_downloader.py 五月天 -p netease    # 只用网易云
  python3 music_downloader.py 五月天 -N 20 -o ~/Music
  python3 music_downloader.py 五月天 --list-only   # 只列出，不下载
"""

import os
import sys
import json
import time
import argparse
import threading
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 平台配置 ────────────────────────────────────────────────────────────────
PLATFORMS = {
    "migu": {
        "label": "咪咕",
        "url": "https://api.xcvts.cn/api/music/migu",
        "params": lambda kw, page, num: {
            "gm": kw, "n": "", "num": num, "type": "json"
        },
        # 解析器：把响应 JSON 转成 [{name, singer, url}, ...]
        "parse": "_parse_migu",
    },
    "netease": {
        "label": "网易云",
        "url": "https://api.vkeys.cn/v2/music/netease",
        "params": lambda kw, page, num: {
            "word": kw, "page": page, "num": num
        },
        "parse": "_parse_netease",
    },
    "qq": {
        "label": "QQ音乐",
        "url": "https://tang.api.s01s.cn/music_open_api.php",
        "params": lambda kw, page, num: {
            "msg": kw, "type": "json"
        },
        "parse": "_parse_generic",
    },
    "kuwo": {
        "label": "酷我",
        "url": "https://kw-api.cenguigui.cn/",
        "params": lambda kw, page, num: {
            "name": kw, "page": page, "limit": num
        },
        "parse": "_parse_generic",
    },
}

# 与浏览器 F12 抓包一致的请求头
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.6",
    "Origin": "http://qjjlb.quanjian.com.cn",
    "Referer": "http://qjjlb.quanjian.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    ),
}

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ── API 解析函数 ─────────────────────────────────────────────────────────────

def _to_list(obj, *keys):
    """递归找第一个 list 值"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                v = obj[k]
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    result = _to_list(v, *keys)
                    if result:
                        return result
    return []


def _item_field(item, *candidates):
    for k in candidates:
        if k in item and item[k]:
            return str(item[k]).strip()
    return ""


def _parse_migu(data):
    """
    咪咕: {"code":200,"data":[{"name","singer","url","pic",...}]}
    """
    songs = _to_list(data, "data", "list", "songs", "result")
    out = []
    for item in songs:
        out.append({
            "name":   _item_field(item, "name", "title", "songName"),
            "singer": _item_field(item, "singer", "artist", "singerName"),
            "url":    _item_field(item, "url", "mp3", "src", "playUrl", "fileUrl"),
            "cover":  _item_field(item, "pic", "cover", "picUrl", "album_pic"),
        })
    return out


def _parse_netease(data):
    """
    网易云: {"code":200,"data":{"list":[{"name","author","url",...}]}}
         或: {"code":200,"data":[...]}
    """
    songs = _to_list(data, "list", "data", "songs", "result")
    out = []
    for item in songs:
        out.append({
            "name":   _item_field(item, "name", "title", "songname", "song"),
            "singer": _item_field(item, "author", "singer", "artist", "artistName"),
            "url":    _item_field(item, "url", "mp3", "src", "playUrl", "music_url"),
            "cover":  _item_field(item, "pic", "picUrl", "cover", "image"),
        })
    return out


def _parse_generic(data):
    """
    通用解析：尝试所有常见 key 名称
    """
    songs = _to_list(data,
                     "data", "list", "songs", "result", "music",
                     "items", "records", "rows")
    out = []
    for item in songs:
        out.append({
            "name":   _item_field(item, "name", "title", "songname", "songName", "song"),
            "singer": _item_field(item, "singer", "author", "artist",
                                  "singerName", "artistName", "singer_name"),
            "url":    _item_field(item, "url", "mp3", "src", "playUrl",
                                  "download_url", "fileUrl", "music_url", "link"),
            "cover":  _item_field(item, "pic", "cover", "picUrl",
                                  "album_pic", "image", "thumb"),
        })
    return out


PARSERS = {
    "_parse_migu":    _parse_migu,
    "_parse_netease": _parse_netease,
    "_parse_generic": _parse_generic,
}


# ── 网络请求 ─────────────────────────────────────────────────────────────────

def http_get(url, extra_headers=None, timeout=15):
    hdrs = {**HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def fetch_platform(platform_key, keyword, page=1, num=10):
    """从单个平台获取歌曲列表，返回 (platform_key, [songs]) 或 (platform_key, None)"""
    cfg = PLATFORMS[platform_key]
    params = cfg["params"](keyword, page, num)
    url = cfg["url"] + "?" + urllib.parse.urlencode(params)
    safe_print(f"  [{cfg['label']}] GET {url}")
    try:
        raw, _ = http_get(url)
        data = json.loads(raw)
        songs = PARSERS[cfg["parse"]](data)
        # 过滤没有解析到名称的条目
        songs = [s for s in songs if s["name"]]
        safe_print(f"  [{cfg['label']}] 获取到 {len(songs)} 首")
        return platform_key, songs
    except Exception as e:
        safe_print(f"  [{cfg['label']}] 失败: {e}")
        return platform_key, None


# ── 下载 ─────────────────────────────────────────────────────────────────────

def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "未知"


def guess_ext(url):
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("flac", "mp3", "m4a", "ogg", "aac", "wav"):
        if path.endswith(f".{ext}"):
            return ext
    return "mp3"


def download_song(song, platform_label, save_dir, index, total):
    name    = sanitize(song["name"])
    singer  = sanitize(song["singer"])
    url     = song["url"]

    if not url:
        safe_print(f"[{index}/{total}] 无链接，跳过: {singer} - {name} [{platform_label}]")
        return False

    ext      = guess_ext(url)
    filename = f"{singer} - {name} [{platform_label}].{ext}"
    filepath = os.path.join(save_dir, filename)

    if os.path.exists(filepath):
        safe_print(f"[{index}/{total}] 已存在: {filename}")
        return True

    safe_print(f"[{index}/{total}] 下载: {filename}")
    safe_print(f"       {url}")

    try:
        raw, headers = http_get(url, timeout=60)
        total_bytes = int(headers.get("Content-Length", len(raw)))
        with open(filepath, "wb") as f:
            f.write(raw)
        safe_print(f"       完成 ({len(raw)/1024:.1f} KB) → {filepath}")
        return True
    except Exception as e:
        safe_print(f"       失败: {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="多平台音乐下载器（咪咕/网易云/QQ/酷我）"
    )
    parser.add_argument("keyword", nargs="?", default="五月天",
                        help="搜索关键词（歌手或歌名，默认：五月天）")
    parser.add_argument("-p", "--platform",
                        choices=list(PLATFORMS.keys()) + ["all"],
                        default="all",
                        help="平台: migu / netease / qq / kuwo / all（默认 all）")
    parser.add_argument("-N", "--num", type=int, default=10,
                        help="每个平台获取数量（默认 10）")
    parser.add_argument("--page", type=int, default=1, help="页码（默认 1）")
    parser.add_argument("-o", "--output", default="downloads",
                        help="保存目录（默认 downloads）")
    parser.add_argument("--list-only", action="store_true",
                        help="只列出歌曲，不下载")
    parser.add_argument("--workers", type=int, default=3,
                        help="并发下载线程数（默认 3）")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    target_platforms = (
        list(PLATFORMS.keys()) if args.platform == "all"
        else [args.platform]
    )

    # ── 1. 并发查询各平台 ──────────────────────────────────────────────────
    print(f"\n搜索「{args.keyword}」- 平台: {', '.join(target_platforms)}\n")
    all_songs = []  # [(platform_key, song_dict), ...]

    with ThreadPoolExecutor(max_workers=len(target_platforms)) as ex:
        futures = {
            ex.submit(fetch_platform, pk, args.keyword, args.page, args.num): pk
            for pk in target_platforms
        }
        for fut in as_completed(futures):
            pk, songs = fut.result()
            if songs:
                for s in songs:
                    all_songs.append((pk, s))

    if not all_songs:
        print("\n[!] 所有平台均未返回结果。")
        sys.exit(1)

    # ── 2. 列出结果 ────────────────────────────────────────────────────────
    print(f"\n共找到 {len(all_songs)} 首（含各平台）:\n")
    for i, (pk, s) in enumerate(all_songs, 1):
        label = PLATFORMS[pk]["label"]
        link  = "✓" if s["url"] else "✗"
        print(f"  {i:3d}. [{link}][{label:4s}] {s['singer']} - {s['name']}")
    print()

    if args.list_only:
        return

    # ── 3. 下载 ────────────────────────────────────────────────────────────
    ok = fail = 0
    total = len(all_songs)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                download_song, s, PLATFORMS[pk]["label"],
                args.output, i, total
            ): i
            for i, (pk, s) in enumerate(all_songs, 1)
        }
        for fut in as_completed(futures):
            if fut.result():
                ok += 1
            else:
                fail += 1

    print(f"\n完成: 成功 {ok} 首，失败/跳过 {fail} 首")
    print(f"保存目录: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
