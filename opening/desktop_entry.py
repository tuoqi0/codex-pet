#!/usr/bin/python3
"""Opt-in, reversible user-level override for the official ChatGPT desktop entry."""
import argparse
import hashlib
import json
import os
import shlex
import tempfile
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def quoted(value):
    # Desktop entry values are unescaped once before Exec argument parsing.
    slash = chr(92)
    value = str(value).replace(slash, slash * 4).replace('"', slash * 2 + '"').replace('`', slash * 2 + '`').replace('$', slash * 2 + '$').replace('%', '%%')
    return '"' + value + '"'


def write_new(path, data):
    # Link a completed temporary file into place without overwriting a pre-existing file.
    fd, temporary = tempfile.mkstemp(prefix='.codex-pet-opening-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def install(source, target, record, launcher, command):
    if target.exists() or target.is_symlink() or record.exists() or record.is_symlink():
        raise ValueError('已有用户入口或记录；未更改任何文件')
    if not launcher.is_absolute() or not launcher.is_file() or not command.is_absolute() or not command.is_file():
        raise ValueError('启动器或官方客户端路径不可用')
    content = source.read_text(encoding='utf-8')
    original = 'Exec=chatgpt %U'
    if content.splitlines().count(original) != 1:
        raise ValueError('官方入口格式已改变；未进行修改')
    launch = shlex.quote(str(launcher))
    client = shlex.quote(str(command))
    fallback = f'if [ -f {launch} ]; then exec /usr/bin/python3 {launch} --command {client} -- "$@"; else exec {client} "$@"; fi'
    replacement = 'Exec=/bin/sh -c ' + quoted(fallback) + ' codex-pet %U'
    lines = content.splitlines(keepends=True)
    output = ''.join(replacement + line[len(original):] if line.rstrip('\r\n') == original else line for line in lines).encode('utf-8')
    manifest = json.dumps({'format': 1, 'output_sha256': sha(output)}, sort_keys=True).encode('utf-8')
    target.parent.mkdir(parents=True, exist_ok=True)
    record.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink() or record.parent.is_symlink():
        raise ValueError('入口目录是符号链接；未进行修改')
    write_new(record, manifest)
    try:
        write_new(target, output)
    except BaseException:
        record.unlink(missing_ok=True)
        raise
    print('已启用桌面客户端开场：', target)


def remove(target, record):
    if record.is_symlink() or not record.is_file() or target.is_symlink():
        raise ValueError('没有有效记录或入口为符号链接；未进行修改')
    try:
        data = json.loads(record.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('记录无效；未进行修改') from exc
    if set(data) != {'format', 'output_sha256'} or data['format'] != 1 or not isinstance(data['output_sha256'], str):
        raise ValueError('记录无效；未进行修改')
    if target.exists():
        if not target.is_file() or sha(target.read_bytes()) != data['output_sha256']:
            raise ValueError('用户入口已更改；拒绝覆盖或删除')
        target.unlink()
    record.unlink()
    print('已撤销桌面客户端开场；系统入口仍然可用')


def main():
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    parser = argparse.ArgumentParser(description='启用或撤销桌面客户端的用户级开场')
    parser.add_argument('action', choices=('install', 'remove'))
    parser.add_argument('--source', type=Path, default=Path('/usr/share/applications/chatgpt.desktop'))
    parser.add_argument('--output', type=Path, default=data / 'applications/chatgpt.desktop')
    parser.add_argument('--record', type=Path, default=data / 'codex-pet/desktop-opening.json')
    parser.add_argument('--launcher', type=Path, default=Path(__file__).resolve().with_name('desktop_splash.py'))
    parser.add_argument('--command', type=Path, default=Path('/usr/bin/chatgpt'))
    args = parser.parse_args()
    try:
        if args.action == 'install':
            install(args.source, args.output, args.record, args.launcher, args.command)
        else:
            remove(args.output, args.record)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
