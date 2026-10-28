import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('desktop_entry', ROOT / 'opening/desktop_entry.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
splash_spec = importlib.util.spec_from_file_location('desktop_splash', ROOT / 'opening/desktop_splash.py')
splash = importlib.util.module_from_spec(splash_spec)
splash_spec.loader.exec_module(splash)


class DesktopOpeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = self.root / 'applications/chatgpt.desktop'
        self.record = self.root / 'codex-pet/desktop-opening.json'
        self.source = self.root / 'system.desktop'
        self.source.write_text('[Desktop Entry]\nName=ChatGPT\nType=Application\nExec=chatgpt %U\nMimeType=x-scheme-handler/codex;\n', encoding='utf-8')
        self.launcher = ROOT / 'opening/desktop_splash.py'
        self.output = self.root / 'received.json'
        self.client = self.root / 'client $%\' spaced'
        self.client.write_text('#!/usr/bin/python3\nimport json,sys\nopen(' + repr(str(self.output)) + ', "w").write(json.dumps(sys.argv[1:]))\n', encoding='utf-8')
        self.client.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def install(self, launcher=None):
        entry.install(self.source, self.target, self.record, launcher or self.launcher, self.client)

    def test_round_trip_and_desktop_entry_valid(self):
        self.install()
        if shutil.which('desktop-file-validate'):
            subprocess.run(['desktop-file-validate', str(self.target)], check=True, capture_output=True)
        text = self.target.read_text(encoding='utf-8')
        self.assertIn(' codex-pet %U', text)
        for line in self.source.read_text(encoding='utf-8').splitlines():
            if not line.startswith('Exec='):
                self.assertIn(line, text)
        entry.remove(self.target, self.record)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.record.exists())

    def test_existing_entry_untouched(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('user entry', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'user entry')

    def test_changed_entry_untouched(self):
        self.install()
        self.target.write_text('changed by user', encoding='utf-8')
        with self.assertRaises(ValueError):
            entry.remove(self.target, self.record)
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'changed by user')
        self.assertTrue(self.record.exists())

    def test_interrupted_install_recovery(self):
        self.install()
        self.target.unlink()
        entry.remove(self.target, self.record)
        self.assertFalse(self.record.exists())

    def test_symlink_not_followed(self):
        self.target.parent.mkdir(parents=True)
        real = self.root / 'other'
        real.write_text('safe', encoding='utf-8')
        self.target.symlink_to(real)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(real.read_text(encoding='utf-8'), 'safe')

    def test_unknown_source_format_rejected(self):
        self.source.write_text('[Desktop Entry]\nExec=unknown %U\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.install()

    def test_splash_passes_flag_spaces_and_urls(self):
        arguments = ['-leading', 'codex://open?x=1&y=2', 'https://example.test/a b', '--flag']
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        proc = subprocess.run(['/usr/bin/python3', str(self.launcher), '--command', str(self.client), '--', *arguments], env=env, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), arguments)

    def test_sprite_has_expected_frames_and_timeline(self):
        from PIL import Image
        with Image.open(splash.asset_path('opening-sprite.png')) as sprite:
            width, height = splash.sprite_geometry(*sprite.size)
            frames = [hashlib.sha256(sprite.crop(((i % 8) * width, (i // 8) * height,
                                   (i % 8 + 1) * width, (i // 8 + 1) * height)).tobytes()).digest()
                      for i in range(32)]
            self.assertGreater(len(set(frames)), 4)
        self.assertEqual(splash.sprite_geometry(8 * 462, 4 * 261), (462, 261))
        self.assertEqual(splash.sprite_geometry(8 * 924, 4 * 522), (924, 522))
        for size in ((0, 0), (7391, 2088), (7392, 2092)):
            with self.assertRaises(ValueError):
                splash.sprite_geometry(*size)
        self.assertEqual(splash.sprite_frame(0), 0)
        self.assertEqual(splash.sprite_frame(1.65), 16)
        self.assertEqual(splash.sprite_frame(3.15), 31)
        self.assertEqual(splash.sprite_frame(3.65), 31)

    def test_missing_sprite_still_starts_client(self):
        folder = self.root / 'opening'
        folder.mkdir()
        copy = folder / 'desktop_splash.py'
        shutil.copyfile(self.launcher, copy)
        arguments = ['codex://open?x=1&y=2', 'https://example.test/a b']
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        proc = subprocess.run(['/usr/bin/python3', str(copy), '--command', str(self.client), '--', *arguments], env=env, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('无法显示开场', proc.stderr.decode())
        for _ in range(50):
            if self.output.exists():
                break
            time.sleep(.1)
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), arguments)

    def test_invalid_sprite_still_starts_client(self):
        from PIL import Image
        folder = self.root / 'opening'
        folder.mkdir()
        copy = folder / 'desktop_splash.py'
        shutil.copyfile(self.launcher, copy)
        Image.new('RGBA', (16, 16)).save(self.root / 'opening-sprite.png')
        arguments = ['codex://invalid-asset?x=1']
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        proc = subprocess.run(['/usr/bin/python3', str(copy), '--command', str(self.client), '--', *arguments], env=env, capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('无法显示开场', proc.stderr.decode())
        for _ in range(50):
            if self.output.exists():
                break
            time.sleep(.1)
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), arguments)

    def test_client_starts_when_qt_import_fails(self):
        # -S removes Debian dist-packages from sys.path, simulating a missing Qt.
        probe = subprocess.run(['/usr/bin/python3', '-S', '-c', 'import PyQt5'], capture_output=True)
        self.assertNotEqual(probe.returncode, 0, 'test requires an unavailable Qt module')
        arguments = ['codex://open?x=1&y=2', 'https://example.test/a b']
        proc = subprocess.run(['/usr/bin/python3', '-S', str(self.launcher), '--command', str(self.client), '--', *arguments], capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('无法显示开场', proc.stderr.decode())
        for _ in range(50):
            if self.output.exists():
                break
            time.sleep(.1)
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), arguments)

    def launch_with_gio(self, arguments):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        script = ('import sys; from gi.repository import Gio; '
                  'app = Gio.DesktopAppInfo.new_from_filename(sys.argv[1]); '
                  'assert app is not None; assert app.launch_uris(sys.argv[2:], None)')
        result = subprocess.run(['/usr/bin/python3', '-c', script, str(self.target), *arguments], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for _ in range(50):
            if self.output.exists():
                break
            time.sleep(.1)
        self.assertTrue(self.output.exists(), '客户端未启动')
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), arguments)

    def test_gio_preserves_multiple_urls_and_escaping(self):
        self.install()
        self.launch_with_gio(['-leading', 'codex://open?x=1&y=2', 'https://example.test/a b', 'https://example.test/c%20d'])

    def test_missing_splash_falls_back_to_client(self):
        copy = self.root / 'splash.py'
        shutil.copyfile(self.launcher, copy)
        self.install(copy)
        copy.unlink()
        self.launch_with_gio(['codex://open?x=1&y=2', 'https://example.test/a%20b'])


if __name__ == '__main__':
    unittest.main()
