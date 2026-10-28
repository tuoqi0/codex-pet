import sys
import os
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from companion import CompanionScheduler, CompanionSettings, CompanionSettingsStore


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 28, 8, 0)
        self.settings = CompanionSettings(meals=False, greetings=False)
        self.scheduler = CompanionScheduler(0, self.settings)

    def advance(self, minutes, available=True):
        events = []
        for minute in range(1, minutes + 1):
            event = self.scheduler.tick(minute * 60, self.start + timedelta(minutes=minute), available)
            if event:
                events.append((minute, event))
        return events

    def test_delayed_runtime_reminders_and_spacing(self):
        events = self.advance(150)
        self.assertEqual([(m, e.kind) for m, e in events], [(60, 'break'), (90, 'water'), (120, 'break')])
        self.assertTrue(all('工作了' not in e.message and '喝過' not in e.message for _, e in events))

    def test_quiet_hours_cross_midnight_and_morning_delay(self):
        self.assertTrue(self.scheduler._quiet(datetime(2026, 10, 28, 23, 30)))
        self.assertTrue(self.scheduler._quiet(datetime(2026, 10, 29, 7, 59)))
        self.assertFalse(self.scheduler._quiet(datetime(2026, 10, 29, 8, 0)))
        self.assertFalse(self.scheduler._quiet(datetime(2026, 10, 29, 21, 59)))
        self.assertFalse(CompanionScheduler(0, replace(self.settings, quiet_start='00:00', quiet_end='00:00'))._quiet(self.start))
        self.start = datetime(2026, 10, 28, 21, 30)
        events = self.advance(720)
        self.assertFalse(any(22 <= (self.start + timedelta(minutes=m)).hour or (self.start + timedelta(minutes=m)).hour < 8 for m, _ in events))
        self.assertGreaterEqual(next(m for m, _ in events if m > 600), 689)

    def test_disabled_and_snoozed_do_not_burst(self):
        self.scheduler.snooze(0, 60)
        self.assertEqual(self.advance(119), [])
        self.assertEqual(self.scheduler.tick(120 * 60, self.start + timedelta(minutes=120)).kind, 'break')
        self.scheduler.set_settings(replace(self.settings, enabled=False), 121 * 60)
        for minute in range(122, 200):
            self.assertIsNone(self.scheduler.tick(minute * 60, self.start + timedelta(minutes=minute)))

    def test_busy_suppresses_without_claiming_to_measure_activity(self):
        self.assertEqual(self.advance(100, available=False), [])
        first = self.scheduler.tick(101 * 60, self.start + timedelta(minutes=101), available=True)
        self.assertEqual(first.kind, 'break')
        self.assertIsNone(self.scheduler.tick(102 * 60, self.start + timedelta(minutes=102)))

    def test_suspend_resets_pending_reminders(self):
        self.scheduler.tick(60, self.start + timedelta(minutes=1))
        self.assertIsNone(self.scheduler.tick(10000, self.start + timedelta(hours=3)))
        self.assertIsNone(self.scheduler.tick(10060, self.start + timedelta(hours=3, minutes=1)))

    def test_meals_once_daily_and_never_immediately_at_start(self):
        self.settings = replace(self.settings, meals=True, breaks=False, water=False)
        self.scheduler = CompanionScheduler(0, self.settings)
        self.start = datetime(2026, 10, 28, 11, 50)
        events = self.advance(200)
        self.assertEqual([e.kind for _, e in events], ['lunch'])
        self.assertGreaterEqual(events[0][0], 20)
        self.assertIn('如果', self.scheduler.MESSAGES['lunch'][1])

    def test_settings_changes_restart_initial_delay(self):
        self.advance(60)
        self.scheduler.set_settings(replace(self.settings, break_minutes=15), 3600)
        self.assertIsNone(self.scheduler.tick(3660, self.start + timedelta(minutes=61)))


class SettingsTests(unittest.TestCase):
    def test_temporary_file_is_private_from_creation_and_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CompanionSettingsStore(Path(folder) / 'prefs.json')
            previous = replace(CompanionSettings(), water_minutes=45)
            store.save(previous)
            names = []

            def fail_while_writing(value, stream, **kwargs):
                temporary, = Path(folder).glob('.prefs.json.*.tmp')
                names.append(temporary.name)
                self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o077, 0)
                stream.write('{')
                raise OSError('simulated write failure')

            with mock.patch('companion.json.dump', side_effect=fail_while_writing):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, 'simulated write failure'):
                        store.save(replace(previous, water_minutes=60))
            self.assertNotEqual(names[0], names[1])
            self.assertEqual(store.load(), previous)
            self.assertEqual([path.name for path in Path(folder).iterdir()], ['prefs.json'])

    def test_round_trip_and_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CompanionSettingsStore(Path(folder) / 'prefs.json')
            self.assertEqual(store.load(), CompanionSettings())
            settings = replace(store.load(), water_minutes=45, quiet_end='07:30')
            store.save(settings)
            self.assertEqual(store.load(), settings)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                store.save(replace(settings, break_minutes=0))
            self.assertEqual(store.load(), settings)
            with self.assertRaises(ValueError):
                store.save(replace(settings, quiet_start='25:00'))
            with self.assertRaises(ValueError):
                store.save(replace(settings, weather_enabled=True))

    def test_unrecognized_settings_do_not_break_forward_compatibility(self):
        self.assertTrue(CompanionSettings.from_dict({'enabled': True, 'future_key': 5}).enabled)


if __name__ == '__main__':
    unittest.main()
