#!/usr/bin/env python3
"""可撤销的版本专用 Codex 侧栏开场补丁。"""
import argparse, hashlib, json, os, shutil, stat, tempfile
from pathlib import Path
VERSION='26.1007.21434'
ORIGINALS={'webview/index.html':'0b6cd1d77fb1c374db06014ce82c829f5a58009d007188b1e742b34113d2db1f','out/extension.js':'a8069de80c8f8141481d033f94c3a95e1b59f399143cb2554d3f68469dfbe5b8'}
ASSETS={'opening.css':'cp-opening.css','opening.js':'cp-opening.js','dragon.png':'cp-dragon.png'}
HERE=Path(__file__).resolve().parent
ARTWORK = next((p for p in (HERE.parent / 'assets/opening-sprite.png', HERE.parent / 'opening-sprite.png') if p.is_file()), None)

def digest(data): return hashlib.sha256(data).hexdigest()
def atomic(path,data,mode=0o644):
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.cp-opening-')
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(name,mode); os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
def publish_asset(path, state, data):
    # Link a completed temporary file into place without overwriting another file.
    fd, name = tempfile.mkstemp(dir=state, prefix='.cp-opening-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.link(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['install','remove','upgrade']); p.add_argument('extension',type=Path); args=p.parse_args()
    root=args.extension.resolve(strict=True)
    meta=json.loads((root/'package.json').read_text())
    if (meta.get('publisher'),meta.get('name'),meta.get('version'))!=('openai','chatgpt',VERSION): raise SystemExit('扩展身份或版本不匹配；未更改文件')
    for name in ['webview','webview/assets','out',*ORIGINALS]:
        if (root/name).is_symlink(): raise SystemExit('扩展目录包含符号链接；未更改文件')
    state=root/'webview/.codex-pet-opening'; manifest=state/'manifest.json'
    if args.action=='upgrade':
        # Remove validates the recorded old files before restoring their originals.
        # If installation subsequently fails, the unmodified extension remains usable.
        import sys
        saved = sys.argv
        try:
            sys.argv = [saved[0], 'remove', str(root)]
            main()
            sys.argv = [saved[0], 'install', str(root)]
            main()
        finally:
            sys.argv = saved
        return
    if args.action=='install':
        originals={name:(root/name).read_bytes() for name in ORIGINALS}
        if any(digest(originals[name])!=expected for name,expected in ORIGINALS.items()) or state.exists(): raise SystemExit('原文件不匹配或已有补丁；未更改文件')
        targets={name:root/'webview/assets'/name for name in ASSETS.values()}
        if any(x.exists() or x.is_symlink() for x in targets.values()): raise SystemExit('目标文件已存在；未更改文件')
        html=originals['webview/index.html'].decode('utf8'); marker='<!-- CP_SIDEBAR -->'
        html=html.replace('</head>',marker+'\n<link rel="stylesheet" href="./assets/cp-opening.css">\n</head>')
        body='<div id="codex-pet-opening" aria-hidden="true"><div class="cp-rings"></div><div class="cp-sparks"><i></i><i></i><i></i><i></i><i></i><i></i></div><div class="cp-portrait"></div><div class="cp-beam"></div><div class="cp-title">CODEX · COMPANION</div></div><script src="./assets/cp-opening.js" defer></script>\n'
        html=html.replace('</body>',body+'</body>')
        old='e.html=await this.getWebviewContent(e),s.start()'
        new='e.html=(await this.getWebviewContent(e)).replace("<!-- CP_SIDEBAR -->",r==="sidebar"?"<meta name=\\"codex-pet-sidebar\\" content=\\"1\\">":""),s.start()'
        ext=originals['out/extension.js'].decode('utf8')
        visibility='e.onDidChangeVisibility(()=>{je.commands.executeCommand("setContext","chatgpt.sidebarView.visible",e.visible)'; visibility_new='e.onDidChangeVisibility(()=>{e.visible&&e.webview.postMessage({type:"codex-pet-opening-show"});je.commands.executeCommand("setContext","chatgpt.sidebarView.visible",e.visible)'
        if html.count(marker)!=1 or ext.count(old)!=1 or ext.count(visibility)!=1: raise SystemExit('扩展结构不匹配；未更改文件')
        outputs={'webview/index.html':html.encode(), 'out/extension.js':ext.replace(old,new).replace(visibility,visibility_new).encode()}
        modes={name:stat.S_IMODE((root/name).stat().st_mode) for name in ORIGINALS}
        if ARTWORK is None: raise SystemExit('没有找到龙娘素材；未更改文件')
        asset_data={name:(ARTWORK if source=='dragon.png' else HERE/source).read_bytes() for source,name in ASSETS.items()}
        state.mkdir(mode=0o700); changed=[]
        try:
            for i,name in enumerate(ORIGINALS): atomic(state/f'original-{i}',originals[name],0o600)
            data={'originals':list(ORIGINALS),'outputs':{name:digest(value) for name,value in outputs.items()},'modes':modes,'assets':{name:digest(value) for name,value in asset_data.items()}}
            atomic(manifest,json.dumps(data).encode(),0o600)
            for name,value in asset_data.items(): publish_asset(targets[name],state,value); changed.append(targets[name])
            for name,value in outputs.items():
                if digest((root/name).read_bytes())!=ORIGINALS[name]: raise RuntimeError('扩展文件在安装期间发生变化；拒绝覆盖')
                atomic(root/name,value,modes[name]); changed.append(root/name)
        except Exception:
            # Only roll back files this invocation created or replaced and whose
            # contents still match ours. Keep the manifest if anything changed.
            recoverable=True
            for name in ORIGINALS:
                path=root/name
                if path in changed:
                    if not path.is_symlink() and digest(path.read_bytes())==digest(outputs[name]): atomic(path,originals[name],modes[name])
                    else: recoverable=False
            for name,target in targets.items():
                if target in changed:
                    if not target.is_symlink() and target.is_file() and digest(target.read_bytes())==digest(asset_data[name]): target.unlink()
                    else: recoverable=False
            if recoverable: shutil.rmtree(state)
            raise
        print('已安装：',root)
    else:
        if state.is_symlink() or manifest.is_symlink(): raise SystemExit('备份为符号链接；未更改文件')
        if state.is_dir() and not manifest.exists():
            # Before the manifest is written, installation has not touched targets.
            allowed={f'original-{i}' for i in range(len(ORIGINALS))}
            if any(p.name not in allowed and not p.name.startswith('.cp-opening-') for p in state.iterdir()): raise SystemExit('备份包含未知文件；拒绝删除')
            if any(digest((root/name).read_bytes())!=expected for name,expected in ORIGINALS.items()) or any((root/'webview/assets'/name).exists() or (root/'webview/assets'/name).is_symlink() for name in ASSETS.values()): raise SystemExit('文件已变化；拒绝恢复')
            for i,name in enumerate(ORIGINALS):
                backup=state/f'original-{i}'
                if backup.is_symlink() or (backup.exists() and digest(backup.read_bytes())!=ORIGINALS[name]): raise SystemExit('备份已变化；拒绝恢复')
            shutil.rmtree(state); print('已清理未完成的准备：',root); return
        m=json.loads(manifest.read_text())
        if m.get('originals')!=list(ORIGINALS) or set(m.get('assets',{}))!=set(ASSETS.values()) or set(m.get('outputs',{}))!=set(ORIGINALS): raise SystemExit('备份清单不匹配；未更改文件')
        allowed={'manifest.json',*(f'original-{i}' for i in range(len(ORIGINALS)))}
        if any(p.name not in allowed and not p.name.startswith('.cp-opening-') for p in state.iterdir()): raise SystemExit('备份包含未知文件；拒绝删除')
        backups={}
        for i,name in enumerate(ORIGINALS):
            source=state/f'original-{i}'; path=root/name
            if source.is_symlink() or digest(source.read_bytes())!=ORIGINALS[name] or digest(path.read_bytes()) not in (ORIGINALS[name],m['outputs'][name]): raise SystemExit('原文件或补丁已变化；拒绝覆盖')
            backups[name]=source.read_bytes()
        for name,expected in m['assets'].items():
            target=root/'webview/assets'/name
            if target.is_symlink() or (target.exists() and (not target.is_file() or digest(target.read_bytes())!=expected)): raise SystemExit('补丁资源已变化；拒绝覆盖')
        for name,value in backups.items():
            if digest((root/name).read_bytes())!=ORIGINALS[name]: atomic(root/name,value,m['modes'][name])
        for name in m['assets']:
            target=root/'webview/assets'/name
            if target.exists(): target.unlink()
        shutil.rmtree(state); print('已还原：',root)
if __name__=='__main__': main()
