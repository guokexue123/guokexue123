#!/usr/bin/env python3
"""
多平台音乐下载器 —— 皮卡丘音乐站同款 API
支持: 咪咕 / 网易云 / QQ音乐 / 酷我

用法:
  python3 music_downloader.py 五月天               # 全平台搜索+下载
  python3 music_downloader.py 五月天 -p netease    # 只用网易云
  python3 music_downloader.py 五月天 -p kuwo,qq   # 只用酷我+QQ（逗号分隔）
  python3 music_downloader.py 五月天 -N 20 -o ~/Music
  python3 music_downloader.py 五月天 --list-only   # 只列出，不下载
  python3 music_downloader.py 五月天 --debug       # 打印原始 JSON 用于诊断
"""

import os, sys, json, argparse, threading, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 全局 ─────────────────────────────────────────────────────────────────────
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
DEBUG = False
_lock = threading.Lock()

# ── 重复文件名保护 ─────────────────────────────────────────────────────────────
# 用于防止多线程并发下载同名文件时互相覆盖
_reserved_paths = set()
_reserved_lock  = threading.Lock()


def _claim_unique_path(save_dir, filename):
    """
    预占一个唯一文件路径：
    - 如果 filename 未被预占且磁盘上不存在 → 直接返回（含预占）
    - 否则自动添加 _2, _3 … 后缀直到找到空位
    """
    root, ext = os.path.splitext(filename)
    with _reserved_lock:
        n = 0
        while True:
            suffix = f"_{n + 1}" if n > 0 else ""
            candidate = os.path.join(save_dir, f"{root}{suffix}{ext}")
            if candidate not in _reserved_paths and not os.path.exists(candidate):
                _reserved_paths.add(candidate)
                return candidate
            n += 1


def log(*a, **k):
    with _lock:
        print(*a, **k)


def dbg(tag, data):
    if not DEBUG:
        return
    with _lock:
        text = (json.dumps(data, ensure_ascii=False, indent=2)
                if isinstance(data, (dict, list)) else str(data))
        print(f"\n[DEBUG:{tag}] {text[:2000]}")


# ── 平台配置 ─────────────────────────────────────────────────────────────────
#   url_support: 该平台的列表 API 是否能提供（或间接解析到）下载 URL
PLATFORMS = {
    "migu":    {"label": "咪咕",   "url_support": False},  # 仅返回元数据
    "netease": {"label": "网易云", "url_support": True},
    "qq":      {"label": "QQ音乐", "url_support": True},
    "kuwo":    {"label": "酷我",   "url_support": True},
}


# ── 网络工具 ─────────────────────────────────────────────────────────────────
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 有些端点返回 HTML，记录并抛出
            dbg("non-json", raw[:200].decode(errors="replace"))
            raise


# ── URL 检测 ─────────────────────────────────────────────────────────────────
_URL_FIELDS = [
    "url", "src", "link", "audio", "mp3", "flac",
    "playUrl", "musicUrl", "audioUrl", "fileUrl", "songUrl", "downUrl",
    "downloadUrl", "streamUrl", "listenUrl", "mp3Url", "flacUrl",
    "play_url", "music_url", "audio_url", "file_url", "song_url",
    "down_url", "download_url", "stream_url", "listen_url",
    "sqUrl", "hqUrl", "freeUrl", "freeflacUrl", "normalUrl",
    "SQ", "HQ", "320", "128",
    "source", "path", "href", "media", "mediaUrl",
]

_IMG_SIGNS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
              "img.", "image.", "pic.", "cover.", "thumb.", "lrc.", ".lrc",
              "type=lyr")

_AUDIO_CDNS = (
    "freetyst.nf.migu.cn",
    "music.126.net",
    "dl.stream.qqmusic",
)
_AUDIO_PARAMS = ("format=mp3", "format=flac", "type=song")
_AUDIO_EXTS   = ("mp3", "flac", "m4a", "ogg", "aac", "wav")


def _is_audio_url(v):
    if not isinstance(v, str) or not v.startswith("http"):
        return False
    lo = v.lower()
    if any(lo.endswith(x) or x in lo for x in _IMG_SIGNS):
        return False
    if any(lo.endswith(f".{e}") or f".{e}?" in lo for e in _AUDIO_EXTS):
        return True
    if any(p in lo for p in _AUDIO_PARAMS + _AUDIO_CDNS):
        return True
    return False


