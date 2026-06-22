"""
HLS 下载器：并发下载 .ts 分片，ffmpeg 合并为 mp4。
ffmpeg 不可用时输出正确的 .ts 文件（而非伪装成 .mp4 的 MPEG-TS 流）。
"""

import asyncio
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import httpx
try:
    from tqdm.asyncio import tqdm
    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False

from config import (
    USER_AGENT, CONCURRENCY, SEG_RETRY, FFMPEG_PATH, DOWNLOAD_DIR
)


# ── ffmpeg 查找 ───────────────────────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    if FFMPEG_PATH:
        if Path(FFMPEG_PATH).exists():
            return FFMPEG_PATH
        print(f"  ⚠ FFMPEG_PATH 不存在: {FFMPEG_PATH}")
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    for p in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if Path(p).exists():
            return p
    return None


# ── m3u8 解析 ────────────────────────────────────────────────────────────────

def _parse_m3u8(playlist: str, base_url: str) -> tuple[list[str], str | None, str | None]:
    """
    解析 m3u8 文本，返回 (segment_urls, key_url, iv_hex)。
    仅处理媒体播放列表（#EXTINF）；主播放列表（#EXT-X-STREAM-INF）需调用者先解析。
    """
    segments: list[str] = []
    key_url: str | None = None
    iv_hex: str | None = None

    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY"):
            # 解析加密信息，例：METHOD=AES-128,URI="...",IV=0x...
            uri_m = __import__("re").search(r'URI="([^"]+)"', line)
            iv_m  = __import__("re").search(r'IV=0x([0-9a-fA-F]+)', line)
            if uri_m:
                u = uri_m.group(1)
                key_url = u if u.startswith("http") else urllib.parse.urljoin(base_url, u)
            if iv_m:
                iv_hex = iv_m.group(1)
        elif line and not line.startswith("#"):
            url = line if line.startswith("http") else urllib.parse.urljoin(base_url, line)
            segments.append(url)

    return segments, key_url, iv_hex


async def _resolve_m3u8(
    m3u8_url: str,
    headers: dict,
    client: httpx.AsyncClient,
) -> tuple[list[str], str | None, str | None, str]:
    """
    处理两级 m3u8（主播放列表 → 媒体播放列表）。
    返回 (segment_urls, key_url, iv_hex, base_url)。
    """
    # 本地缓存文件（playlist_cache.m3u8）
    if not m3u8_url.startswith("http"):
        text = Path(m3u8_url).read_text(encoding="utf-8")
        base = ""
    else:
        resp = await client.get(m3u8_url, headers=headers, timeout=30)
        resp.raise_for_status()
        text = resp.text
        base = m3u8_url.rsplit("/", 1)[0] + "/"

    # 判断是否为主播放列表（含 #EXT-X-STREAM-INF）
    if "#EXT-X-STREAM-INF" in text:
        # 选取最后（通常画质最高）的媒体播放列表
        media_url = None
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                media_url = line if line.startswith("http") else urllib.parse.urljoin(base, line)
        if not media_url:
            raise ValueError("主播放列表中未找到媒体流 URL")
        resp2 = await client.get(media_url, headers=headers, timeout=30)
        resp2.raise_for_status()
        text = resp2.text
        base = media_url.rsplit("/", 1)[0] + "/"

    segments, key_url, iv_hex = _parse_m3u8(text, base)
    return segments, key_url, iv_hex, base


# ── AES-128 解密（可选，需 pycryptodome 或 cryptography）─────────────────────

def _make_decryptor(key: bytes, iv: bytes):
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        return cipher.decryptor()
    except ImportError:
        pass
    raise RuntimeError(
        "流已加密（AES-128），需安装解密库：\n"
        "  pip install pycryptodome\n"
        "或\n"
        "  pip install cryptography"
    )


def _decrypt(data: bytes, decryptor) -> bytes:
    """兼容 pycryptodome 和 cryptography 的解密调用。"""
    if hasattr(decryptor, "decrypt"):
        return decryptor.decrypt(data)           # pycryptodome
    return decryptor.update(data) + decryptor.finalize()  # cryptography


# ── 并发分片下载 ──────────────────────────────────────────────────────────────

async def _download_segments(
    segments: list[str],
    headers: dict,
    tmp_dir: Path,
    key_bytes: bytes | None,
    iv_hex: str | None,
) -> bool:
    """
    并发下载所有分片到 tmp_dir/{index:06d}.ts，保留顺序。
    返回是否全部成功。
    """
    sem = asyncio.Semaphore(CONCURRENCY)
    failed: list[int] = []
    width = len(str(len(segments)))

    async def _fetch(idx: int, url: str):
        async with sem:
            seg_path = tmp_dir / f"{idx:06d}.ts"
            for attempt in range(SEG_RETRY):
                try:
                    async with httpx.AsyncClient(
                        headers=headers, timeout=30, follow_redirects=True
                    ) as c:
                        data = (await c.get(url)).content
                    if key_bytes:
                        # 每个分片用自增 IV（若未指定 IV，用分片索引）
                        if iv_hex:
                            iv = bytes.fromhex(iv_hex.zfill(32))
                        else:
                            iv = idx.to_bytes(16, "big")
                        dec = _make_decryptor(key_bytes, iv)
                        data = _decrypt(data, dec)
                    seg_path.write_bytes(data)
                    return
                except Exception as e:
                    if attempt == SEG_RETRY - 1:
                        failed.append(idx)
                    else:
                        await asyncio.sleep(1)

    tasks = [_fetch(i, url) for i, url in enumerate(segments)]

    if _HAS_TQDM:
        for coro in tqdm(
            asyncio.as_completed(tasks),
            total=len(tasks),
            desc="下载分片",
            unit="seg",
        ):
            await coro
    else:
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 20 == 0 or done == len(tasks):
                print(f"  ▶ {done}/{len(tasks)} ({done*100//len(tasks)}%)", end="\r")
        print()

    if failed:
        print(f"  ✗ {len(failed)} 个分片下载失败: {sorted(failed)[:10]}...")
        return False
    return True


