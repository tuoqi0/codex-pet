"""Explicit, asynchronous Open-Meteo queries for a manually chosen place.

Callbacks run on a worker thread; GUI consumers must marshal them to the UI
thread. Creating a client never queries a service or infers the user's location.
"""
import math
import threading

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
    except (requests.RequestException, ValueError) as exc:
        raise WeatherError('天气服务暂时不可用，请稍后重试。') from exc
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
                                   'current': 'temperature_2m,weather_code', 'timezone': 'auto'}, get)
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
            'source': SOURCE}


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
