import copy
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from twonote.vault import Vault, VaultError, aad, atomic_write, digest, json_bytes, new_config, seal
from twonote.legacy import migrate_legacy
from twonote.sync import SyncHost, exchange, parse_pairing, request


PASSWORD = 'テスト用password-1234'


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twonote-v2-test-')
        self.root = Path(self.temp.name)
        self.vault = Vault.create(self.root / 'a', PASSWORD)

    def tearDown(self):
        self.vault.lock()
        self.temp.cleanup()

    def replica(self):
        return Vault.create(self.root / 'b', PASSWORD, self.vault.config)

    def transfer(self, sender, receiver):
        inventory = sender.inventory()
        for kind in ('blobs', 'revisions'):
            for oid in inventory[kind]:
                receiver.stage_object(kind, oid, sender.read_object(kind, oid))
        receiver.commit(inventory['revisions'])

    def test_duplicate_titles_keep_distinct_images_and_search_fields(self):
        for i in range(2):
            item = self.vault.add_blob('same.png', 'image/png', b'IMAGE' + bytes([i]))
            self.vault.save(uuid.uuid4().hex, [], {'title':'Same', 'content':'', 'tags':str(i), 'attachments':[item]})
        self.assertEqual(len(self.vault.heads()), 2)
        self.assertEqual(len(self.vault.inventory()['blobs']), 2)
        reopened = Vault.open(self.vault.root, PASSWORD)
        self.assertEqual(len(reopened.heads()), 2)
        reopened.lock()

    def test_ciphertext_files_do_not_contain_body_or_filenames(self):
        marker = 'PRIVATE-PHRASE-DO-NOT-STORE-PLAIN'
        item = self.vault.add_blob(marker + '.png', 'image/png', marker.encode() * 5000)
        self.vault.save(uuid.uuid4().hex, [], {'title':marker, 'content':marker * 2000, 'attachments':[item]})
        for path in self.vault.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(marker.encode(), path.read_bytes(), str(path))

    def test_conflicting_edits_and_delete_are_preserved(self):
        first = self.vault.save(uuid.uuid4().hex, [], {'title':'Fixture', 'content':'first'})
        replica = self.replica()
        self.transfer(self.vault, replica)
        desktop = self.vault.save(first['note_id'], [first['revision']], {**first, 'content':'PC edit'})
        phone = replica.save(first['note_id'], [first['revision']], {**first, 'tags':'Android tag'})
        self.transfer(replica, self.vault)
        self.transfer(self.vault, replica)
        self.assertEqual(len(self.vault.heads()), 2)
        self.assertEqual({n['content'] for n in self.vault.heads()}, {'first','PC edit'})
        resolved = self.vault.save(first['note_id'], [desktop['revision'], phone['revision']], {**desktop,'tags':'Android tag'})
        self.assertEqual(len(self.vault.heads()), 1)
        self.assertEqual(self.vault.heads()[0]['tags'], 'Android tag')
        self.vault.save(first['note_id'], [resolved['revision']], {**resolved,'deleted':True})
        self.transfer(self.vault, replica)
        self.assertEqual(replica.heads(), [])
        replica.lock()

    def test_atomic_publish_failure_does_not_lose_previous_note(self):
        first = self.vault.save(uuid.uuid4().hex, [], {'title':'original'})
        write = atomic_write
        def fail_index(path, data):
            if path == self.vault.root / 'index.enc':
                raise OSError('injected disk failure')
            return write(path, data)
        with patch('twonote.vault.atomic_write', fail_index):
            with self.assertRaises(OSError):
                self.vault.save(first['note_id'], [first['revision']], {'title':'not committed'})
        self.assertEqual(self.vault.heads()[0]['title'], 'original')
        reopened = Vault.open(self.vault.root, PASSWORD)
        self.assertEqual(reopened.heads()[0]['title'], 'original')
        reopened.lock()

    def test_partial_transfer_cannot_publish_missing_attachment(self):
        item = self.vault.add_blob('image.png','image/png',b'bytes')
        first = self.vault.save(uuid.uuid4().hex, [], {'title':'with image','attachments':[item]})
        replica = self.replica()
        replica.stage_object('revisions', first['revision'], self.vault.read_object('revisions', first['revision']))
        with self.assertRaises(VaultError):
            replica.commit([first['revision']])
        self.assertEqual(replica.heads(), [])
        self.transfer(self.vault, replica)
        self.assertEqual(replica.read_blob(item['id']), b'bytes')
        self.assertEqual(replica.commit([first['revision']]), 0)
        replica.lock()

    def test_tampering_and_wrong_password_fail_closed(self):
        with self.assertRaises(VaultError):
            Vault.open(self.vault.root, 'wrong-password')
        item = self.vault.add_blob('file','text/plain',b'fixture')
        raw = bytearray(self.vault.read_object('blobs',item['id']))
        raw[-1] ^= 1
        with self.assertRaises(VaultError):
            self.vault.stage_object('blobs',digest(raw),bytes(raw))
        self.vault.lock()
        with self.assertRaises(VaultError):
            self.vault.heads()

    def test_revisions_change_within_same_second(self):
        first = self.vault.save(uuid.uuid4().hex, [], {'title':'first'})
        second = self.vault.save(first['note_id'],[first['revision']],{'title':'second'})
        self.assertNotEqual(first['revision'],second['revision'])

    def test_missing_config_cannot_overwrite_existing_encrypted_index(self):
        original = (self.vault.root / 'index.enc').read_bytes()
        (self.vault.root / 'config.json').unlink()
        with self.assertRaises(VaultError):
            Vault.create(self.vault.root, PASSWORD)
        self.assertEqual((self.vault.root / 'index.enc').read_bytes(), original)

    def test_binary_one_note_image_is_recognized_by_header(self):
        item = self.vault.add_blob('image.bin', 'application/octet-stream', b'\x89PNG\r\n\x1a\nfixture')
        self.assertEqual(item['mime'], 'image/png')

    def test_parent_from_another_note_is_rejected_even_if_content_is_identical(self):
        first = self.vault.save(uuid.uuid4().hex, [], {'title': 'same'})
        with self.assertRaises(VaultError):
            self.vault.save(uuid.uuid4().hex, [first['revision']], {'title': 'same'})


