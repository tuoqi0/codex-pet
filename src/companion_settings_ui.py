"""Editable, local companion preferences; network weather is opt-in."""
from dataclasses import replace
import weakref

from PyQt5.QtCore import QObject, QTime, Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                             QSpinBox, QTimeEdit, QVBoxLayout, QWidget)

from weather import WeatherClient


STYLE = '''
QDialog, QWidget#content { background: #f7f5ff; color: #35304f; font-family: "Noto Sans CJK SC", "Sans Serif"; font-size: 13px; }
QLabel#title { font-size: 23px; font-weight: 700; color: #30284d; }
QLabel#subtitle, QLabel#hint { color: #77728f; font-size: 12px; }
QLabel#section { font-size: 14px; font-weight: 700; color: #4d426b; }
QLabel#error { color: #b03d63; }
QFrame#card { background: white; border: 1px solid #e9e5f8; border-radius: 14px; }
QLineEdit, QSpinBox, QTimeEdit, QComboBox { background: #faf9ff; border: 1px solid #ded9ef; border-radius: 7px; padding: 5px 8px; min-height: 22px; color: #3e3759; }
QCheckBox { spacing: 8px; color: #514a68; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #c5bfd9; border-radius: 4px; background: white; }
QCheckBox::indicator:checked { background: #9381d7; border: 1px solid #9381d7; }
QPushButton { background: #eeeafd; color: #65528d; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #e3dbfb; }
QPushButton#primary { background: #8c79d2; color: white; }
QPushButton#primary:hover { background: #7c69c2; }
QPushButton:disabled { background: #efedf5; color: #b7b3c4; }
QScrollArea { background: transparent; border: none; }
'''


class WeatherBridge(QObject):
    received = pyqtSignal(int, str, object)


