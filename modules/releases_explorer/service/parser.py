# modules/releases_explorer/service/parser.py
import tarfile
import io
import os

MAX_ARCHIVE_BYTES = int(os.getenv("PS_RELEASES_MAX_BYTES", str(100 * 1024 * 1024)))
MAX_FILES = int(os.getenv("PS_RELEASES_MAX_FILES", "3000"))
MAX_TOTAL_UNCOMPRESSED_BYTES = int(
    os.getenv("PS_RELEASES_MAX_UNCOMPRESSED_BYTES", str(500 * 1024 * 1024))
)
MAX_FILE_BYTES = int(os.getenv("PS_RELEASES_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_COMPRESSION_RATIO = float(os.getenv("PS_RELEASES_MAX_COMPRESSION_RATIO", "200"))


def validate_release_archive(data: bytes) -> dict[str, int | float]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Release exceeds the {MAX_ARCHIVE_BYTES}-byte compressed-size limit.")
    count = 0
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                count += 1
                if count > MAX_FILES:
                    raise ValueError(f"Release contains more than {MAX_FILES} files.")
                if member.size > MAX_FILE_BYTES:
                    raise ValueError(f"File '{member.name}' exceeds the per-file size limit.")
                total += member.size
                if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ValueError("Release exceeds the total uncompressed-size limit.")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("Invalid TAR.GZ release archive.") from exc
    ratio = total / max(len(data), 1)
    if ratio > MAX_COMPRESSION_RATIO:
        raise ValueError("Release has a suspicious compression ratio.")
    return {
        "compressed_bytes": len(data),
        "uncompressed_bytes": total,
        "file_count": count,
        "compression_ratio": round(ratio, 2),
    }


def extract_files(data: bytes) -> dict[str, str]:
    """Extract YAML/text files from tar.gz, normalize paths."""
    validate_release_archive(data)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith((".yaml", ".yml", ".txt")):
                continue
            handle = archive.extractfile(member)
            if not handle:
                continue
            raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"File '{member.name}' exceeds the per-file size limit.")
            content = raw.decode("utf-8", errors="replace")
            fname = member.name.split("/")[-1].strip()
            if fname in out:
                fname = member.name.replace("/", "_")
            out[fname] = content

    return out
