"""Generate synthetic review data outside the source-only publication checkout."""
from __future__ import annotations

import argparse
import getpass
import io
import json
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
packages = ROOT / ".tools" / "python-packages"
if packages.is_dir(): sys.path.insert(0, str(packages))


def create_demo(output: Path, password: str) -> dict:
    from PIL import Image, ImageDraw
    from twonote import blocks, inline
    from twonote.vault import Vault

    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("ダミーデータは公開用フォルダの外へ作成してください")
    if output.exists():
        raise FileExistsError("既存のフォルダは上書きしません。新しい保存先を指定してください")
    if len(password) < 12:
        raise ValueError("ダミー専用のパスワードは12文字以上にしてください")
    output.mkdir(parents=True)
    vault = Vault.create(output / "vault-v2", password)
    try:
        image = Image.new("RGB", (640, 220), "#14273d")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((24, 24, 190, 194), fill="#6dd3cf")
        drawing.rectangle((210, 24, 616, 194), outline="#9fb3c8", width=4)
        drawing.text((238, 70), "DEMO IMAGE", fill="#e6edf5", font_size=30)
        drawing.text((238, 125), "SYNTHETIC DATA ONLY", fill="#e6edf5", font_size=20)
        buffer = io.BytesIO(); image.save(buffer, format="PNG"); raw_image = buffer.getvalue()
        attachment = vault.add_blob("demo-image.png", "image/png", raw_image)
        vault.save(uuid.uuid4().hex, [], {"title":"DEMO｜画像と日本語", "notebook":"DEMO ノートブック", "section":"画像・本文",
            "content":inline.encode("これは公開確認用の合成データです。個人のノートや写真は使っていません。\n\n画像の前の文章です。\n" + inline.token(attachment["id"]) + "\n画像の後の文章です。\n日本語・カタカナ・ひらがな・絵文字 😀 を確認できます。"), "attachments":[attachment]})
        files, references = [], []
        for kind, heading, text in [("table", "DEMO 集計表", "項目,内容\nサンプルA,10\nサンプルB,20"), ("bar", "DEMO 棒グラフ", "A,10\nB,20\nC,15"), ("line", "DEMO 折れ線", "A,5\nB,15\nC,10")]:
            value = blocks.parse(kind, heading, text)
            added = vault.add_blob("demo-" + kind + ".json", inline.BLOCK_MIME, blocks.dump(value))
            files.append(added); references.append(inline.token(added["id"]))
        vault.save(uuid.uuid4().hex, [], {"title":"DEMO｜表とグラフ", "notebook":"DEMO ノートブック", "section":"表・グラフ",
            "content":inline.encode("すべて架空の項目と数値です。\n\n" + "\n\n".join(references)), "attachments":files})
        vault.save(uuid.uuid4().hex, [], {"title":"DEMO｜確認メモ", "notebook":"DEMO ノートブック", "section":"確認手順",
            "content":"この保管庫はレビューと撮影専用です。\n通常の個人用保管庫とは分けて起動してください。\n取り込み確認用MHTは、この保存先のexportsフォルダにあります。"})
        exports = output / "exports"; exports.mkdir()
        message = EmailMessage(); message.make_related()
        html = EmailMessage(); html.set_content('<html><head><title>DEMO 移行チェック</title></head><body><p>DEMO：画像の前</p><img src="cid:demo-image"><p>DEMO：画像の後</p><table><caption>DEMO 表の説明文</caption><tr><th>項目</th><th>値</th></tr><tr><td>サンプル</td><td>123</td></tr></table></body></html>', subtype="html", charset="utf-8")
        picture = EmailMessage(); picture.set_content(raw_image, maintype="image", subtype="png")
        picture["Content-ID"] = "<demo-image>"; picture.add_header("Content-Disposition", "inline", filename="demo-image.png")
        message.attach(html); message.attach(picture)
        (exports / "demo.mht").write_bytes(message.as_bytes())
        return {"synthetic_only":True, "notes":len(vault.heads()), "vault":"vault-v2", "sample_export":"exports/demo.mht"}
    finally:
        vault.lock()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    password = getpass.getpass("ダミー専用パスワード（12文字以上）: ")
    print(json.dumps(create_demo(args.output, password), ensure_ascii=True))


if __name__ == "__main__": main()
