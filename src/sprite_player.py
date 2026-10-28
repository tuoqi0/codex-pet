"""Play a local character catalogue with at most one decoded frame retained."""
import json
import math
from pathlib import Path

from PIL import Image
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QImage


class SpritePlayer(QObject):
    frame_changed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root).resolve()
        manifest = json.loads((self.root / '素材清单.json').read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError('素材清单格式无效')
        self.entries = {}
        for category in ('idle', 'additionalIdleAnimations', 'variants', 'workAnimations',
                         'dragAnimation', 'landingAnimation', 'quotaAnimation'):
            group = manifest.get(category, [])
            for entry in group if isinstance(group, list) else [group]:
                if isinstance(entry, dict) and isinstance(entry.get('id'), str):
                    if entry['id'] in self.entries:
                        raise ValueError('重复的动画 ID')
                    self.entries[entry['id']] = entry
        if 'idle' not in self.entries:
            raise ValueError('素材清单缺少待机动画')
        self.source = None
        self.image = None
        self.current = None
        self.index = 0
        self.loop = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        if not self.play('idle'):
            raise ValueError('无法打开待机动画')

    def path_for(self, entry):
        name = entry.get('playbackFile') or entry.get('file')
        if not isinstance(name, str) or not name:
            raise ValueError('动画缺少文件名')
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() != '.webp':
            raise ValueError('素材路径不安全')
        return path

    def frame(self, source, index):
        source.seek(index)
        rgba = source.convert('RGBA')
        w, h = rgba.size
        data = rgba.tobytes()
        image = QImage(data, w, h, w * 4, QImage.Format_RGBA8888).copy()
        duration = source.info.get('duration', 100)
        if not isinstance(duration, (int, float)) or not math.isfinite(duration):
            duration = 100
        return image, max(10, min(10000, int(duration)))

    def play(self, ident):
        entry = self.entries.get(ident)
        if not entry:
            self.error.emit(f'动画不可用：{ident}')
            return False
        source = None
        try:
            source = Image.open(self.path_for(entry))
            if source.format != 'WEBP':
                raise ValueError('不是 WebP 文件')
            image, duration = self.frame(source, 0)
        except (OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
            if source:
                source.close()
            self.error.emit(f'无法播放 {ident}：{exc}')
            return False
        self.timer.stop()
        if self.source:
            self.source.close()
        self.source, self.current, self.index = source, ident, 0
        self.loop = ident in ('idle', 'drag', 'work02')
        self.image = image
        self.timer.start(duration)
        self.frame_changed.emit()
        return True

    def advance(self):
        if not self.source:
            return
        index = self.index + 1
        if index >= self.source.n_frames:
            if not self.loop:
                self.play('idle')
                return
            index = 0
        try:
            self.image, duration = self.frame(self.source, index)
            self.index = index
            self.timer.start(duration)
            self.frame_changed.emit()
        except (OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
            self.timer.stop()
            self.error.emit(f'动画解码失败：{exc}')

    def close(self):
        self.timer.stop()
        if self.source:
            self.source.close()
            self.source = None
