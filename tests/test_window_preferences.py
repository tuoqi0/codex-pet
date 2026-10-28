import json
import os
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from window_preferences import (PetWindowPreference, WindowPreferences, WindowPreferencesStore,
                                ensure_visible, remember_position, restored_position, set_visible)


class WindowPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'windows.json'
        self.store = WindowPreferencesStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_independent_round_trip(self):
        prefs, warning = self.store.load()
        self.assertIsNone(warning)
        self.assertFalse(self.path.exists())
        prefs = replace(prefs, dragon=PetWindowPreference(-100, 500, .7875, True),
                        qpet=PetWindowPreference(1800, -50, .75, False), dragon_patrol=True)
        self.store.save(prefs)
        self.assertEqual(self.store.load(), (prefs, None))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(self.path.read_text()).keys()),
                         {'version', 'dragon', 'qpet', 'dragon_patrol'})

    def test_bad_file_is_retained_and_error_is_visible(self):
        for contents in ('{bad json', '{"version": 1, "dragon": {"scale": 999}}',
                         '{"version": 1, "dragon": {"x": 10}}',
                         '{"version": 1, "dragon": {"visible": false}, "qpet": {"visible": false}}',
                         '{"version": 1, "dragon_patrol": "true"}', '{"version": true}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                prefs, warning = self.store.load()
                self.assertEqual(prefs, WindowPreferences())
                self.assertIn('原文件已保留', warning)
                self.assertEqual(self.path.read_text(), contents)

    def test_failed_save_preserves_previous_file_and_cleans_private_temp(self):
        original = WindowPreferences()
        self.store.save(original)
        names = []

        def fail(_data, stream, **_kwargs):
            temp, = Path(self.temp.name).glob('.windows.json.*.tmp')
            names.append(temp.name)
            self.assertEqual(os.fstat(stream.fileno()).st_mode & 0o077, 0)
            stream.write('{')
            raise OSError('simulated failure')

        with mock.patch('window_preferences.json.dump', side_effect=fail):
            for _ in range(2):
                with self.assertRaises(OSError):
                    self.store.save(replace(original, dragon_patrol=True))
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(self.store.load(), (original, None))
        self.assertEqual([p.name for p in Path(self.temp.name).iterdir()], ['windows.json'])

    def test_explicit_save_backs_up_damaged_configuration(self):
        self.path.write_bytes(b'{broken')
        prefs, warning = self.store.load()
        self.assertIsNotNone(warning)
        self.assertEqual(self.path.read_bytes(), b'{broken')
        self.store.save(replace(prefs, dragon_patrol=True))
        self.assertTrue(self.store.load()[0].dragon_patrol)
        self.assertEqual(self.store.backup_path.read_bytes(), b'{broken')
        self.assertEqual(self.store.backup_path.stat().st_mode & 0o777, 0o600)

    def test_at_least_one_available_character_stays_visible(self):
        prefs = WindowPreferences()
        prefs = set_visible(prefs, 'dragon', False, {'dragon', 'qpet'})
        with self.assertRaises(ValueError):
            set_visible(prefs, 'qpet', False, {'dragon', 'qpet'})
        restored = ensure_visible(prefs, {'dragon'})  # Q assets unavailable
        self.assertTrue(restored.dragon.visible)
        with self.assertRaises(ValueError):
            set_visible(restored, 'dragon', False, {'dragon'})
        self.assertFalse(prefs.dragon.visible)  # loading adjustments aren't implicitly saved

    def test_wayland_preserves_but_never_restores_x11_coordinates(self):
        dragon = PetWindowPreference(100, 200, .63)
        self.assertEqual(remember_position(dragon, 500, 600, 'wayland-egl'), dragon)
        self.assertIsNone(restored_position(dragon, (300, 190), [(0, 0, 800, 600)], 'wayland'))
        moved = remember_position(dragon, -100, 250, 'xcb')
        self.assertEqual((moved.x, moved.y), (-100, 250))

    def test_clamps_after_monitor_or_scale_changes_using_nearest_work_area(self):
        areas = [(-1920, 0, 1920, 1080), (0, 0, 1280, 720)]
        self.assertEqual(restored_position(PetWindowPreference(-50, 50), (300, 190), areas, 'xcb'), (0, 50))
        self.assertEqual(restored_position(PetWindowPreference(5000, 400), (300, 190), areas, 'xcb'), (980, 400))
        self.assertEqual(restored_position(PetWindowPreference(-2500, 990), (300, 190), areas, 'xcb'), (-1920, 890))
        self.assertEqual(restored_position(PetWindowPreference(1100, 650), (400, 250), areas, 'xcb'), (880, 470))
        self.assertEqual(restored_position(PetWindowPreference(1100, 650), (2000, 1000), areas, 'xcb'), (0, 0))
        self.assertIsNone(restored_position(PetWindowPreference(), (300, 190), areas, 'xcb'))
        self.assertIsNone(restored_position(PetWindowPreference(0, 0), (300, 190), [], 'xcb'))


if __name__ == '__main__':
    unittest.main()
