"""Local, opt-out companionship reminders.

The scheduler knows the application's runtime and the local wall clock only;
it does not observe keyboard, mouse, food, drink, or a user's actual work.
All clock values are supplied by the caller to make suspend and tests explicit.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, time as clock_time
import json
import math
import os
from pathlib import Path
import random
import tempfile


@dataclass
class CompanionSettings:
    enabled: bool = True
    breaks: bool = True
    water: bool = True
    meals: bool = True
    greetings: bool = True
    break_minutes: int = 60
    water_minutes: int = 90
    greeting_minutes: int = 150
    quiet_start: str = '22:00'
    quiet_end: str = '08:00'
    lunch_time: str = '12:00'
    dinner_time: str = '18:30'
    weather_enabled: bool = False
    weather_label: str = ''
    weather_latitude: float | None = None
    weather_longitude: float | None = None

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('陪伴设定应为物件')
        names = cls.__dataclass_fields__
        data = {key: val for key, val in value.items() if key in names}
        result = cls(**data)
        result.validate()
        return result

    def validate(self):
        for key in ('enabled', 'breaks', 'water', 'meals', 'greetings', 'weather_enabled'):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f'{key} 必须是布林值')
        for key in ('break_minutes', 'water_minutes', 'greeting_minutes'):
            val = getattr(self, key)
            if isinstance(val, bool) or not isinstance(val, int) or not 15 <= val <= 1440:
                raise ValueError(f'{key} 必须介于 15 和 1440 分钟')
        for key in ('quiet_start', 'quiet_end', 'lunch_time', 'dinner_time'):
            _parse_time(getattr(self, key))
        if not isinstance(self.weather_label, str) or len(self.weather_label) > 80:
            raise ValueError('天气地点名称无效')
        for key, limit in (('weather_latitude', 90), ('weather_longitude', 180)):
            val = getattr(self, key)
            if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float))
                                    or not math.isfinite(val) or abs(val) > limit):
                raise ValueError(f'{key} 超出有效范围')
        if self.weather_enabled and (self.weather_latitude is None or self.weather_longitude is None):
            raise ValueError('启用天气需要明确设定经纬度')


def _parse_time(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ':' or not (value[:2] + value[3:]).isdigit():
        raise ValueError('时间须为 HH:MM')
    try:
        return clock_time(int(value[:2]), int(value[3:]))
    except ValueError as exc:
        raise ValueError('时间须为有效的 HH:MM') from exc


class CompanionSettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / '.config' / 'codex-pet' / 'companion.json'

    def load(self):
        try:
            with self.path.open(encoding='utf-8') as stream:
                return CompanionSettings.from_dict(json.load(stream))
        except FileNotFoundError:
            return CompanionSettings()

    def save(self, settings):
        settings.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkstemp creates an exclusive, unpredictable file with mode 0600
        # before any settings (which may include coordinates) are written.
        descriptor, name = tempfile.mkstemp(prefix=f'.{self.path.name}.',
                                            suffix='.tmp', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CompanionEvent:
    kind: str
    message: str
    action: str


class CompanionScheduler:
    """Produce at most one gentle prompt at a time, never infer activity.

    Call tick(monotonic_seconds, local_datetime, available) on a GUI timer.
    Call set_settings after the user changes settings, and snooze to pause.
    'available' should be false when pets are hidden, being dragged, or busy.
    """
    FIRST_PROMPT_DELAY = 20 * 60
    MIN_PROMPT_GAP = 20 * 60
    SUSPEND_GAP = 5 * 60
    MEAL_WINDOW = 90 * 60
    MESSAGES = {
        'break': ('我陪你有一阵子了，要不要起身舒展一下？', '休息一下眼睛，活动活动肩膀吧。', '我们一起停一小会儿，换个姿势吧。'),
        'water': ('想起来提醒你，方便的话喝点水吧。', '给自己倒杯水，慢慢喝一口吧。', '我来陪你喝水时间，不着急哦。'),
        'lunch': ('到午餐时间附近啦，记得照顾自己。', '如果还没吃午饭，找个空档吃点东西吧。'),
        'dinner': ('晚餐时间附近啦，别忘了照顾自己。', '忙完这一点，记得留时间吃晚饭哦。'),
        'greeting': ('我在这里陪着你，需要我时叫我哦。', '希望今天也有一点让你开心的小事。', '慢慢来，我们一起。'),
    }
    ACTIONS = {'break': 'break', 'water': 'water', 'lunch': 'meal', 'dinner': 'meal', 'greeting': 'greeting'}

    def __init__(self, now, settings=None, rng=None):
        self.settings = settings or CompanionSettings()
        self.settings.validate()
        self.rng = rng or random.Random()
        self.next_allowed = now + self.FIRST_PROMPT_DELAY
        self.last_tick = now
        self.paused_until = 0.0
        self.meals_seen = {}
        self._reset_intervals(now)

    def _reset_intervals(self, now):
        self.next_due = {kind: now + getattr(self.settings, f'{kind}_minutes') * 60
                         for kind in ('break', 'water', 'greeting')}

    def set_settings(self, settings, now):
        settings.validate()
        self.settings = settings
        self._reset_intervals(now)
        self.next_allowed = now + self.FIRST_PROMPT_DELAY
        self.last_tick = now

    def snooze(self, now, minutes=60):
        if not 1 <= minutes <= 1440:
            raise ValueError('暂停时间须介于 1 和 1440 分钟')
        self.paused_until = now + minutes * 60
        self._reset_intervals(self.paused_until)
        self.next_allowed = self.paused_until + self.FIRST_PROMPT_DELAY

    def _quiet(self, wall):
        start, end = _parse_time(self.settings.quiet_start), _parse_time(self.settings.quiet_end)
        local = wall.time().replace(tzinfo=None)
        if start == end:
            return False
        return start <= local < end if start < end else local >= start or local < end

    def tick(self, now, wall, available=True):
        if not isinstance(wall, datetime):
            raise ValueError('wall 必须是本地 datetime')
        elapsed = now - self.last_tick
        self.last_tick = now
        # Sleep/resume or a restarted monotonic clock must not flood the user.
        if elapsed < 0 or elapsed > self.SUSPEND_GAP:
            self._reset_intervals(now)
            self.next_allowed = now + self.FIRST_PROMPT_DELAY
            return None
        if now < self.paused_until:
            return None
        if not self.settings.enabled or self._quiet(wall):
            self._reset_intervals(now)
            self.next_allowed = now + self.FIRST_PROMPT_DELAY
            return None
        if not available or now < self.next_allowed:
            return None
        candidates = []
        if self.settings.meals:
            local = wall.time().replace(tzinfo=None)
            seconds = local.hour * 3600 + local.minute * 60 + local.second
            for kind in ('lunch', 'dinner'):
                target = _parse_time(getattr(self.settings, f'{kind}_time'))
                target_seconds = target.hour * 3600 + target.minute * 60
                if (0 <= seconds - target_seconds < self.MEAL_WINDOW
                        and self.meals_seen.get(kind) != wall.date()):
                    candidates.append(kind)
        for kind, enabled in (('break', self.settings.breaks), ('water', self.settings.water), ('greeting', self.settings.greetings)):
            if enabled and now >= self.next_due[kind]:
                candidates.append(kind)
        if not candidates:
            return None
        kind = candidates[0]
        if kind in ('lunch', 'dinner'):
            self.meals_seen[kind] = wall.date()
        else:
            self.next_due[kind] = now + getattr(self.settings, f'{kind}_minutes') * 60
        self.next_allowed = now + self.MIN_PROMPT_GAP
        return CompanionEvent(kind, self.rng.choice(self.MESSAGES[kind]), self.ACTIONS[kind])

