"""Read a bounded module ZIP into draft text files without extracting to disk."""
import io
import stat
import zipfile
import zlib
from pathlib import PurePosixPath
from . import authoring


def import_zip(raw):
    if len(raw) > 2_500_000:
        raise ValueError('The ZIP must be no larger than 2.5 MB.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise ValueError('The ZIP contains too many entries (maximum 1,000).')
            files, skipped = {}, []
            total = 0
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or '\x00' in name or ':' in name:
                    raise ValueError('The ZIP contains an unsafe file path.')
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError('Symbolic links are not supported in module uploads.')
                if entry.is_dir():
                    continue
                if any(p in ('.git', '.terraform', '.github', '__MACOSX') for p in path.parts) or path.name in ('.DS_Store', '.env', 'praxis-source.json', 'PRAXIS-IMPORT-NOTES.txt') or path.name.startswith('.env.') or '.tfstate' in path.name or path.name.endswith(('.tfvars', '.tfvars.json', '.tfplan')):
                    skipped.append(name)
                    continue
                if entry.flag_bits & 1:
                    raise ValueError('Password-protected ZIP files are not supported.')
                total += entry.file_size
                if total > 2_000_000 or len(files) >= 200:
                    raise ValueError('A module supports up to 200 text files and 2 MB of uncompressed content.')
                canonical = path.as_posix()
                if canonical in files:
                    raise ValueError('The ZIP contains duplicate file paths.')
                content = archive.read(entry).decode('utf-8-sig')
                if '\x00' in content:
                    raise ValueError('Only UTF-8 text files are supported; remove binary files from the ZIP.')
                files[canonical] = content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, UnicodeDecodeError, zlib.error, OSError) as exc:
        raise ValueError('Could not read this ZIP. Use an unencrypted ZIP containing UTF-8 text files.') from exc
    # GitHub/repository ZIPs commonly wrap the module in one directory.
    while files and all('/' in name for name in files) and len({name.split('/')[0] for name in files}) == 1:
        files = {name.split('/', 1)[1]: value for name, value in files.items()}
    if not any('/' not in name and name.endswith(('.tf', '.tf.json')) for name in files):
        raise ValueError('The ZIP must contain a Terraform module with .tf or .tf.json files at its root (a containing folder is fine).')
    authoring.validate_files(files)
    warnings = ['Excluded Git metadata, workflows, local state, environment or variable-value files: ' + ', '.join(skipped[:10]) + (' …' if len(skipped) > 10 else '')] if skipped else []
    return files, warnings
