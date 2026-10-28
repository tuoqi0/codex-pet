"""Explicit, asynchronous Open-Meteo queries for a manually chosen place.

Callbacks run on a worker thread; GUI consumers must marshal them to the UI
thread. Creating a client never queries a service or infers the user's location.
"""
import math
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
SOURCE = 'Open-Meteo（天气模型）'
TIMEOUT = (4, 8)

# Open-Meteo uses WMO interpretation codes, not free-text conditions.
CONDITIONS = {
    0: '晴朗', 1: '大致晴朗', 2: '局部多云', 3: '阴天',
    45: '雾', 48: '雾凇', 51: '小毛毛雨', 53: '中等毛毛雨', 55: '大毛毛雨',
    56: '小冻毛毛雨', 57: '大冻毛毛雨', 61: '小雨', 63: '中雨', 65: '大雨',
    66: '小冻雨', 67: '大冻雨', 71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '小阵雨', 81: '中阵雨', 82: '强阵雨', 85: '小阵雪', 86: '大阵雪',
    95: '雷暴', 96: '雷暴伴小冰雹', 97: '强雷暴', 99: '雷暴伴大冰雹',
}


class WeatherError(Exception):
    """The service was unavailable or did not return usable data."""


def _get_json(url, params, get):
    try:
        response = get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except requests.Timeout as exc:
        raise WeatherError('天气查询超时，请检查网络后重试。') from exc
    except requests.ConnectionError as exc:
        raise WeatherError('无法连接天气服务，请检查网络或代理设置后重试。') from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            raise WeatherError('天气服务请求过于频繁，请稍后重试。') from exc
        raise WeatherError('天气服务返回错误，请稍后重试。') from exc
    except requests.RequestException as exc:
        raise WeatherError('天气服务暂时不可用，请稍后重试。') from exc
    except ValueError as exc:
        raise WeatherError('天气服务返回了无效数据。') from exc
    if not isinstance(body, dict) or body.get('error'):
        raise WeatherError('天气服务返回了无效数据。')
    return body


def _coordinates(location):
    if not isinstance(location, dict):
        raise WeatherError('请先选择一个地点。')
    try:
        lat, lon = float(location['latitude']), float(location['longitude'])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError('地点坐标无效，请重新选择。') from exc
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise WeatherError('地点坐标无效，请重新选择。')
    return lat, lon


def search_locations(query, get=requests.get):
    """Return matching locations; caller must let the user choose one."""
    query = query.strip()
    if len(query) < 2:
        raise WeatherError('请输入至少两个字符的城市名称。')
    data = _get_json(GEOCODING_URL, {'name': query, 'count': 10, 'language': 'zh', 'format': 'json'}, get)
    results = data.get('results', [])
    if not isinstance(results, list):
        raise WeatherError('天气服务返回了无效地点列表。')
    locations = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            continue
        try:
            lat, lon = _coordinates(item)
        except WeatherError:
            continue
        locations.append({'name': item['name'], 'admin1': item.get('admin1', ''),
                          'country': item.get('country', ''), 'latitude': lat, 'longitude': lon,
                          'timezone': item.get('timezone', '')})
    return locations


def current_weather(location, get=requests.get):
    """Fetch current model conditions for exactly the user-selected location."""
    lat, lon = _coordinates(location)
    data = _get_json(FORECAST_URL, {'latitude': lat, 'longitude': lon,
                                   'current': 'temperature_2m,weather_code', 'timezone': 'auto',
                                   'daily': 'temperature_2m_max,rain_sum,showers_sum,snowfall_sum,weather_code,wind_gusts_10m_max',
                                   'hourly': 'weather_code',
                                   'forecast_days': 2}, get)
    current, units = data.get('current'), data.get('current_units')
    if not isinstance(current, dict) or not isinstance(units, dict):
        raise WeatherError('天气服务未返回当前天气。')
    temp, code = current.get('temperature_2m'), current.get('weather_code')
    if (isinstance(temp, bool) or not isinstance(temp, (int, float)) or not math.isfinite(temp)
            or isinstance(code, bool) or not isinstance(code, int)
            or not isinstance(current.get('time'), str) or not current['time']
            or not isinstance(data.get('timezone'), str) or not data['timezone']
            or not isinstance(units.get('temperature_2m'), str) or not units['temperature_2m']):
        raise WeatherError('天气服务返回的数据不完整。')
    return {'location': dict(location), 'time': current['time'], 'timezone': data['timezone'],
            'temperature': temp, 'temperature_unit': units['temperature_2m'],
            'weather_code': code, 'description': CONDITIONS.get(code, '天气代码 %s' % code),
            'source': SOURCE, 'daily': data.get('daily'), 'hourly': data.get('hourly')}


