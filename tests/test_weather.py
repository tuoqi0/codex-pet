import threading
import unittest
from unittest.mock import Mock

import requests

from weather import (FORECAST_URL, GEOCODING_URL, TIMEOUT, WeatherClient,
                     WeatherError, current_weather, search_locations)


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class WeatherTests(unittest.TestCase):
    def test_search_shows_all_matches_and_sends_only_user_query(self):
        get = Mock(return_value=Response({'results': [
            {'name': 'Springfield', 'admin1': 'Illinois', 'country': 'United States', 'latitude': 39.8, 'longitude': -89.6, 'timezone': 'America/Chicago'},
            {'name': 'Springfield', 'admin1': 'Massachusetts', 'country': 'United States', 'latitude': 42.1, 'longitude': -72.6},
        ]}))
        matches = search_locations('Springfield', get)
        self.assertEqual([x['admin1'] for x in matches], ['Illinois', 'Massachusetts'])
        self.assertEqual(matches[1]['country'], 'United States')
        self.assertEqual(get.call_args.args[0], GEOCODING_URL)
        self.assertEqual(get.call_args.kwargs['params']['name'], 'Springfield')
        self.assertEqual(get.call_args.kwargs['timeout'], TIMEOUT)
        self.assertEqual(search_locations('none', Mock(return_value=Response({}))), [])

    def test_current_only_uses_selected_coordinates_and_returns_valid_time(self):
        location = {'name': 'A', 'admin1': 'B', 'country': 'C', 'latitude': 42.1, 'longitude': -72.6}
        get = Mock(return_value=Response({'timezone': 'America/New_York', 'current': {
            'time': '2026-10-28T08:15', 'temperature_2m': 15.2, 'weather_code': 61},
            'current_units': {'temperature_2m': '°C'}}))
        weather = current_weather(location, get)
        self.assertEqual((weather['description'], weather['temperature'], weather['time']), ('小雨', 15.2, '2026-10-28T08:15'))
        self.assertEqual(weather['timezone'], 'America/New_York')
        self.assertIn('Open-Meteo', weather['source'])
        self.assertEqual(weather['temperature_unit'], '°C')
        self.assertEqual(get.call_args.args[0], FORECAST_URL)
        self.assertEqual(get.call_args.kwargs['params']['latitude'], 42.1)
        self.assertEqual(get.call_args.kwargs['params']['longitude'], -72.6)
        self.assertEqual(get.call_args.kwargs['params']['timezone'], 'auto')

    def test_errors_never_fabricate_weather_or_search_unselected_location(self):
        get = Mock(side_effect=requests.Timeout())
        with self.assertRaises(WeatherError):
            search_locations('Paris', get)
        with self.assertRaises(WeatherError):
            current_weather(None, get)
        self.assertEqual(get.call_count, 1)
        with self.assertRaises(WeatherError):
            current_weather({'latitude': float('nan'), 'longitude': 1}, get)
        with self.assertRaises(WeatherError):
            current_weather({'latitude': 1, 'longitude': 2}, Mock(return_value=Response({'current': {}})))
        no_request = Mock()
        with self.assertRaises(WeatherError):
            search_locations('a', no_request)
        no_request.assert_not_called()

    def test_explicit_async_calls_deliver_success_and_failure_off_main_thread(self):
        get = Mock(return_value=Response({'results': []}))
        client = WeatherClient(get)
        get.assert_not_called()
        seen = []
        main = threading.get_ident()
        worker = client.search('Paris', lambda event, data: seen.append((event, data, threading.get_ident())))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen[0][:2], ('locations', []))
        self.assertNotEqual(seen[0][2], main)
        worker = client.current(None, lambda event, data: seen.append((event, data, threading.get_ident())))
        worker.join(3)
        self.assertEqual(seen[1][0], 'error')
        self.assertEqual(seen[1][1]['operation'], 'weather')
        self.assertEqual(get.call_count, 1)

    def test_failures_explain_network_timeout_and_rate_limit(self):
        location = {'latitude': 38.91, 'longitude': 121.61}
        for exception, message in ((requests.Timeout(), '超时'),
                                   (requests.ConnectionError(), '检查网络'),
                                   (requests.HTTPError(response=requests.Response()), '返回错误')):
            with self.subTest(error=type(exception).__name__):
                with self.assertRaisesRegex(WeatherError, message):
                    current_weather(location, Mock(side_effect=exception))
        response = requests.Response()
        response.status_code = 429
        with self.assertRaisesRegex(WeatherError, '过于频繁'):
            current_weather(location, Mock(side_effect=requests.HTTPError(response=response)))


if __name__ == '__main__':
    unittest.main()
