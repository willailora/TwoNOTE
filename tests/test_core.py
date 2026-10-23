import tempfile
import unittest
from pathlib import Path

from twonote.importing import import_into
from twonote.vault import Vault


class ImportTests(unittest.TestCase):
    def test_html_import_and_same_source_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            image=root/'photo.png';image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            page=root/'Page.html';page.write_text('<html><title>テスト</title><style>ignored</style><p>本文</p><img src="photo.png"></html>',encoding='utf-8')
            vault=Vault.create(root/'vault','test-password-1234')
            result=import_into(vault,page)
            self.assertEqual(result['imported'],1)
            self.assertEqual(result['attachments'],1)
            note=vault.heads()[0]
            self.assertEqual(note['title'],'テスト')
            self.assertNotIn('ignored',note['content'])
            self.assertEqual(vault.read_blob(note['attachments'][0]['id']),image.read_bytes())
            self.assertEqual(import_into(vault,page)['skipped'],1)
            vault.lock()

    def test_references_cannot_read_outside_import_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'private.png').write_bytes(b'private')
            folder=root/'export';folder.mkdir()
            page=folder/'Page.html';page.write_text('<img src="../private.png">',encoding='utf-8')
            vault=Vault.create(root/'vault','test-password-1234')
            result=import_into(vault,page)
            self.assertEqual(result['attachments'],0)
            self.assertTrue(result['warnings'])
            vault.lock()

    def test_mht_inline_image_import_is_editable(self):
        from email.message import EmailMessage
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            mail=EmailMessage();mail.set_type('multipart/related')
            html=EmailMessage();html.set_content('<title>MHT</title><p>本文</p><img src="cid:photo">',subtype='html',charset='utf-8');mail.attach(html)
            image=EmailMessage();image.set_content(b'IMAGE',maintype='image',subtype='png');image['Content-ID']='<photo>';mail.attach(image)
            path=root/'export.mht';path.write_bytes(mail.as_bytes())
            vault=Vault.create(root/'vault','test-password-1234')
            self.assertEqual(import_into(vault,path)['attachments'],1)
            note=vault.heads()[0];changed=vault.save(note['note_id'],[note['revision']],{**note,'content':'編集しました'})
            self.assertEqual(changed['content'],'編集しました')
            vault.lock()
