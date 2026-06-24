#!/usr/bin/env python3
"""
多平台音乐下载器 —— 皮卡丘音乐站同款 API
支持: 咪咕 / 网易云 / QQ音乐 / 酷我

用法:
  python3 music_downloader.py 五月天               # 全平台搜索+下载
  python3 music_downloader.py 五月天 -p netease    # 只用网易云
  python3 music_downloader.py 五月天 -N 20 -o ~/Music
  python3 music_downloader.py 五月天 --list-only   # 只列出，不下载
  python3 music_downloader.py 五月天 --debug       # 打印原始 JSON 用于诊断
"""

import os, sys, json, time, argparse, threading, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 全局配置 ─────────────────────────────────────────────────────────────────
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

DEBUG = False          # --debug 时置为 True
_lock = threading.Lock()


def log(*a, **k):
    with _lock:
        print(*a, **k)


def dbg(tag, data):
    if DEBUG:
        with _lock:
            print(f"\n[DEBUG:{tag}]")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])


# ── 平台配置 ─────────────────────────────────────────────────────────────────
PLATFORMS = {
    "migu": {
        "label": "咪咕",
        "search_url": "https://api.xcvts.cn/api/music/migu",
        "search_params": lambda kw, page, num: {"gm": kw, "n": "", "num": num, "type": "json"},
        # 如果列表里没有 URL，尝试此端点获取下载链接：传入 song_id
        "url_api": None,
    },
    "netease": {
        "label": "网易云",
        "search_url": "https://api.vkeys.cn/v2/music/netease",
        "search_params": lambda kw, page, num: {"word": kw, "page": page, "num": num},
        "url_api": None,
    },
    "qq": {
        "label": "QQ音乐",
        "search_url": "https://tang.api.s01s.cn/music_open_api.php",
        "search_params": lambda kw, page, num: {"msg": kw, "type": "json"},
        "url_api": None,
    },
    "kuwo": {
        "label": "酷我",
        "search_url": "https://kw-api.cenguigui.cn/",
        "search_params": lambda kw, page, num: {"name": kw, "page": page, "limit": num},
        "url_api": None,
    },
}


# ── URL 自动检测 ──────────────────────────────────────────────────────────────
# 优先检查的字段名（按常见程度排列）
_URL_FIELDS = [
    # 通用
    "url", "src", "link", "audio", "mp3", "flac",
    # 驼峰变体
    "playUrl", "musicUrl", "audioUrl", "fileUrl", "songUrl", "downUrl",
    "downloadUrl", "streamUrl", "listenUrl", "mp3Url", "flacUrl",
    # 下划线变体
    "play_url", "music_url", "audio_url", "file_url", "song_url",
    "down_url", "download_url", "stream_url", "listen_url",
    # 咪咕特有质量字段
    "sqUrl", "hqUrl", "freeUrl", "freeflacUrl", "normalUrl",
    "SQ", "HQ", "320", "128",
    # 其他
    "source", "path", "href", "media", "mediaUrl",
]


def _is_audio_url(v):
    """判断字符串是否像音频 URL（排除图片/歌词等误判）"""
    if not isinstance(v, str) or not v.startswith("http"):
        return False
    lo = v.lower()
    # 排除明显的图片/歌词域名或扩展名
    _EXCLUDE = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                "img.", "image.", "pic.", "cover.", "thumb.", "lrc.", ".lrc")
    if any(lo.endswith(x) or x in lo for x in _EXCLUDE):
        return False
    # 包含明确的音频扩展名
    if any(lo.endswith(f".{x}") or f".{x}?" in lo for x in
           ("mp3", "flac", "m4a", "ogg", "aac", "wav")):
        return True
    # 包含音频参数特征
    if any(x in lo for x in (
        "format=mp3", "format=flac", "type=song",
        "music.126.net",          # 网易云 CDN
        "freetyst.nf.migu.cn",    # 咪咕音乐 CDN
        "kw-api.cenguigui.cn",    # 酷我 API
        "dl.stream.qqmusic",      # QQ音乐 CDN
    )):
        return True
    return False


def find_audio_url(item):
    """
    多策略音频 URL 提取：
    1. 按优先列表检查已知字段名
    2. 扫描全部字段的字符串值
    3. 递归检查嵌套 dict
    """
    if not isinstance(item, dict):
        return ""

    # 策略1：已知字段名（含大小写变体）
    for field in _URL_FIELDS:
        for key in (field, field.lower(), field.upper()):
            v = item.get(key, "")
            if _is_audio_url(v):
                return v

    # 策略2：扫描全部字符串字段
    for k, v in item.items():
        if _is_audio_url(v):
            return v

    # 策略3：递归嵌套 dict
    for k, v in item.items():
        if isinstance(v, dict):
            found = find_audio_url(v)
            if found:
                return found

    return ""


def _field(item, *keys):
    """取第一个非空字段值"""
    for k in keys:
        v = item.get(k, "")
        if v and isinstance(v, str):
            return v.strip()
    return ""