def tomorrow_warnings(data, now=None):
    """Return (local tomorrow, forecast warnings, unavailable category names).

    Missing inputs do not invalidate other categories. A warning needs positive
    forecast evidence; absent values must never be interpreted as no hazard.
    """
    try:
        zone = ZoneInfo(data['timezone'])
        local = (now or datetime.now(zone)).astimezone(zone)
        model_time = datetime.fromisoformat(data['time']).replace(tzinfo=zone)
        if not -300 <= (local - model_time).total_seconds() <= 2 * 3600:
            return None, {}, set()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None, {}, set()
    today, tomorrow = local.date(), local.date() + timedelta(days=1)
    daily = data.get('daily')
    daily = daily if isinstance(daily, dict) else {}
    days = daily.get('time')
    days = days if isinstance(days, list) else []
    i = days.index(today.isoformat()) if today.isoformat() in days else None
    j = days.index(tomorrow.isoformat()) if tomorrow.isoformat() in days else None

    def value(name, index):
        values = daily.get(name)
        if index is None or not isinstance(values, list) or index >= len(values):
            return None
        item = values[index]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            return None
        return float(item)

    warnings, missing = {}, set()
    today_max, next_max = value('temperature_2m_max', i), value('temperature_2m_max', j)
    if today_max is None or next_max is None:
        missing.add('气温')
    elif today_max - next_max >= 5:
        warnings['cold'] = f'明天最高温预计 {next_max:g}°C，比今天低 {today_max - next_max:g}°C，记得添衣。'
    elif next_max - today_max >= 5:
        warnings['hot'] = f'明天最高温预计 {next_max:g}°C，比今天高 {next_max - today_max:g}°C，适时调整衣物。'
    rain, showers, snow = (value(name, j) for name in ('rain_sum', 'showers_sum', 'snowfall_sum'))
    if rain is None or showers is None:
        missing.add('雨量')
    if (rain is not None and rain > 0) or (showers is not None and showers > 0):
        warnings['rain'] = '明天预报有雨，出门记得带伞。'
    if snow is None:
        missing.add('雪量')
    elif snow > 0:
        warnings['snow'] = '明天预报有雪，出门注意保暖和路滑。'
    gust = value('wind_gusts_10m_max', j)
    if gust is None:
        missing.add('阵风')
    elif gust >= 62:
        warnings['wind'] = f'明天最大阵风预计 {gust:g} 公里/小时，请关注当地气象部门预警。'
    hourly = data.get('hourly')
    times = hourly.get('time') if isinstance(hourly, dict) else None
    codes = hourly.get('weather_code') if isinstance(hourly, dict) else None
    if not isinstance(times, list) or not isinstance(codes, list) or len(times) != len(codes):
        missing.add('雾')
    else:
        selected = [(t, code) for t, code in zip(times, codes)
                    if isinstance(t, str) and t.startswith(tomorrow.isoformat() + 'T')]
        if len(selected) < 23 or any(type(code) is not int for _, code in selected):
            missing.add('雾')
        if any(type(code) is int and code in (45, 48) for _, code in selected):
            warnings['fog'] = '明天预报有雾，能见度可能降低，出行注意安全。'
    return tomorrow.isoformat(), warnings, missing


class WeatherClient:
    """Requests are only made when search or current is explicitly called."""
    def __init__(self, get=requests.get):
        self.get = get

    def _start(self, operation, value, callback):
        def work():
            try:
                result = (search_locations if operation == 'locations' else current_weather)(value, self.get)
            except WeatherError as exc:
                callback('error', {'operation': operation, 'message': str(exc)})
            else:
                callback(operation, result)
        thread = threading.Thread(target=work, daemon=True, name='pet-weather-' + operation)
        thread.start()
        return thread

    def search(self, query, callback):
        return self._start('locations', query, callback)

    def current(self, location, callback):
        return self._start('weather', location, callback)