# ── 合并分片 ──────────────────────────────────────────────────────────────────

def _merge_with_ffmpeg(tmp_dir: Path, output: Path, ffmpeg_bin: str) -> bool:
    """用 ffmpeg concat demuxer 合并为 mp4（正确的容器格式）。"""
    list_file = tmp_dir / "segments.txt"
    parts = sorted(tmp_dir.glob("*.ts"))
    if not parts:
        return False
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in parts),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",   # ADTS → LATM，mp4 容器必须
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg 错误:\n{result.stderr[-800:]}")
        return False
    return True


def _merge_binary(tmp_dir: Path, output_ts: Path) -> bool:
    """
    无 ffmpeg 时：按顺序二进制拼接 .ts 分片。
    输出仍为 MPEG-TS 格式，扩展名保持 .ts，可被 VLC / mpv 等播放。
    """
    parts = sorted(tmp_dir.glob("*.ts"))
    if not parts:
        return False
    with open(output_ts, "wb") as out:
        for p in parts:
            out.write(p.read_bytes())
    return True


# ── 公共接口 ──────────────────────────────────────────────────────────────────

async def download_async(
    m3u8_url: str,
    name: str,
    *,
    referer: str = "",
    user_agent: str = USER_AGENT,
) -> str:
    """
    下载 m3u8 流，返回输出文件路径字符串。
    ffmpeg 可用 → 输出 {name}.mp4（正确 MP4 容器）
    ffmpeg 不可用 → 输出 {name}.ts（MPEG-TS，可被 VLC/mpv 播放）
    """
    out_dir = Path(DOWNLOAD_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": user_agent,
        "Referer": referer or m3u8_url,
    }

    # ── 解析 m3u8，获取分片列表 ─────────────────────────────────────────────
    async with httpx.AsyncClient(
        headers=headers, timeout=30, follow_redirects=True
    ) as client:
        segments, key_url, iv_hex, base = await _resolve_m3u8(m3u8_url, headers, client)

        if not segments:
            raise RuntimeError("m3u8 中未找到任何分片")

        # 取加密 Key
        key_bytes: bytes | None = None
        if key_url:
            key_bytes = (await client.get(key_url, headers=headers, timeout=10)).content
            print(f"  ℹ 加密流（AES-128），将自动解密")

    print(f"  ▶ 解析 m3u8: {m3u8_url[:80]}...")
    print(f"  ✓ 共 {len(segments)} 个分片，并发数: {CONCURRENCY}")

    # ── 下载所有分片到临时目录 ───────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="hls_") as tmp_str:
        tmp_dir = Path(tmp_str)

        ok = await _download_segments(segments, headers, tmp_dir, key_bytes, iv_hex)
        if not ok:
            raise RuntimeError("部分分片下载失败，中止合并")

        # ── 合并 ─────────────────────────────────────────────────────────────
        ffmpeg = _find_ffmpeg()

        if ffmpeg:
            output = out_dir / f"{name}.mp4"
            print(f"\n  ▶ 合并分片 → {output}")
            if not _merge_with_ffmpeg(tmp_dir, output, ffmpeg):
                raise RuntimeError("ffmpeg 合并失败")
            print(f"  ✓ 合并完成（ffmpeg）: {output}")
        else:
            # 【修复】：输出 .ts 而非 .mp4
            # 二进制拼接的分片本质上是 MPEG-TS 容器，必须使用 .ts 扩展名。
            # 若命名为 .mp4 则容器格式与扩展名不符，大部分播放器将拒绝播放。
            # VLC / mpv / ffplay 均可正常播放 .ts 文件。
            output = out_dir / f"{name}.ts"
            print(f"\n  ▶ 合并分片 → {output}")
            print(f"  ⚠ ffmpeg 不可用，使用二进制拼接（输出 .ts 格式）...")
            print(f"    提示：安装 ffmpeg 可输出标准 mp4")
            print(f"    Linux: apt install ffmpeg  |  Windows: winget install ffmpeg")
            if not _merge_binary(tmp_dir, output):
                raise RuntimeError("二进制合并失败")
            print(f"  ✓ 合并完成（MPEG-TS）: {output}")
            print(f"  ℹ .ts 文件可用 VLC / mpv 直接播放")
            print(f"    转 mp4: ffmpeg -i \"{output}\" -c copy \"{output.with_suffix('.mp4')}\"")

    return str(output)
