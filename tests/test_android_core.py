"""Runs the actual Android storage and TLS client Java classes on the bundled JDK."""
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from twonote.vault import Vault
from twonote.sync import SyncHost

PASSWORD='日本語パスワード-test-123'


@unittest.skipUnless(os.environ.get('TWONOTE_JAVA') and os.environ.get('TWONOTE_JAVA_CP'), 'run tools/test_android_core.ps1')
class AndroidCoreTests(unittest.TestCase):
    def test_deleted_conflict_visibility_selection_and_merge_sync_without_resurrection(self):
        from twonote import inline
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); pc = Vault.create(root/'desktop', PASSWORD)
            image = pc.add_blob('keep.png', 'image/png', b'KEEP-IMAGE')
            unwanted_image = pc.add_blob('discard.png', 'image/png', b'UNWANTED-IMAGE')
            base = pc.save(uuid.uuid4().hex, [], {'title':'Conflict fixture', 'content':'base', 'attachments':[image]})
            keep = pc.save(base['note_id'], [base['revision']], {**base, 'content':inline.encode('KEEP' + inline.token(image['id']))})
            other = pc.save(base['note_id'], [base['revision']], {**base, 'content':'UNWANTED', 'attachments':[unwanted_image]})
            host = SyncHost(pc)
            try:
                host.start('127.0.0.1', 0, seconds=120); phone = root/'android'
                self.probe('pair', phone, PASSWORD, host.pairing_uri('127.0.0.1'))
                deleted = pc.set_deleted(other['revision'], True)
                self.probe('sync', phone, PASSWORD)
                status = self.probe('heads-json', phone, PASSWORD)
                self.assertEqual((len(status['live']), len(status['all']), status['visible']), (1, 2, 1))
                self.assertEqual(status['live'][0]['conflicts'], 1)
                chosen = self.probe('resolve-versions', phone, PASSWORD, keep['revision'], json.dumps([n['revision'] for n in status['all']]), 'keep')
                self.assertEqual(chosen['content'], keep['content']); self.assertEqual(chosen['attachments'], keep['attachments'])
                self.probe('sync', phone, PASSWORD)
                self.assertEqual(len(pc.heads(True)), 1)
                self.assertEqual(pc.get(deleted['revision'])['content'], 'UNWANTED')
                self.assertEqual(self.probe('sync', phone, PASSWORD), {'uploaded':0,'downloaded':0})
                # A genuinely new offline edit must still surface as a conflict.
                newer = pc.save(base['note_id'], [base['revision']], {**base, 'content':'NEW-OFFLINE-EDIT'})
                dead = pc.save(base['note_id'], [base['revision']], {**other, 'content':'DELETED-DO-NOT-MERGE', 'deleted':True})
                self.probe('sync', phone, PASSWORD)
                status = self.probe('heads-json', phone, PASSWORD)
                self.assertEqual((len(status['live']), len(status['all']), status['visible']), (2, 3, 2))
                self.assertEqual({n['conflicts'] for n in status['live']}, {2})
                # Snapshot validation refuses unseen heads even though the selected head is still live.
                rejected = subprocess.run([os.environ['TWONOTE_JAVA'], '-cp', os.environ['TWONOTE_JAVA_CP'], 'CoreProbe', 'resolve-versions', str(phone), PASSWORD, chosen['revision'], json.dumps([chosen['revision']]), 'keep'], capture_output=True)
                self.assertNotEqual(rejected.returncode, 0); self.assertIn(b'IllegalArgumentException', rejected.stderr)
                merged = self.probe('resolve-versions', phone, PASSWORD, chosen['revision'], json.dumps([n['revision'] for n in status['all']]), 'merge')
                self.assertIn('NEW-OFFLINE-EDIT', merged['content']); self.assertNotIn('DELETED-DO-NOT-MERGE', merged['content'])
                self.assertNotIn(unwanted_image['id'], {a['id'] for a in merged['attachments']})
                self.probe('sync', phone, PASSWORD)
                self.assertEqual(len(pc.heads(True)), 1); self.assertEqual(pc.heads()[0]['content'], merged['content'])
                # Last live version can be hidden and explicitly restored on Android.
                removed = self.probe('set-deleted', phone, PASSWORD, merged['revision'], 'true')
                self.assertEqual(self.probe('heads-json', phone, PASSWORD)['live'], [])
                restored = self.probe('set-deleted', phone, PASSWORD, removed['revision'], 'false')
                self.assertEqual(restored['content'], merged['content'])
            finally:
                host.stop(); pc.lock()

    def test_inline_and_table_format_matches_python(self):
        from twonote import inline, blocks
        result = subprocess.run([os.environ['TWONOTE_JAVA'], '-XX:ActiveProcessorCount=1', '-Xmx128m', '-cp', os.environ['TWONOTE_JAVA_CP'], 'InlineBlockProbe'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertGreaterEqual(data['checks'], 15)
        self.assertEqual(data['legacy'], inline.body('前😀後', [{'id': 'a'*64, 'mime':'image/png'}]))
        self.assertEqual(data['inserted'], inline.encode('前😀' + inline.token('a'*64) + '後'))
        self.assertEqual(data['table'], blocks.parse('table', '日本語', '"a,b","line\n2"\n"引用""符",😀\n'))
        self.assertEqual(data['normalized'], [blocks.parse('bar', '', 'A,' + n)['rows'][0][1] for n in ['１２','1_000','－１．５','1e2']])
        self.assertGreater(data['large_bytes'], 400000)

    def probe(self,*args):
        result=subprocess.run([os.environ['TWONOTE_JAVA'],'-cp',os.environ['TWONOTE_JAVA_CP'],'CoreProbe',*map(str,args)],capture_output=True,text=True,encoding='utf-8',timeout=60)
        if result.returncode:
            self.fail(result.stderr)
        return json.loads(result.stdout)

    def test_python_java_offline_edit_pinned_sync_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            pc=Vault.create(root/'desktop',PASSWORD)
            image=pc.add_blob('日本語.png','image/png','画像バイナリ'.encode())
            original=pc.save(uuid.uuid4().hex,[],{'title':'日本語のノート','content':'本文','attachments':[image]})
            host=SyncHost(pc)
            try:
                host.start('127.0.0.1',0,seconds=120)
                phone=root/'android'
                stats=self.probe('pair',phone,PASSWORD,host.pairing_uri('127.0.0.1'))
                self.assertEqual(stats,{'uploaded':0,'downloaded':2})
                content=self.probe('read',phone,PASSWORD)
                self.assertEqual(content['title'],'日本語のノート')
                self.assertEqual(content['blob'],'画像バイナリ')
                self.assertEqual(self.probe('sync',phone,PASSWORD),{'uploaded':0,'downloaded':0})
                self.probe('edit',phone,PASSWORD)
                # The actual Java editor's files are readable by the Windows core.
                decoded=Vault.open(phone,PASSWORD)
                self.assertEqual(decoded.heads()[0]['content'],'Androidの編集')
                decoded.lock()
                pc.save(original['note_id'],[original['revision']],{**original,'content':'Windowsの編集'})
                stats=self.probe('sync',phone,PASSWORD)
                self.assertEqual(stats,{'uploaded':2,'downloaded':1})
                self.assertEqual({n['content'] for n in pc.heads()},{'Windowsの編集','Androidの編集'})
                self.assertEqual(self.probe('sync',phone,PASSWORD),{'uploaded':0,'downloaded':0})
                host.stop()
                self.assertEqual(self.probe('read',phone,PASSWORD)['heads'],2)
                self.assertTrue(self.probe('lock',phone,PASSWORD)['locked'])
            finally:
                host.stop();pc.lock()

    def test_inline_photo_chart_order_and_data_survive_actual_sync(self):
        from twonote import inline, blocks
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pc = Vault.create(root/'desktop', PASSWORD)
            image = pc.add_blob('fixture.png', 'image/png', b'synthetic-photo')
            pc.save(uuid.uuid4().hex, [], {'title':'Inline', 'content':'before', 'attachments':[image]})
            host = SyncHost(pc)
            try:
                host.start('127.0.0.1', 0, seconds=120)
                phone = root/'android'
                self.probe('pair', phone, PASSWORD, host.pairing_uri('127.0.0.1'))
                result = self.probe('inline-edit', phone, PASSWORD)
                self.probe('sync', phone, PASSWORD)
                note = pc.heads()[0]
                self.assertEqual(note['content'], result['content'])
                self.assertEqual([kind for kind, value in inline.parts(inline.body(note['content']), note['attachments'])], ['text','embed','text','embed','text'])
                self.assertEqual(blocks.load(pc.read_blob(result['chart']))['rows'], [['A','-2'], ['B','3']])
                self.assertEqual(pc.read_blob(image['id']), b'synthetic-photo')
            finally:
                host.stop(); pc.lock()
