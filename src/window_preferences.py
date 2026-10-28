"""Private, local preferences for the two independent pet windows.

Positions are X11 logical desktop coordinates. Wayland owns placement, so
callers must not use or overwrite those coordinates in a Wayland session.
This module does not inspect screens or widgets; the UI supplies work areas.
"""
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import shutil
import tempfile


@dataclass(frozen=True)
class PetWindowPreference:
    x: int | None = None
    y: int | None = None
    scale: float = 1.0
    visible: bool = True

    def validate(self):
        if (self.x is None) != (self.y is None):
            raise ValueError('窗口位置必须同时包含 x 和 y')
        for value in (self.x, self.y):
            if value is not None and (type(value) is not int or not -(2 ** 31) <= value < 2 ** 31):
                raise ValueError('窗口位置无效')
        if (isinstance(self.scale, bool) or not isinstance(self.scale, (int, float))
                or not math.isfinite(self.scale) or not 0.35 <= self.scale <= 2.0):
            raise ValueError('角色缩放比例无效')
        if type(self.visible) is not bool:
            raise ValueError('角色可见状态无效')


@dataclass(frozen=True)
class WindowPreferences:
    dragon: PetWindowPreference = PetWindowPreference(scale=0.63)
    qpet: PetWindowPreference = PetWindowPreference()
    dragon_patrol: bool = False

    def validate(self):
        if not isinstance(self.dragon, PetWindowPreference) or not isinstance(self.qpet, PetWindowPreference):
            raise ValueError('角色窗口设置无效')
        self.dragon.validate()
        self.qpet.validate()
        if type(self.dragon_patrol) is not bool:
            raise ValueError('巡游设置无效')
        if not self.dragon.visible and not self.qpet.visible:
            raise ValueError('至少需要显示一个角色')


def _window(value, default):
    if not isinstance(value, dict):
        raise ValueError('角色窗口设置应为对象')
    fields = ('x', 'y', 'scale', 'visible')
    result = replace(default, **{key: value[key] for key in fields if key in value})
    result.validate()
    return result


class WindowPreferencesStore:
    """Load settings without rewriting damaged files; save by explicit action."""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / '.config' / 'codex-pet' / 'windows.json'
        self._damaged = False
        self.backup_path = None

    def load(self):
        """Return ``(preferences, warning)``; a missing file is not an error.

        Bad data falls back to defaults and leaves the original file intact.
        The caller should show the warning before a subsequent user save.
        """
        defaults = WindowPreferences()
        try:
            with self.path.open(encoding='utf-8') as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
                raise ValueError('版本或格式无效')
            prefs = WindowPreferences(
                dragon=_window(data.get('dragon', {}), defaults.dragon),
                qpet=_window(data.get('qpet', {}), defaults.qpet),
                dragon_patrol=data.get('dragon_patrol', False))
            prefs.validate()
            self._damaged = False
            return prefs, None
        except FileNotFoundError:
            self._damaged = False
            return defaults, None
        except (OSError, ValueError, TypeError) as exc:
            self._damaged = True
            return defaults, f'角色窗口设置读取失败，原文件已保留：{exc}'

    def save(self, preferences):
        preferences.validate()
        def dump_window(value):
            return {'x': value.x, 'y': value.y, 'scale': value.scale, 'visible': value.visible}
        data = {'version': 1, 'dragon': dump_window(preferences.dragon),
                'qpet': dump_window(preferences.qpet), 'dragon_patrol': preferences.dragon_patrol}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=f'.{self.path.name}.', suffix='.tmp', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            if self._damaged and self.path.exists():
                backup_fd, backup_name = tempfile.mkstemp(prefix=f'.{self.path.name}.damaged-',
                                                          suffix='.bak', dir=self.path.parent)
                backup = Path(backup_name)
                try:
                    with os.fdopen(backup_fd, 'wb') as target, self.path.open('rb') as source:
                        shutil.copyfileobj(source, target)
                except OSError:
                    backup.unlink(missing_ok=True)
                    raise
                self.backup_path = backup
            temporary.replace(self.path)
            self._damaged = False
        finally:
            temporary.unlink(missing_ok=True)


def ensure_visible(preferences, available):
    """Ensure at least one *available* pet is visible, without persisting it."""
    available = tuple(name for name in ('dragon', 'qpet') if name in available)
    if not available:
        raise ValueError('没有可显示的角色')
    if not any(getattr(preferences, name).visible for name in available):
        name = available[0]
        preferences = replace(preferences, **{name: replace(getattr(preferences, name), visible=True)})
    return preferences


def set_visible(preferences, name, visible, available):
    """Set one visibility, rejecting an action that hides the final available pet."""
    if name not in ('dragon', 'qpet') or type(visible) is not bool:
        raise ValueError('角色或可见状态无效')
    updated = replace(preferences, **{name: replace(getattr(preferences, name), visible=visible)})
    if not any(getattr(updated, key).visible for key in ('dragon', 'qpet') if key in available):
        raise ValueError('至少需要显示一个可用角色')
    updated.validate()
    return updated


def remember_position(window, x, y, platform):
    """Return updated X11 coordinates; preserve stored coordinates on Wayland."""
    if platform.lower().startswith('wayland'):
        return window
    updated = replace(window, x=x, y=y)
    updated.validate()
    return updated


def restored_position(window, size, screens, platform):
    """Return a reachable X11 position, or None if placement belongs to Wayland.

    ``size`` is (width, height), and ``screens`` contains (x, y, width, height)
    available work areas in logical pixels. Prefer the screen with greatest
    overlap, otherwise the closest screen. Oversized windows start at the
    work area's top-left; shrinking them is the caller's responsibility.
    """
    if platform.lower().startswith('wayland') or window.x is None or window.y is None:
        return None
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError('窗口尺寸无效')
    screens = tuple(area for area in screens if area[2] > 0 and area[3] > 0)
    if not screens:
        return None
    x, y = window.x, window.y

    def rank(area):
        sx, sy, sw, sh = area
        overlap_w = max(0, min(x + width, sx + sw) - max(x, sx))
        overlap_h = max(0, min(y + height, sy + sh) - max(y, sy))
        cx, cy = x + width / 2, y + height / 2
        dx = max(sx - cx, 0, cx - (sx + sw))
        dy = max(sy - cy, 0, cy - (sy + sh))
        return overlap_w * overlap_h, -(dx * dx + dy * dy)

    sx, sy, sw, sh = max(screens, key=rank)
    return (max(sx, min(x, sx + sw - width)), max(sy, min(y, sy + sh - height)))
