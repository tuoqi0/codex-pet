import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PyQt5 import sip
from PyQt5.QtGui import QFontDatabase, QTextCursor, QTextFormat
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import pet_panel


class MarkdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stream_splits_and_history_compare_raw(self):
        transcript = pet_panel.ChatTranscript()
        transcript.show()
        transcript.clear()
        transcript.start_exchange('**用户原文** <b>x</b>')
        for part in ('# 标', '题\n\n这段 ', '**加', '粗**\n\n```py', 'thon\nprint(1)', '\n```'):
            transcript.append_response(part)
        QTest.qWait(100)
        bubble = transcript.bubbles[-1]
        self.assertIn('标题', bubble.toPlainText())
        self.assertIn('print(1)', bubble.toPlainText())
        self.assertEqual(transcript.bubbles[-2].toPlainText(), '**用户原文** <b>x</b>')
        self.assertIn('**加粗**', bubble.raw_text)
        self.assertNotIn('**加粗**', bubble.toPlainText())
        block = bubble.document().begin()
        fences = []
        while block.isValid():
            if block.blockFormat().hasProperty(QTextFormat.BlockCodeFence):
                fences.append(block)
            block = block.next()
        self.assertTrue(fences)
        self.assertTrue(fences[0].blockFormat().background().color().isValid())
        first_fragment = fences[0].begin().fragment()
        self.assertTrue(QFontDatabase().isFixedPitch(first_fragment.charFormat().fontFamily()))
        before = bubble
        transcript.replace_messages_preserving_scroll([('user', '**用户原文** <b>x</b>'), ('assistant', bubble.raw_text)])
        self.assertIs(transcript.bubbles[-1], before)
        transcript.close()

    def test_history_append_preserves_selection_and_nonappend_waits_for_latest(self):
        transcript = pet_panel.ChatTranscript()
        transcript.clear()
        old = transcript.add_message('assistant', '**选中的原文**')
        cursor = old.textCursor()
        cursor.select(QTextCursor.Document)
        old.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([
            ('assistant', '**选中的原文**'), ('assistant', '追加消息')])
        self.assertIs(transcript.bubbles[0], old)
        self.assertEqual(old.textCursor().selectedText(), '选中的原文')
        transcript.replace_messages_preserving_scroll([('assistant', '第一次修订')])
        transcript.replace_messages_preserving_scroll([('assistant', '最新修订')])
        self.assertIs(transcript.bubbles[0], old)
        self.assertEqual(old.textCursor().selectedText(), '选中的原文')
        cursor.clearSelection()
        old.setTextCursor(cursor)
        QTest.qWait(20)
        self.assertEqual(transcript.toPlainText(), '最新修订')
        transcript.close()

    def test_deferred_history_is_discarded_when_cleared_or_new_exchange_starts(self):
        transcript = pet_panel.ChatTranscript()
        transcript.clear()
        old = transcript.add_message('assistant', '先前消息')
        cursor = old.textCursor()
        cursor.select(QTextCursor.Document)
        old.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([('assistant', '旧对话延迟刷新')])
        transcript.clear()
        transcript.add_message('assistant', '新对话')
        QTest.qWait(20)
        self.assertEqual(transcript.toPlainText(), '新对话')
        current = transcript.bubbles[0]
        cursor = current.textCursor()
        cursor.select(QTextCursor.Document)
        current.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([('assistant', '已过期的刷新')])
        transcript.start_exchange('新的请求')
        cursor.clearSelection()
        current.setTextCursor(cursor)
        QTest.qWait(20)
        self.assertIn('新的请求', transcript.toPlainText())
        self.assertNotIn('已过期的刷新', transcript.toPlainText())
        transcript.close()

    def test_deferred_callback_after_transcript_destruction_is_ignored(self):
        transcript = pet_panel.ChatTranscript()
        transcript.clear()
        bubble = transcript.add_message('assistant', '原始')
        cursor = bubble.textCursor()
        cursor.select(QTextCursor.Document)
        bubble.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([('assistant', '更新')])
        cursor.clearSelection()
        bubble.setTextCursor(cursor)
        sip.delete(transcript)
        QTest.qWait(20)

    def test_newer_unchanged_snapshot_cancels_older_pending_replacement(self):
        transcript = pet_panel.ChatTranscript()
        transcript.clear()
        old = transcript.add_message('assistant', '当前')
        cursor = old.textCursor()
        cursor.select(QTextCursor.Document)
        old.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([('assistant', '过期')])
        transcript.replace_messages_preserving_scroll([('assistant', '当前')])
        cursor.clearSelection()
        old.setTextCursor(cursor)
        QTest.qWait(20)
        self.assertIs(transcript.bubbles[0], old)
        self.assertEqual(transcript.toPlainText(), '当前')
        transcript.close()

    def test_selection_defers_render_until_finished(self):
        bubble = pet_panel.MessageBubble('hello', 'assistant')
        c = bubble.textCursor()
        c.select(QTextCursor.Document)
        bubble.setTextCursor(c)
        bubble.append_text(' **world**')
        QTest.qWait(100)
        self.assertEqual(bubble.raw_text, 'hello **world**')
        self.assertEqual(bubble.toPlainText(), 'hello')
        c.clearSelection()
        bubble.setTextCursor(c)
        QTest.qWait(100)
        self.assertEqual(bubble.toPlainText(), 'hello world')

    def test_html_and_resources_are_not_executed_or_loaded(self):
        calls = []
        original = pet_panel._MessageDocument.loadResource
        def audited(document, kind, url):
            result = original(document, kind, url)
            calls.append((url.toString(), result))
            return result
        raw = '<b>literal</b> <img src="https://example.invalid/x">\n\n![remote](https://example.invalid/probe)'
        with mock.patch.object(pet_panel._MessageDocument, 'loadResource', audited):
            bubble = pet_panel.MessageBubble(raw, 'assistant')
            bubble.resize(400, 300)
            bubble.show()
            QTest.qWait(150)
            bubble.grab()
            self.assertIn('<b>literal</b>', bubble.toPlainText())
            self.assertIn('&lt;img', bubble.toHtml())
            self.assertTrue(any(url == 'https://example.invalid/probe' for url, result in calls))
            self.assertTrue(all(result is None for url, result in calls))
            bubble.close()

    def test_older_qt_falls_back_to_literal(self):
        with mock.patch.object(pet_panel, '_markdown_features', return_value=None):
            bubble = pet_panel.MessageBubble('**字面文本**', 'assistant')
            self.assertEqual(bubble.toPlainText(), '**字面文本**')
            bubble.append_text(' 继续')
            QTest.qWait(100)
            self.assertEqual(bubble.toPlainText(), '**字面文本** 继续')


if __name__ == '__main__':
    unittest.main()