class CompanionSettingsDialog(QDialog):
    """Call save_callback(settings) and snooze_callback(minutes) on user action."""
    def __init__(self, settings, save_callback, snooze_callback, parent=None, weather_client=None):
        super().__init__(parent)
        self.settings = settings
        self.save_callback = save_callback
        self.snooze_callback = snooze_callback
        self.weather = weather_client or WeatherClient()
        self.bridge = WeatherBridge(self)
        self.bridge.received.connect(self._weather_result)
        self.closed = False
        self.request_id = 0
        self.location = None
        if settings.weather_latitude is not None and settings.weather_longitude is not None:
            self.location = {'name': settings.weather_label, 'latitude': settings.weather_latitude,
                             'longitude': settings.weather_longitude}
        self.setWindowTitle('陪伴设置')
        self.setStyleSheet(STYLE)
        self.resize(540, 690)
        self.setMinimumSize(420, 450)
        self._build()

    @staticmethod
    def _time(value):
        return QTime.fromString(value, 'HH:mm')

    @staticmethod
    def _card():
        card = QFrame()
        card.setObjectName('card')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.setSpacing(10)
        return card, layout

    @staticmethod
    def _section(text):
        label = QLabel(text)
        label.setObjectName('section')
        return label

    @staticmethod
    def _hint(text):
        label = QLabel(text)
        label.setObjectName('hint')
        label.setWordWrap(True)
        return label

    def _time_edit(self, value):
        edit = QTimeEdit(self._time(value))
        edit.setDisplayFormat('HH:mm')
        return edit

    def _row(self, layout, label, widget, extra=None):
        row = QHBoxLayout()
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(widget)
        if extra:
            row.addWidget(extra)
        layout.addLayout(row)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        header = QLabel('让陪伴刚刚好')
        header.setObjectName('title')
        outer.addWidget(header)
        sub = self._hint('由你决定提醒的节奏，安静时也可以随时暂停。')
        sub.setObjectName('subtitle')
        outer.addWidget(sub)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName('content')
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 8, 0, 8)
        body.setSpacing(10)

        card, layout = self._card()
        layout.addWidget(self._section('日常陪伴'))
        self.enabled = QCheckBox('开启主动陪伴提醒')
        self.enabled.setChecked(self.settings.enabled)
        layout.addWidget(self.enabled)
        layout.addWidget(self._hint('按照桌宠运行时间计时，不监控键盘鼠标，也不判断你是否工作、喝水或吃饭。'))
        for key, label in (('break', '起身休息'), ('water', '喝水提醒'), ('greeting', '轻声问候')):
            check = QCheckBox(label)
            check.setChecked(getattr(self.settings, 'breaks' if key == 'break' else 'greetings' if key == 'greeting' else key))
            spinner = QSpinBox()
            spinner.setRange(15, 1440)
            spinner.setSuffix(' 分钟')
            spinner.setValue(getattr(self.settings, key + '_minutes'))
            setattr(self, key + '_check', check)
            setattr(self, key + '_spin', spinner)
            check.toggled.connect(spinner.setEnabled)
            spinner.setEnabled(check.isChecked())
            self._row(layout, check, spinner)
        self.meals = QCheckBox('用餐时间提醒')
        self.meals.setChecked(self.settings.meals)
        layout.addWidget(self.meals)
        self.lunch = self._time_edit(self.settings.lunch_time)
        self.dinner = self._time_edit(self.settings.dinner_time)
        self._row(layout, QLabel('午餐时间'), self.lunch)
        self._row(layout, QLabel('晚餐时间'), self.dinner)
        self.meals.toggled.connect(self.lunch.setEnabled)
        self.meals.toggled.connect(self.dinner.setEnabled)
        self.lunch.setEnabled(self.meals.isChecked())
        self.dinner.setEnabled(self.meals.isChecked())
        body.addWidget(card)

        card, layout = self._card()
        layout.addWidget(self._section('安静时间'))
        row = QHBoxLayout()
        self.quiet_start = self._time_edit(self.settings.quiet_start)
        self.quiet_end = self._time_edit(self.settings.quiet_end)
        row.addWidget(QLabel('每天'))
        row.addWidget(self.quiet_start)
        row.addWidget(QLabel('至'))
        row.addWidget(self.quiet_end)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self._hint('这段时间不主动打扰；开始与结束相同表示不设置安静时间。'))
        body.addWidget(card)

        card, layout = self._card()
        layout.addWidget(self._section('天气 · 可选'))
        self.weather_enabled = QCheckBox('启用所选地点的天气')
        self.weather_enabled.setChecked(self.settings.weather_enabled)
        layout.addWidget(self.weather_enabled)
        layout.addWidget(self._hint('主动搜索并选择城市后才会联网查询；不会自动定位。中文结果不完整时可试英文或拼音，例如 Dalian；请核对地区和国家。'))
        row = QHBoxLayout()
        self.city = QLineEdit()
        self.city.setPlaceholderText('城市名、英文或拼音（如 Dalian）')
        self.search_button = QPushButton('搜索')
        self.search_button.clicked.connect(self._search)
        row.addWidget(self.city, 1)
        row.addWidget(self.search_button)
        layout.addLayout(row)
        self.results = QComboBox()
        self.results.addItem('请选择搜索结果', None)
        self.results.currentIndexChanged.connect(self._select_location)
        layout.addWidget(self.results)
        self.weather_status = self._hint('当前地点：' + (self.settings.weather_label or '尚未选择'))
        layout.addWidget(self.weather_status)
        self.weather_enabled.toggled.connect(self._set_weather_controls)
        self._set_weather_controls(self.weather_enabled.isChecked())
        body.addWidget(card)
        body.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.error = QLabel('')
        self.error.setObjectName('error')
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        actions = QHBoxLayout()
        snooze = QPushButton('暂停 1 小时')
        snooze.clicked.connect(self._snooze)
        actions.addWidget(snooze)
        actions.addStretch(1)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        save = QPushButton('保存设置')
        save.setObjectName('primary')
        save.clicked.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        outer.addLayout(actions)

    def _set_weather_controls(self, enabled):
        self.city.setEnabled(enabled)
        self.search_button.setEnabled(enabled)
        self.results.setEnabled(enabled)
        if not enabled:
            self.request_id += 1

    def _search(self):
        query = self.city.text().strip()
        if len(query) < 2:
            self.weather_status.setText('请输入至少两个字符的城市名称。')
            return
        self.request_id += 1
        ident = self.request_id
        # A new search is a new choice. Never silently persist the old city
        # while the result list asks the user to select a different one.
        self.location = None
        self.results.blockSignals(True)
        self.results.clear()
        self.results.addItem('请选择搜索结果', None)
        self.results.blockSignals(False)
        self.search_button.setEnabled(False)
        self.weather_status.setText('正在搜索城市…')
        bridge_ref = weakref.ref(self.bridge)

        def deliver(event, data):
            bridge = bridge_ref()
            if bridge is not None:
                try:
                    bridge.received.emit(ident, event, data)
                except RuntimeError:
                    # The application can destroy the Qt object between the
                    # weak-reference lookup and delivery from this worker.
                    pass

        self.weather.search(query, deliver)

    def _weather_result(self, ident, event, data):
        if self.closed or ident != self.request_id or not self.weather_enabled.isChecked():
            return
        self.search_button.setEnabled(True)
        if event == 'error':
            self.weather_status.setText(data.get('message', '天气服务暂时不可用。'))
        elif event == 'locations':
            self.results.blockSignals(True)
            self.results.clear()
            self.results.addItem('请选择搜索结果', None)
            for place in data:
                label = ' · '.join(str(place.get(key, '')) for key in ('name', 'admin1', 'country') if place.get(key))
                self.results.addItem(label, place)
            self.results.blockSignals(False)
            self.weather_status.setText('请核对地区和国家后选择地点。' if data else '未找到匹配城市，可试英文或拼音。')

    def _select_location(self, index):
        place = self.results.itemData(index)
        if place is not None:
            self.location = dict(place)
            self.weather_status.setText('已选择：' + self.results.itemText(index))

    def _save(self):
        location = self.location
        if self.weather_enabled.isChecked() and location is None:
            self.error.setText('请先搜索并选择城市，或关闭天气。')
            return
        selected_label = ''
        if location:
            selected_label = ' · '.join(str(location.get(key, '')) for key in ('name', 'admin1', 'country') if location.get(key))
        settings = replace(self.settings, enabled=self.enabled.isChecked(),
                           breaks=self.break_check.isChecked(), water=self.water_check.isChecked(),
                           greetings=self.greeting_check.isChecked(), meals=self.meals.isChecked(),
                           break_minutes=self.break_spin.value(), water_minutes=self.water_spin.value(),
                           greeting_minutes=self.greeting_spin.value(),
                           lunch_time=self.lunch.time().toString('HH:mm'), dinner_time=self.dinner.time().toString('HH:mm'),
                           quiet_start=self.quiet_start.time().toString('HH:mm'), quiet_end=self.quiet_end.time().toString('HH:mm'),
                           weather_enabled=self.weather_enabled.isChecked(), weather_label=selected_label,
                           weather_latitude=location.get('latitude') if location else None,
                           weather_longitude=location.get('longitude') if location else None)
        try:
            settings.validate()
            self.save_callback(settings)
        except (OSError, ValueError) as exc:
            self.error.setText('无法保存设置：' + str(exc))
            return
        self.accept()

    def _snooze(self):
        try:
            self.snooze_callback(60)
        except (OSError, ValueError) as exc:
            self.error.setText('无法暂停提醒：' + str(exc))
            return
        self.accept()

    def done(self, result):
        if not self.closed:
            self.closed = True
            self.bridge.received.disconnect(self._weather_result)
        super().done(result)
        self.deleteLater()