def find_audio_url(obj):
    """三层策略：已知字段 → 全字段扫描 → 递归嵌套 dict"""
    if not isinstance(obj, dict):
        return ""
    for f in _URL_FIELDS:
        for key in (f, f.lower(), f.upper()):
            if _is_audio_url(obj.get(key, "")):
                return obj[key]
    for v in obj.values():
        if _is_audio_url(v):
            return v
    for v in obj.values():
        if isinstance(v, dict):
            u = find_audio_url(v)
            if u:
                return u
    return ""


# ── 响应解析 ──────────────────────────────────────────────────────────────────
def _field(item, *keys):
    for k in keys:
        v = item.get(k, "")
        if v and isinstance(v, str):
            return v.strip()
    return ""


def _to_list(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in ("data", "list", "songs", "result", "music", "items", "records"):
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("list", "data", "songs", "items"):
                v2 = v.get(k2)
                if isinstance(v2, list):
                    return v2
    return []


def _item_to_song(item, platform_key):
    if not isinstance(item, dict):
        return None
    name = _field(item,
                  "name", "title", "song", "musicName",
                  "song_title", "songTitle",
                  "songName", "song_name", "songname", "music_name")
    singer = _field(item,
                    "singer", "author", "artist", "artists",
                    "singer_name", "singerName",
                    "artistName", "artist_name", "singers", "singerlist")
    song_id = _field(item,
                     "id", "rid",
                     "songId", "song_id",
                     "song_mid", "songMid",
                     "copyrightId", "cid", "musicId")
    url   = find_audio_url(item)
    cover = _field(item, "pic", "cover", "picUrl", "pic_url",
                   "coverUrl", "albumPic", "album_pic", "image", "thumb")
    if not name:
        return None
    return {
        "name": name, "singer": singer or "未知",
        "url": url, "id": song_id, "cover": cover,
        "_platform": platform_key,
        "_raw": item,
    }


def parse_response(data, platform_key):
    return [s for s in (_item_to_song(i, platform_key) for i in _to_list(data)) if s]


# ── 搜索 ─────────────────────────────────────────────────────────────────────
def search_migu(keyword, num):
    params = urllib.parse.urlencode({"gm": keyword, "n": "", "num": num, "type": "json"})
    data = http_get(f"https://api.xcvts.cn/api/music/migu?{params}")
    dbg("migu/search", data)
    return parse_response(data, "migu")


def search_netease(keyword, page, num):
    params = urllib.parse.urlencode({"word": keyword, "page": page, "num": num})
    data = http_get(f"https://api.vkeys.cn/v2/music/netease?{params}")
    dbg("netease/search", data)
    return parse_response(data, "netease")


def search_qq(keyword, num):
    params = urllib.parse.urlencode({"msg": keyword, "num": num, "type": "json"})
    data = http_get(f"https://tang.api.s01s.cn/music_open_api.php?{params}")
    dbg("qq/search", data)
    return parse_response(data, "qq")


def search_kuwo(keyword, page, num):
    params = urllib.parse.urlencode({"name": keyword, "page": page, "limit": num})
    data = http_get(f"https://kw-api.cenguigui.cn/?{params}")
    dbg("kuwo/search", data)
    return parse_response(data, "kuwo")


# ── URL 二次解析 ──────────────────────────────────────────────────────────────
def _scan_for_url(data):
    u = find_audio_url(data)
    if u:
        return u
    items = _to_list(data)
    if items:
        u = find_audio_url(items[0])
        if u:
            return u
    return ""


def resolve_migu_url(song):
    """
    咪咕 API (api.xcvts.cn) 的列表端点只返回 {title, singer, n}，
    即使用歌名+歌手精确搜索，响应结构完全相同，不含 URL 字段。
    经过多次探索确认该 API 不暴露下载链接，故直接返回空。
    """
    return ""


def resolve_netease_url(song):
    """
    网易云：用 song id 换取播放 URL。
    按成功概率从高到低尝试各端点。
    """
    sid = song.get("id", "")
    if not sid:
        return ""
    name_q = urllib.parse.quote(song.get("name", ""))
    candidates = [
        # 最可能成功的（id 直接换 URL）
        f"https://api.vkeys.cn/v2/music/netease?id={sid}",
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&type=json",
        # 尝试旧版路径
        f"https://api.vkeys.cn/v1/music/netease?id={sid}",
        f"https://api.vkeys.cn/music/netease?id={sid}",
        # 专用 URL 子路径
        f"https://api.vkeys.cn/v2/music/netease/url?id={sid}",
        f"https://api.vkeys.cn/v2/music/netease/play?id={sid}",
        # 用歌名+num=1 精确搜索（可能返回 URL）
        f"https://api.vkeys.cn/v2/music/netease?word={name_q}&num=1",
        # 带质量参数
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&quality=standard",
        f"https://api.vkeys.cn/v2/music/netease?id={sid}&br=320000",
    ]
    for ep in candidates:
        dbg("netease/resolve", ep)
        try:
            data = http_get(ep, timeout=15)
            dbg("netease/resolve/resp", data)
            u = _scan_for_url(data)
            if u:
                return u
        except Exception as e:
            dbg("netease/resolve/err", f"{ep} → {e}")
    return ""


def resolve_qq_url(song):
    """QQ音乐：用 song_mid（存为 id）换取播放 URL。"""
    mid = song.get("id", "")
    if not mid:
        return ""
    candidates = [
        f"https://tang.api.s01s.cn/music_open_api.php?id={mid}&type=json",
        f"https://tang.api.s01s.cn/music_open_api.php?song_mid={mid}&type=json",
        f"https://tang.api.s01s.cn/music_open_api.php?mid={mid}&type=json",
    ]
    for ep in candidates:
        dbg("qq/resolve", ep)
        try:
            data = http_get(ep, timeout=15)
            dbg("qq/resolve/resp", data)
            u = _scan_for_url(data)
            if u:
                return u
        except Exception as e:
            dbg("qq/resolve/err", str(e))
    return ""


URL_RESOLVERS = {
    "migu":    resolve_migu_url,   # 该 API 不提供下载 URL，始终返回空
    "netease": resolve_netease_url,
    "qq":      resolve_qq_url,
    "kuwo":    None,               # 列表已含 URL，无需二次解析
}


def resolve_song_url(song):
    """对无直链的歌曲调用平台解析器，原地更新 song['url']"""
    if song["url"]:
        return song
    pk = song["_platform"]
    label = PLATFORMS[pk]["label"]
    # 已知不支持的平台不打印解析失败，避免刷屏
    if not PLATFORMS[pk]["url_support"]:
        return song
    resolver = URL_RESOLVERS.get(pk)
    if resolver:
        u = resolver(song)
        if u:
            song["url"] = u
            log(f"    [解析成功] {song['singer']} - {song['name']} [{label}]")
        else:
            log(f"    [解析失败] {song['singer']} - {song['name']} [{label}]")
    return song


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
    return "flac" if "flac" in url.lower() else "mp3"


def download_song(song, save_dir, idx, total):
    label  = PLATFORMS[song["_platform"]]["label"]
    name   = sanitize(song["name"])
    singer = sanitize(song["singer"])
    url    = song["url"]

    if not url:
        # 只对 url_support=True 的平台打印跳过（咪咕静默跳过避免刷屏）
        if PLATFORMS[song["_platform"]]["url_support"]:
            log(f"[{idx}/{total}] 无链接，跳过: {singer} - {name} [{label}]")
        else:
            log(f"[{idx}/{total}] 跳过（{label}不支持下载）: {singer} - {name}")
        return False

    ext      = guess_ext(url)
    filename = f"{singer} - {name} [{label}].{ext}"

    # ── 重复文件名保护：预占唯一路径（防并发覆盖）──
    filepath = _claim_unique_path(save_dir, filename)
    is_dedup = os.path.basename(filepath) != filename  # 是否被改名

    # 如果文件刚好已存在（_claim_unique_path 检查过但这里再判断一次也无妨）
    if os.path.exists(filepath):
        log(f"[{idx}/{total}] 已存在: {os.path.basename(filepath)}")
        return True

    display_name = os.path.basename(filepath)
    if is_dedup:
        log(f"[{idx}/{total}] 下载（重名+后缀）: {display_name}")
    else:
        log(f"[{idx}/{total}] 下载: {display_name}")
    log(f"       {url}")

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with open(filepath, "wb") as f:
            f.write(data)
        log(f"       完成 ({len(data)/1024:.1f} KB) → {filepath}")
        return True
    except Exception as e:
        log(f"       失败: {e}")
        # 下载失败时释放预占
        with _reserved_lock:
            _reserved_paths.discard(filepath)
        if os.path.exists(filepath):
            os.remove(filepath)
        return False


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="多平台音乐下载器（咪咕/网易云/QQ/酷我）")
    ap.add_argument("keyword", nargs="?", default="五月天")
    ap.add_argument("-p", "--platform", default="all",
                    help="平台: migu/netease/qq/kuwo/all，逗号分隔多选（默认 all）")
    ap.add_argument("-N", "--num",  type=int, default=10, help="每平台结果数（默认10）")
    ap.add_argument("--page",       type=int, default=1)
    ap.add_argument("-o", "--output", default="downloads")
    ap.add_argument("--list-only",  action="store_true")
    ap.add_argument("--workers",    type=int, default=3, help="并发线程数（默认3）")
    ap.add_argument("--debug",      action="store_true", help="打印原始 API JSON")
    args = ap.parse_args()
    DEBUG = args.debug

    os.makedirs(args.output, exist_ok=True)

    # 解析平台参数（支持逗号分隔：-p kuwo,qq）
    if args.platform == "all":
        targets = list(PLATFORMS)
    else:
        targets = [p.strip() for p in args.platform.split(",") if p.strip() in PLATFORMS]
        if not targets:
            print(f"[!] 无效平台: {args.platform}，可选: {', '.join(PLATFORMS)}")
            sys.exit(1)

    print(f"\n搜索「{args.keyword}」- 平台: {', '.join(targets)}\n")

    # 提示咪咕不支持下载
    if "migu" in targets:
        print("  [提示] 咪咕平台仅提供歌曲元数据，该 API 不暴露下载链接，"
              "咪咕结果将被自动跳过。如只需下载，可用 -p kuwo,qq,netease\n")

    # ── 步骤1：并发搜索 ────────────────────────────────────────────────────
    def _search(pk):
        label = PLATFORMS[pk]["label"]
        log(f"  [{label}] 搜索中…")
        try:
            if pk == "migu":
                songs = search_migu(args.keyword, args.num)
            elif pk == "netease":
                songs = search_netease(args.keyword, args.page, args.num)
            elif pk == "qq":
                songs = search_qq(args.keyword, args.num)
            else:
                songs = search_kuwo(args.keyword, args.page, args.num)
            need = sum(1 for s in songs if not s["url"] and PLATFORMS[pk]["url_support"])
            log(f"  [{label}] {len(songs)} 首，{need} 首需二次解析")
            return pk, songs
        except Exception as e:
            log(f"  [{label}] 搜索失败: {e}")
            return pk, []

    all_songs = []
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        for pk, songs in ex.map(_search, targets):
            all_songs.extend(songs)

    if not all_songs:
        print("[!] 所有平台均无结果。")
        sys.exit(1)

    # ── 步骤2：并发解析无 URL 的歌曲 ─────────────────────────────────────
    need_resolve = [
        s for s in all_songs
        if not s["url"] and PLATFORMS[s["_platform"]]["url_support"]
    ]
    if need_resolve:
        print(f"\n[二次解析] 共 {len(need_resolve)} 首需要解析链接…")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(resolve_song_url, need_resolve))

    # ── 步骤3：显示结果列表 ───────────────────────────────────────────────
    print(f"\n共找到 {len(all_songs)} 首:\n")
    has_url = no_url_supported = no_url_unsupported = 0
    for i, s in enumerate(all_songs, 1):
        label   = PLATFORMS[s["_platform"]]["label"]
        support = PLATFORMS[s["_platform"]]["url_support"]
        if s["url"]:
            flag = "✓"
            has_url += 1
        elif support:
            flag = "✗"
            no_url_supported += 1
        else:
            flag = "—"        # 平台本身不支持
            no_url_unsupported += 1
        print(f"  {i:3d}. [{flag}][{label}] {s['singer']} - {s['name']}")
        if DEBUG and not s["url"] and support:
            raw = s.get("_raw", {})
            non_empty = {k: v for k, v in raw.items()
                         if isinstance(v, str) and v
                         and k not in ("name", "singer", "title", "song",
                                       "song_title", "singer_name", "cover", "pic")}
            if non_empty:
                print(f"         原始字段: {non_empty}")

    print(f"\n有效链接: {has_url}  "
          f"解析失败: {no_url_supported}  "
          f"平台不支持: {no_url_unsupported}")

    if args.list_only:
        return

    # ── 步骤4：并发下载 ───────────────────────────────────────────────────
    print()
    ok = fail = 0
    total = len(all_songs)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(download_song, s, args.output, i, total): i
            for i, s in enumerate(all_songs, 1)
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
