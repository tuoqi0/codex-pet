#!/usr/bin/python3
"""Import the Q character from a user-supplied codex-pet.tar.xz."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

PREFIX = 'codex-pet/user-assets/bailong/'
MAX_SIZE = 2_000_000_000
MAX_FILES = 128


def default_destination():
    data = os.environ.get('XDG_DATA_HOME')
    root = Path(data) if data and Path(data).is_absolute() else Path.home() / '.local/share'
    return root / 'codex-pet/bailong'


def import_assets(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f'素材目录已存在：{destination}；请先手动备份或移走')
    destination.parent.mkdir(parents=True, exist_ok=True)
    total, names = 0, set()
    with tempfile.TemporaryDirectory(prefix='.bailong-import-', dir=destination.parent) as temp:
        root = Path(temp) / 'bailong'
        root.mkdir()
        with tarfile.open(archive, mode='r|xz') as bundle:
            for item in bundle:
                if not item.name.startswith(PREFIX):
                    continue
                name = item.name[len(PREFIX):]
                if not name or item.isdir():
                    continue
                relative = PurePosixPath(name)
                if (not item.isfile() or relative.is_absolute() or '..' in relative.parts
                        or relative.suffix.lower() not in ('.webp', '.json', '.md')):
                    raise ValueError(f'不安全的素材条目：{name}')
                if name in names or len(names) >= MAX_FILES:
                    raise ValueError('素材条目重复或数量过多')
                names.add(name)
                total += item.size
                if total > MAX_SIZE:
                    raise ValueError('素材总大小超过限制')
                target = root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(item) as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out, length=1024 * 1024)
        manifest = json.loads((root / '素材清单.json').read_text(encoding='utf-8'))
        idle = manifest.get('idle', {}) if isinstance(manifest, dict) else {}
        path = (idle.get('playbackFile') or idle.get('file')) if isinstance(idle, dict) else None
        if not isinstance(path, str) or path not in names:
            raise ValueError('素材清单缺少有效的待机动画')
        os.rename(root, destination)
    return len(names), total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path, default=default_destination())
    args = parser.parse_args()
    try:
        count, total = import_assets(args.archive, args.destination)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f'导入失败：{exc}\n')
    print(f'已导入 {count} 个文件（{total} 字节）到 {args.destination}')


if __name__ == '__main__':
    main()
