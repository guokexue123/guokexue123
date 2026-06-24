#!/usr/bin/env python3
"""
咪咕音乐歌曲下载脚本
API 来源: api.xcvts.cn
用法: python3 migu_downloader.py [歌手] [数量] [保存目录]
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error

API_BASE = "https://api.xcvts.cn/api/music/migu"

# 模拟浏览器请求头（与 F12 抓包一致）
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.6,en;q=0.5",
    "Origin": "http://qjjlb.quanjian.com.cn",
    "Referer": "http://qjjlb.quanjian.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) "
        "Gecko/20100101 Firefox/152.0"
    ),
}


def fetch_song_list(artist="", song="", num=10):
    """调用 API 获取歌曲列表，返回解析后的 JSON 数据"""
    params = urllib.parse.urlencode({
        "gm": artist,
        "n": song,
        "num": num,
        "type": "json",
    })
    url = f"{API_BASE}?{params}"
    print(f"[*] 请求列表: {url}")

    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            data = json.loads(raw)
            return data
    except urllib.error.HTTPError as e:
        print(f"[!] HTTP 错误 {e.code}: {e.reason}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[!] 网络错误: {e.reason}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[!] JSON 解析失败: {e}")
        sys.exit(1)


def extract_songs(data):
    """
    从 API 响应中提取歌曲列表。
    尝试常见的 key 名称（data / list / songs / result），兼容不同 API 版本。
    每首歌返回 dict: {name, singer, url}
    """
    # 打印原始结构供调试
    if isinstance(data, dict):
        print(f"[*] API 响应 keys: {list(data.keys())}")

    songs = []

    # 常见外层包装
    for key in ("data", "list", "songs", "result", "music"):
        if isinstance(data, dict) and key in data:
            inner = data[key]
            if isinstance(inner, list):
                songs = inner
                break
            # 有些 API 再嵌一层
            if isinstance(inner, dict):
                for k2 in ("data", "list", "songs"):
                    if k2 in inner and isinstance(inner[k2], list):
                        songs = inner[k2]
                        break

    # 顶层直接就是列表
    if not songs and isinstance(data, list):
        songs = data

    if not songs:
        print(f"[!] 无法识别歌曲列表结构，完整响应:\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        sys.exit(1)

    result = []
    for item in songs:
        # 尝试常见字段名
        name = item.get("name") or item.get("title") or item.get("songName") or "未知歌曲"
        singer = (
            item.get("singer") or item.get("artist") or
            item.get("singerName") or item.get("artistName") or "未知歌手"
        )
        url = (
            item.get("url") or item.get("mp3") or item.get("src") or
            item.get("playUrl") or item.get("download_url") or
            item.get("fileUrl") or ""
        )
        result.append({"name": name, "singer": singer, "url": url})

    return result


def sanitize_filename(name):
    """去掉文件名中的非法字符"""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


def download_song(song, save_dir, index, total):
    """下载单首歌曲到 save_dir"""
    name = sanitize_filename(song["name"])
    singer = sanitize_filename(song["singer"])
    url = song["url"]

    if not url:
        print(f"[{index}/{total}] 跳过（无下载链接）: {name} - {singer}")
        return False

    # 尝试从 URL 推断扩展名，默认 mp3
    ext = "mp3"
    path_part = urllib.parse.urlparse(url).path
    if "." in path_part.split("/")[-1]:
        ext = path_part.split("/")[-1].rsplit(".", 1)[-1].lower()
        if ext not in ("mp3", "flac", "m4a", "ogg", "aac", "wav"):
            ext = "mp3"

    filename = f"{singer} - {name}.{ext}"
    filepath = os.path.join(save_dir, filename)

    if os.path.exists(filepath):
        print(f"[{index}/{total}] 已存在，跳过: {filename}")
        return True

    print(f"[{index}/{total}] 下载: {filename}")
    print(f"       URL: {url}")

    req = urllib.request.Request(url, headers={**HEADERS, "Referer": url})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_size = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 65536  # 64 KB

            with open(filepath, "wb") as f:
                while True:
                    block = resp.read(chunk)
                    if not block:
                        break
                    f.write(block)
                    downloaded += len(block)
                    if total_size:
                        pct = downloaded / total_size * 100
                        print(f"\r       进度: {pct:.1f}% ({downloaded}/{total_size} B)", end="", flush=True)

            print()  # 换行
        print(f"       已保存: {filepath}")
        return True

    except urllib.error.HTTPError as e:
        print(f"\n[!] 下载失败 HTTP {e.code}: {filename}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return False
    except Exception as e:
        print(f"\n[!] 下载出错: {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="咪咕音乐歌曲下载器 (via api.xcvts.cn)"
    )
    parser.add_argument("artist", nargs="?", default="五月天", help="歌手名（默认：五月天）")
    parser.add_argument("-n", "--song", default="", help="歌曲名（可选，留空获取歌手列表）")
    parser.add_argument("-N", "--num", type=int, default=10, help="获取数量（默认 10）")
    parser.add_argument("-o", "--output", default="downloads", help="保存目录（默认 downloads）")
    parser.add_argument("--list-only", action="store_true", help="只列出歌曲，不下载")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    # 1. 获取歌曲列表
    data = fetch_song_list(artist=args.artist, song=args.song, num=args.num)
    songs = extract_songs(data)

    print(f"\n[*] 共找到 {len(songs)} 首歌曲:\n")
    for i, s in enumerate(songs, 1):
        flag = "✓" if s["url"] else "✗"
        print(f"  {i:02d}. [{flag}] {s['singer']} - {s['name']}")
    print()

    if args.list_only:
        return

    # 2. 逐首下载
    ok = fail = 0
    for i, song in enumerate(songs, 1):
        success = download_song(song, args.output, i, len(songs))
        if success:
            ok += 1
        else:
            fail += 1
        if i < len(songs):
            time.sleep(0.5)  # 避免请求过频

    print(f"\n[完成] 成功 {ok} 首，失败/跳过 {fail} 首，保存至: {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
