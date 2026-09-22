# ZIP inspection helpers for Artiac Operations.
from io import BytesIO
import os
import time
import zipfile

MAX_PREVIEW_BYTES = 200_000
MAX_ARCHIVE_BYTES = int(os.getenv("PS_IAC_ARCHIVE_MAX_BYTES", str(50 * 1024 * 1024)))
MAX_FILES = int(os.getenv("PS_IAC_ARCHIVE_MAX_FILES", "2000"))
MAX_TOTAL_UNCOMPRESSED_BYTES = int(
    os.getenv("PS_IAC_ARCHIVE_MAX_UNCOMPRESSED_BYTES", str(250 * 1024 * 1024))
)
MAX_FILE_BYTES = int(os.getenv("PS_IAC_ARCHIVE_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_COMPRESSION_RATIO = float(os.getenv("PS_IAC_ARCHIVE_MAX_COMPRESSION_RATIO", "200"))
CACHE_TTL_SECONDS = int(os.getenv("PS_IAC_ARCHIVE_ZIP_CACHE_TTL", "300"))
_cache = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    if time.time() >= entry["expires_at"]:
        _cache.pop(key, None)
        return None
    return entry["files"]


def _cache_set(key: str, files: list[dict[str, object]]) -> None:
    _cache[key] = {
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "files": files,
    }


def validate_zip_archive(data: bytes) -> dict[str, int | float]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Archive exceeds the {MAX_ARCHIVE_BYTES}-byte compressed-size limit.")
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = [info for info in zf.infolist() if not info.is_dir()]
            if len(infos) > MAX_FILES:
                raise ValueError(f"Archive contains more than {MAX_FILES} files.")
            total = 0
            max_ratio = 0.0
            for info in infos:
                if info.file_size > MAX_FILE_BYTES:
                    raise ValueError(f"File '{info.filename}' exceeds the per-file size limit.")
                total += info.file_size
                if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ValueError("Archive exceeds the total uncompressed-size limit.")
                ratio = info.file_size / max(info.compress_size, 1)
                max_ratio = max(max_ratio, ratio)
                if ratio > MAX_COMPRESSION_RATIO:
                    raise ValueError(f"File '{info.filename}' has a suspicious compression ratio.")
            return {
                "compressed_bytes": len(data),
                "uncompressed_bytes": total,
                "file_count": len(infos),
                "max_compression_ratio": round(max_ratio, 2),
            }
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid ZIP archive.") from exc


def list_zip_files(data: bytes, cache_key: str | None = None) -> list[dict[str, object]]:
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return list(cached)

    validate_zip_archive(data)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        files = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            files.append({
                "name": name,
                "size": info.file_size,
                "compressed": info.compress_size,
                "crc": info.CRC,
                "ext": ext,
            })

    if cache_key:
        _cache_set(cache_key, list(files))

    return files


def read_zip_file(data: bytes, filename: str) -> str:
    validate_zip_archive(data)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        with zf.open(filename) as fh:
            raw = fh.read(MAX_PREVIEW_BYTES + 1)
            if len(raw) > MAX_PREVIEW_BYTES:
                raise ValueError("Preview too large; download the archive to view full file.")
            return raw.decode("utf-8", errors="replace")


def read_zip_file_bytes(data: bytes, filename: str) -> bytes:
    validate_zip_archive(data)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        with zf.open(filename) as fh:
            return fh.read()
