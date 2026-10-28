import os
import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5 import sip
from companion import CompanionSettings
from companion_settings_ui import CompanionSettingsDialog


class FakeWeather:
    def __init__(self):
        self.calls = []
        self.callback = None

    def search(self, query, callback):
        self.calls.append(query)
        self.callback = callback


class CompanionDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.saved, self.snoozed = [], []
        self.weather = FakeWeather()
        self.dialog = CompanionSettingsDialog(CompanionSettings(), self.saved.append, self.snoozed.append,
                                               weather_client=self.weather)

    def tearDown(self):
        if not sip.isdeleted(self.dialog):
            self.dialog.close()
            self.dialog.deleteLater()
        self.app.processEvents()

    def test_save_controls_and_privacy_explanation(self):
        self.dialog.water_spin.setValue(45)
        self.dialog.break_check.setChecked(False)
        self.dialog.meals.setChecked(False)
        self.dialog.quiet_start.setTime(self.dialog._time('23:00'))
        self.dialog._save()
        self.assertEqual(self.dialog.result(), QDialog.Accepted)
        self.assertEqual(self.saved[0].water_minutes, 45)
        self.assertFalse(self.saved[0].breaks)
        self.assertFalse(self.saved[0].meals)
        self.assertEqual(self.saved[0].quiet_start, '23:00')
        self.assertFalse(self.saved[0].weather_enabled)
        self.assertEqual(self.weather.calls, [])

    def test_snooze_does_not_save_unsaved_fields(self):
        self.dialog.water_spin.setValue(50)
        self.dialog._snooze()
        self.assertEqual(self.snoozed, [60])
        self.assertEqual(self.saved, [])

    def test_weather_requires_selection_and_search_uses_qt_signal(self):
        self.dialog.weather_enabled.setChecked(True)
        self.dialog._save()
        self.assertEqual(self.saved, [])
        self.assertIn('选择城市', self.dialog.error.text())
        self.dialog.city.setText('Paris')
        self.dialog._search()
        self.assertEqual(self.weather.calls, ['Paris'])
        place = {'name': 'Paris', 'admin1': 'Ile-de-France', 'country': 'France', 'latitude': 48.85, 'longitude': 2.35}
        worker = threading.Thread(target=lambda: self.weather.callback('locations', [place]))
        worker.start()
        worker.join()
        QTest.qWait(25)
        self.assertEqual(self.dialog.results.count(), 2)
        self.dialog.results.setCurrentIndex(1)
        self.dialog._save()
        self.assertEqual(self.saved[0].weather_label, 'Paris · Ile-de-France · France')
        self.assertEqual(self.saved[0].weather_latitude, 48.85)
        self.assertTrue(self.saved[0].weather_enabled)

    def test_disabled_weather_ignores_late_search(self):
        self.dialog.weather_enabled.setChecked(True)
        self.dialog.city.setText('Paris')
        self.dialog._search()
        self.dialog.weather_enabled.setChecked(False)
        self.weather.callback('locations', [{'name': 'Paris', 'latitude': 1, 'longitude': 2}])
        self.app.processEvents()
        self.assertEqual(self.dialog.results.count(), 1)
        self.dialog._save()
        self.assertFalse(self.saved[0].weather_enabled)

    def test_save_error_stays_open_and_preserves_original(self):
        def fail(settings):
            raise OSError('磁盘错误')
        self.dialog.save_callback = fail
        self.dialog._save()
        self.assertIn('磁盘错误', self.dialog.error.text())
        self.assertEqual(self.dialog.result(), QDialog.Rejected)
        self.assertEqual(self.saved, [])

    def test_new_search_requires_new_explicit_selection(self):
        old = replace(CompanionSettings(), weather_enabled=True, weather_label='Paris · France',
                      weather_latitude=48.85, weather_longitude=2.35)
        self.dialog.close()
        self.dialog = CompanionSettingsDialog(old, self.saved.append, self.snoozed.append,
                                               weather_client=self.weather)
        self.dialog.city.setText('Tokyo')
        self.dialog._search()
        self.weather.callback('locations', [{'name': 'Tokyo', 'country': 'Japan', 'latitude': 35.68, 'longitude': 139.76}])
        self.app.processEvents()
        self.dialog._save()
        self.assertEqual(self.saved, [])
        self.assertIn('选择城市', self.dialog.error.text())
        self.dialog.results.setCurrentIndex(1)
        self.dialog._save()
        self.assertEqual(self.saved[0].weather_label, 'Tokyo · Japan')
        self.assertEqual(self.saved[0].weather_latitude, 35.68)

    def test_city_search_hints_and_explicit_selection_for_namesakes(self):
        self.dialog.weather_enabled.setChecked(True)
        self.assertIn('拼音', self.dialog.city.placeholderText())
        self.assertIn('Dalian', self.dialog.city.placeholderText())
        self.dialog.city.setText('Dalian')
        self.dialog._search()
        self.assertEqual(self.weather.calls, ['Dalian'])
        places = [
            {'name': 'Dalian', 'admin1': 'Liaoning', 'country': 'China', 'latitude': 38.91, 'longitude': 121.61},
            {'name': 'Dalian', 'admin1': 'Other', 'country': 'Country', 'latitude': 1.0, 'longitude': 2.0},
        ]
        self.weather.callback('locations', places)
        self.app.processEvents()
        self.assertEqual(self.dialog.results.currentIndex(), 0)
        self.assertIsNone(self.dialog.location)
        self.assertIn('Liaoning', self.dialog.results.itemText(1))
        self.assertIn('China', self.dialog.results.itemText(1))
        self.assertIn('Other', self.dialog.results.itemText(2))
        self.assertIn('地区和国家', self.dialog.weather_status.text())
        self.dialog._save()
        self.assertEqual(self.saved, [])
        self.dialog.results.setCurrentIndex(2)
        self.dialog._save()
        self.assertEqual(self.saved[0].weather_latitude, 1.0)

    def test_late_worker_after_dialog_destroyed_is_safe(self):
        self.dialog.weather_enabled.setChecked(True)
        self.dialog.city.setText('Paris')
        self.dialog._search()
        callback = self.weather.callback
        self.dialog.reject()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(sip.isdeleted(self.dialog))
        errors = []
        def deliver():
            try:
                callback('locations', [{'name': 'Paris', 'latitude': 1, 'longitude': 2}])
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=deliver)
        worker.start()
        worker.join()
        self.app.processEvents()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
