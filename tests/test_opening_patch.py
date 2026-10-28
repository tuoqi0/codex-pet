import contextlib, hashlib, importlib.util, io, json, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
spec=importlib.util.spec_from_file_location('opening_patch',Path(__file__).resolve().parents[1]/'opening/patch.py'); patch=importlib.util.module_from_spec(spec); spec.loader.exec_module(patch)
class PatchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='opening-test-'); self.root=Path(self.temp.name); (self.root/'webview/assets').mkdir(parents=True); (self.root/'out').mkdir()
  (self.root/'package.json').write_text(json.dumps({'publisher':'openai','name':'chatgpt','version':patch.VERSION}))
  (self.root/'webview/index.html').write_text('<html><head></head><body><div id="root"></div></body></html>')
  (self.root/'out/extension.js').write_text('async function load(e,r,s){e.html=await this.getWebviewContent(e),s.start()}\ne.onDidChangeVisibility(()=>{je.commands.executeCommand("setContext","chatgpt.sidebarView.visible",e.visible)})')
  self.originals={name:(self.root/name).read_bytes() for name in patch.ORIGINALS}
  self.expected=mock.patch.object(patch,'ORIGINALS',{name:hashlib.sha256(data).hexdigest() for name,data in self.originals.items()});self.expected.start();self.addCleanup(self.expected.stop)
  self.modes={name:(self.root/name).stat().st_mode & 0o777 for name in patch.ORIGINALS}
 def tearDown(self): self.temp.cleanup()
 def runpatch(self,action):
  with unittest.mock.patch.object(sys,'argv',['patch.py',action,str(self.root)]), contextlib.redirect_stdout(io.StringIO()): patch.main()
 def assertOriginal(self):
  for name,b in self.originals.items(): self.assertEqual((self.root/name).read_bytes(),b); self.assertEqual((self.root/name).stat().st_mode & 0o777,self.modes[name])
 def test_roundtrip_and_sidebar_branch(self):
  self.runpatch('install'); ext=(self.root/'out/extension.js').read_text(); self.assertIn('r==="sidebar"?',ext); self.runpatch('remove'); self.assertOriginal(); self.assertFalse((self.root/'webview/.codex-pet-opening').exists()); self.assertFalse(list((self.root/'webview/assets').iterdir()))
 def test_user_edit_refuses_rollback(self):
  self.runpatch('install'); path=self.root/'out/extension.js'; path.write_text(path.read_text()+'// user\n'); before=path.read_bytes()
  with self.assertRaises(SystemExit): self.runpatch('remove')
  self.assertEqual(path.read_bytes(),before); self.assertTrue((self.root/'webview/.codex-pet-opening').exists())
 def test_modified_source_and_existing_asset_refuses_install(self):
  path=self.root/'webview/index.html'; path.write_bytes(path.read_bytes()+b'!')
  with self.assertRaises(SystemExit): self.runpatch('install')
  path.write_bytes(self.originals['webview/index.html']); (self.root/'webview/assets/cp-dragon.png').write_bytes(b'other')
  with self.assertRaises(SystemExit): self.runpatch('install')
  self.assertOriginal(); self.assertEqual((self.root/'webview/assets/cp-dragon.png').read_bytes(),b'other')
 def test_recover_partial_failure(self):
  real=patch.atomic
  def fail(path,data,mode=0o644):
   if path == self.root/'out/extension.js': raise OSError('simulated write failure')
   return real(path,data,mode)
  with unittest.mock.patch.object(patch,'atomic',side_effect=fail):
   with self.assertRaises(OSError): self.runpatch('install')
  self.assertOriginal(); self.assertFalse((self.root/'webview/.codex-pet-opening').exists()); self.assertFalse(list((self.root/'webview/assets').iterdir()))
 def test_interrupted_install_recovery(self):
  self.runpatch('install')
  (self.root/'out/extension.js').write_bytes(self.originals['out/extension.js'])
  (self.root/'webview/assets/cp-opening.css').unlink()
  self.runpatch('remove'); self.assertOriginal(); self.assertFalse(list((self.root/'webview/assets').iterdir()))
 def test_interrupted_remove_recovery(self):
  self.runpatch('install')
  (self.root/'webview/index.html').write_bytes(self.originals['webview/index.html'])
  (self.root/'webview/assets/cp-opening.js').unlink()
  self.runpatch('remove'); self.assertOriginal(); self.assertFalse(list((self.root/'webview/assets').iterdir()))
 def test_interrupted_modified_asset_refuses_recovery(self):
  self.runpatch('install')
  target=self.root/'webview/assets/cp-opening.css'; target.write_bytes(b'user change')
  with self.assertRaises(SystemExit): self.runpatch('remove')
  self.assertEqual(target.read_bytes(),b'user change'); self.assertTrue((self.root/'webview/.codex-pet-opening').exists())
 def test_recover_before_manifest(self):
  state=self.root/'webview/.codex-pet-opening';state.mkdir();(state/'original-0').write_bytes(self.originals['webview/index.html'])
  self.runpatch('remove');self.assertOriginal();self.assertFalse(state.exists())
 def test_asset_created_by_another_process_is_not_overwritten(self):
  real=patch.publish_asset
  def competing(path,state,data):
   path.write_bytes(b'user resource')
   return real(path,state,data)
  with mock.patch.object(patch,'publish_asset',side_effect=competing):
   with self.assertRaises(FileExistsError): self.runpatch('install')
  self.assertOriginal(); self.assertEqual((self.root/'webview/assets/cp-opening.css').read_bytes(),b'user resource')
  self.assertFalse((self.root/'webview/.codex-pet-opening').exists())
 def test_failure_preserves_created_resource_if_user_changes_it(self):
  real=patch.publish_asset
  def publish(path,state,data):
   if path.name=='cp-opening.js':
    (self.root/'webview/assets/cp-opening.css').write_bytes(b'user edit')
    raise OSError('simulated write failure')
   return real(path,state,data)
  with mock.patch.object(patch,'publish_asset',side_effect=publish):
   with self.assertRaises(OSError): self.runpatch('install')
  self.assertOriginal();self.assertEqual((self.root/'webview/assets/cp-opening.css').read_bytes(),b'user edit')
  self.assertTrue((self.root/'webview/.codex-pet-opening/manifest.json').exists())
  with self.assertRaises(SystemExit): self.runpatch('remove')
 def test_version_mismatch(self):
  path=self.root/'package.json'; data=json.loads(path.read_text()); data['version']='99.0'; path.write_text(json.dumps(data))
  with self.assertRaises(SystemExit): self.runpatch('install')
  self.assertOriginal()
if __name__ == '__main__': unittest.main(verbosity=2)