# ── 响应解析 ──────────────────────────────────────────────────────────────────
def _to_song_list(data):
    """从任意层级的响应 JSON 中提取歌曲列表"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("data", "list", "songs", "result", "music", "items", "records"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("list", "data", "songs", "items"):
                v2 = v.get(k2)
                if isinstance(v2, list):
                    return v2
    return []


def parse_songs(data, platform_key):
    raw_list = _to_song_list(data)
    songs = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = _field(item,
                      "name", "title", "songName", "song_name", "songname",
                      "song", "musicName", "music_name")
        singer = _field(item,
                        "singer", "author", "artist", "singerName", "singer_name",
                        "artistName", "artist_name", "singers", "singerlist")
        url = find_audio_url(item)
        song_id = _field(item, "id", "songId", "song_id", "rid", "copyrightId", "cid", "musicId")
        cover = find_cover(item)

        if name:
            songs.append({
                "name": name, "singer": singer or "未知",
                "url": url, "id": song_id, "cover": cover,
                "_raw": item,          # 保留原始数据供后续处理
                "_platform": platform_key,
            })
    return songs


def find_cover(item):
    for f in ("pic", "cover", "picUrl", "pic_url", "coverUrl", "cover_url",
              "albumPic", "album_pic", "image", "thumb", "imgUrl", "img"):
        v = item.get(f, "")
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


# ── 网络请求 ──────────────────────────────────────────────────────────────────
def http_get(url, extra_headers=None, timeout=20):
    hdrs = {**HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def fetch_platform(platform_key, keyword, page=1, num=10):
    cfg = PLATFORMS[platform_key]
    params = cfg["search_params"](keyword, page, num)
    url = cfg["search_url"] + "?" + urllib.parse.urlencode(params)
    log(f"  [{cfg['label']}] GET {url}")
    try:
        raw, _ = http_get(url)
        data = json.loads(raw)
        dbg(f"{platform_key}/raw", data)
        songs = parse_songs(data, platform_key)
        no_url = sum(1 for s in songs if not s["url"])
        log(f"  [{cfg['label']}] 共 {len(songs)} 首，其中 {no_url} 首无直链")
        return platform_key, songs
    except Exception as e:
        log(f"  [{cfg['label']}] 失败: {e}")
        return platform_key, []


# ── 下载 ─────────────────────────────────────────────────────────────────────
def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "未知"


def guess_ext(url):
    lo = urllib.parse.urlparse(url).path.lower()
    for ext in ("flac", "mp3", "m4a", "ogg", "aac", "wav"):
        if lo.endswith(f".{ext}"):
            return ext
    if "flac" in url.lower():
        return "flac"
    return "mp3"


def download_song(song, label, save_dir, idx, total):
    name   = sanitize(song["name"])
    singer = sanitize(song["singer"])
    url    = song["url"]

    if not url:
        log(f"[{idx}/{total}] 无链接，跳过: {singer} - {name} [{label}]")
        return False

    ext      = guess_ext(url)
    filename = f"{singer} - {name} [{label}].{ext}"
    filepath = os.path.join(save_dir, filename)

    if os.path.exists(filepath):
        log(f"[{idx}/{total}] 已存在: {filename}")
        return True

    log(f"[{idx}/{total}] 下载: {filename}")
    log(f"       {url}")
    try:
        data, hdrs = http_get(url, timeout=60)
        with open(filepath, "wb") as f:
            f.write(data)
        log(f"       完成 ({len(data)/1024:.1f} KB) → {filepath}")
        return True
    except Exception as e:
        log(f"       失败: {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    global DEBUG
    parser = argparse.ArgumentParser(description="多平台音乐下载器")
    parser.add_argument("keyword", nargs="?", default="五月天")
    parser.add_argument("-p", "--platform",
                        choices=list(PLATFORMS.keys()) + ["all"], default="all")
    parser.add_argument("-N", "--num", type=int, default=10)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("-o", "--output", default="downloads")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--debug", action="store_true",
                        help="打印原始 API 响应 JSON（诊断用）")
    args = parser.parse_args()
    DEBUG = args.debug

    os.makedirs(args.output, exist_ok=True)
    targets = list(PLATFORMS) if args.platform == "all" else [args.platform]

    print(f"\n搜索「{args.keyword}」- 平台: {', '.join(targets)}\n")

    # ── 1. 并发查询 ───────────────────────────────────────────────────────────
    all_songs = []
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futs = {ex.submit(fetch_platform, pk, args.keyword, args.page, args.num): pk
                for pk in targets}
        for fut in as_completed(futs):
            pk, songs = fut.result()
            for s in songs:
                all_songs.append((pk, s))

    if not all_songs:
        print("[!] 所有平台均无结果。")
        sys.exit(1)

    # ── 2. 列出结果 ──────────────────────────────────────────────────────────
    print(f"\n共找到 {len(all_songs)} 首:\n")
    for i, (pk, s) in enumerate(all_songs, 1):
        label = PLATFORMS[pk]["label"]
        flag  = "✓" if s["url"] else "✗"
        print(f"  {i:3d}. [{flag}][{label}] {s['singer']} - {s['name']}")
        if DEBUG and not s["url"]:
            raw = s.get("_raw", {})
            non_empty = {k: v for k, v in raw.items()
                         if isinstance(v, str) and v and k not in ("name","singer","cover")}
            print(f"         可用字段: {non_empty}")
    print()

    if args.list_only:
        return

    # ── 3. 下载 ──────────────────────────────────────────────────────────────
    ok = fail = 0
    total = len(all_songs)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(download_song, s, PLATFORMS[pk]["label"],
                      args.output, i, total): i
            for i, (pk, s) in enumerate(all_songs, 1)
        }
        for fut in as_completed(futs):
            if fut.result():
                ok += 1
            else:
                fail += 1

    print(f"\n完成: 成功 {ok} 首，失败/跳过 {fail} 首")
    print(f"保存目录: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