class MigrationTests(unittest.TestCase):
    def legacy(self, root):
        root.mkdir()
        (root/'attachments').mkdir()
        db=sqlite3.connect(root/'notes.sqlite3')
        db.executescript('''CREATE TABLE notes (id TEXT PRIMARY KEY,title TEXT,content TEXT,notebook TEXT,section TEXT,tags TEXT,source_path TEXT,created_at TEXT,updated_at TEXT,payload BLOB);
        CREATE TABLE attachments (id TEXT PRIMARY KEY,note_id TEXT,filename TEXT,mime TEXT,size INTEGER,stored_name TEXT,created_at TEXT,payload BLOB);''')
        for i in range(2):
            nid=uuid.uuid4().hex
            aid=uuid.uuid4().hex
            db.execute('INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?)',(nid,'same','PRIVATE-LEGACY-BODY'*300,'book','section','','','t','t',None))
            raw=b'LEGACY-IMAGE'+bytes([i])
            (root/'attachments'/aid).write_bytes(raw)
            db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?)',(aid,nid,'photo.png','image/png',len(raw),aid,'t',None))
        db.commit();db.close()

    def test_migration_preserves_all_notes_and_encrypts_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'legacy'
            self.legacy(source)
            vault,result=migrate_legacy(source,PASSWORD)
            self.assertEqual(result['notes'],2)
            self.assertEqual(result['attachments'],2)
            self.assertEqual(result['remaining'],[])
            self.assertFalse((source/'notes.sqlite3').exists())
            self.assertEqual(len(vault.heads()),2)
            for path in source.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'PRIVATE-LEGACY-BODY',path.read_bytes())
                    self.assertNotIn(b'LEGACY-IMAGE',path.read_bytes())
            vault.lock()

    def test_failed_migration_does_not_modify_legacy_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'legacy'
            self.legacy(source)
            before={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob('*') if p.is_file()}
            with patch.object(Vault,'add_blob',side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    migrate_legacy(source,PASSWORD)
            for relative,raw in before.items():
                self.assertEqual((source/relative).read_bytes(),raw)
            self.assertFalse((source/'vault-v2'/'config.json').exists())

    def test_encrypted_legacy_and_double_encrypted_attachment_are_recovered(self):
        import base64
        import json
        import os
        from twonote.vault import derive
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'legacy'
            self.legacy(source)
            salt = os.urandom(16)
            key = derive(PASSWORD, salt)
            config = {'iterations': 600000, 'salt': base64.urlsafe_b64encode(salt).decode(),
                      'verifier': base64.urlsafe_b64encode(seal(key, b'TwoNote vault verifier v1', b'verifier')).decode()}
            (source / 'vault.json').write_text(json.dumps(config), encoding='utf-8')
            db = sqlite3.connect(source / 'notes.sqlite3')
            db.row_factory = sqlite3.Row
            expected = []
            for row in db.execute('SELECT * FROM notes').fetchall():
                values = {f: row[f] for f in ('title', 'content', 'notebook', 'section', 'tags', 'source_path')}
                db.execute('UPDATE notes SET payload=? WHERE id=?', (seal(key, json_bytes(values), row['id'].encode()), row['id']))
            for row in db.execute('SELECT * FROM attachments').fetchall():
                path = source / 'attachments' / row['stored_name']
                raw = path.read_bytes()
                expected.append(raw)
                path.write_bytes(seal(key, seal(key, raw, row['id'].encode()), row['id'].encode()))
                values = {f: row[f] for f in ('filename', 'mime', 'size')}
                db.execute('UPDATE attachments SET payload=? WHERE id=?', (seal(key, json_bytes(values), row['id'].encode()), row['id']))
            db.commit()
            db.close()
            vault, report = migrate_legacy(source, PASSWORD)
            self.assertEqual(report['remaining'], [])
            actual = [vault.read_blob(a['id']) for n in vault.heads() for a in n['attachments']]
            self.assertCountEqual(actual, expected)
            vault.lock()

    def test_crashed_wal_database_is_migrated_without_plaintext_leftover(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'legacy'
            self.legacy(source)
            subprocess.run([sys.executable, '-c',
                "import sqlite3,sys,os; db=sqlite3.connect(sys.argv[1]); db.execute('PRAGMA journal_mode=WAL'); db.execute(\"UPDATE notes SET content='WAL-PRIVATE-CONTENT'\"); db.commit(); os._exit(0)",
                str(source / 'notes.sqlite3')], check=True)
            self.assertTrue((source / 'notes.sqlite3-wal').is_file())
            vault, report = migrate_legacy(source, PASSWORD)
            self.assertEqual(report['remaining'], [])
            self.assertFalse((source / 'notes.sqlite3').exists())
            self.assertFalse((source / 'notes.sqlite3-wal').exists())
            self.assertEqual({n['content'] for n in vault.heads()}, {'WAL-PRIVATE-CONTENT'})
            vault.lock()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.vault=Vault.create(self.root/'desktop',PASSWORD)
        self.host=SyncHost(self.vault)
        self.port=self.host.start('127.0.0.1',0,seconds=20)

    def tearDown(self):
        self.host.stop()
        self.vault.lock()
        self.temp.cleanup()

    def pair(self):
        peer=parse_pairing(self.host.pairing_uri('127.0.0.1'))
        paired=request(peer,'POST','/v2/pair',{'device_id':uuid.uuid4().hex,'label':'fixture'},token=peer['secret'])
        peer['token']=paired['token']
        return peer,paired

    def test_bidirectional_delta_and_no_unchanged_image_downloads(self):
        blob=self.vault.add_blob('one.png','image/png',b'image')
        n=self.vault.save(uuid.uuid4().hex,[],{'title':'one','attachments':[blob]})
        peer,paired=self.pair()
        phone=Vault.create(self.root/'phone',PASSWORD,paired['config'])
        first=exchange(phone,peer,finish=False)
        self.assertEqual(first['downloaded'],2)
        self.assertEqual(exchange(phone,peer,finish=False),{'uploaded':0,'downloaded':0})
        p=phone.heads()[0]
        phone.save(p['note_id'],[p['revision']],{**p,'content':'phone edit'})
        self.assertEqual(exchange(phone,peer,finish=False),{'uploaded':1,'downloaded':0})
        self.assertEqual(self.vault.heads()[0]['content'],'phone edit')
        request(peer,'POST','/v2/done',{})
        for _ in range(30):
            if not self.host.active:break
            time.sleep(.05)
        self.assertFalse(self.host.active)
        phone.lock()

    def test_pin_mismatch_and_revoked_token(self):
        peer,_=self.pair()
        with self.assertRaises(VaultError):
            request({**peer,'pin':'0'*64},'GET','/v2/index')
        with self.assertRaises(VaultError):
            request(peer,'GET','/v2/index',token='invalid')
        self.host.revoke_all()
        self.host.start('127.0.0.1',0)
        peer['port']=self.host.server.server_address[1]
        with self.assertRaises(VaultError):
            request(peer,'GET','/v2/index')

    def test_stop_closes_incomplete_tls_connection(self):
        with socket.create_connection(('127.0.0.1',self.port)):
            time.sleep(.1)
            started=time.monotonic()
            self.host.stop()
            self.assertLess(time.monotonic()-started,2)
            self.assertFalse(self.host.active)
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1',self.port)),0)

    def test_timer_stops_listener(self):
        self.host.stop()
        self.host.start('127.0.0.1',0,seconds=.25)
        for _ in range(30):
            if self.host.server is None:break
            time.sleep(.05)
        self.assertIsNone(self.host.server)

    def test_old_completion_timer_cannot_stop_a_new_sync_session(self):
        old_server = self.host.server
        self.host.stop()
        self.host.start('127.0.0.1', 0, seconds=20)
        self.host.stop(old_server)
        self.assertTrue(self.host.active)

    def test_tls_private_key_is_encrypted_at_rest(self):
        from cryptography.hazmat.primitives import serialization
        raw = (self.vault.root / 'tls.key').read_bytes()
        self.assertIn(b'BEGIN ENCRYPTED PRIVATE KEY', raw)
        with self.assertRaises(TypeError):
            serialization.load_pem_private_key(raw, password=None)


if __name__=='__main__':
    unittest.main()
