"""Import sources without deduplicating different pages by their text."""
from __future__ import annotations

import email
import base64
import hashlib
import html
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from email import policy
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from .vault import MAX_BLOB, Vault, VaultError, json_bytes
from . import inline, blocks

# MS-ONESTORE 2.3.1: guidFileType (first 16 bytes, GUID little-endian).
# https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-onestore/2b394c6b-8788-441f-b631-da1583d772fd
ONE_SECTION_HEADER = uuid.UUID("7b5c52e4-d88c-4da7-aeb1-5378d02996d3").bytes_le
ONE_TOC_HEADER = uuid.UUID("43ff2fa1-efd9-4c76-9ee2-10ea5722765f").bytes_le


def local_image_path(parent: Path, reference: str) -> Path:
    """Reject network/absolute paths before any resolving/stat operation on them."""
    decoded = unquote(reference.split("#", 1)[0].split("?", 1)[0])
    path = PureWindowsPath(decoded)
    if (not decoded or path.drive or path.root or urlsplit(decoded).scheme
            or ":" in decoded or any(ord(c) < 32 for c in decoded)):
        raise ValueError("ローカルの相対画像パスではありません")
    parts = []
    for part in path.parts:
        if part == "..":
            if not parts: raise ValueError("取り込みフォルダの外にある画像です")
            parts.pop()
        elif part != ".": parts.append(part)
    if not parts: raise ValueError("画像パスが空です")
    candidate = parent
    for part in parts:
        candidate = candidate / part
        # A relative reference must not traverse a junction/symlink to a share.
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("リンク経由の画像は直接コピーしてから取り込んでください")
    return candidate


def onenote_file_kind(path: Path) -> str:
    """Do not trust suffixes: expand.exe can mangle the dot in UTF-8 CAB names."""
    with Path(path).open("rb") as stream:
        header = stream.read(16)
    if header == ONE_SECTION_HEADER:
        return "section"
    if header == ONE_TOC_HEADER:
        return "toc"
    return "unknown"


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.images = [], [], []
        self.in_title, self.skip = False, 0
        self.runs, self.tables = [], []
        self.table_plain, self.complex_tables = [], set()
        self.table_captions, self.in_caption = [], False
        self.table_depth = 0
        self.table_rows, self.table_row, self.cell = None, None, None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if self.skip or self.in_title: return
        if tag == "table":
            if not self.table_depth:
                self.table_rows = []; self.tables.append(self.table_rows)
                self.table_plain.append([])
                self.table_captions.append([])
                self.runs.append(("table", len(self.tables) - 1))
            else: self.complex_tables.add(len(self.tables) - 1)
            self.table_depth += 1
        if self.table_depth and tag == "caption": self.in_caption = True
        if self.table_depth and any(attributes.get(name, "1") != "1" for name in ("colspan", "rowspan")):
            self.complex_tables.add(len(self.tables) - 1)
        if self.table_depth and tag == "tr": self.table_row = []; self.table_rows.append(self.table_row)
        if self.table_depth and tag in ("td", "th"): self.cell = []; self.table_row = [] if self.table_row is None else self.table_row
        if tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"])
            self.runs.append(("image", attributes["src"]))
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")
            if not self.table_depth: self.runs.append("\n")
            elif self.cell is not None: self.cell.append("\n")
            if self.table_depth: self.table_plain[-1].append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
            return
        if self.skip: return
        if tag == "caption": self.in_caption = False
        if self.table_depth and tag in ("td", "th") and self.cell is not None:
            self.table_row.append("".join(self.cell).strip()); self.cell = None
            self.table_plain[-1].append("\t")
        if tag == "table" and self.table_depth:
            self.table_depth -= 1
            if not self.table_depth: self.cell = None; self.table_row = None; self.in_caption = False
        if tag in ("p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")
            if not self.table_depth: self.runs.append("\n")

    def handle_data(self, text):
        if self.skip:
            return
        (self.title if self.in_title else self.parts).append(text)
        if not self.in_title:
            if self.table_depth: self.table_plain[-1].append(text)
            if self.in_caption: self.table_captions[-1].append(text)
            if self.cell is not None: self.cell.append(text)
            elif not self.table_depth: self.runs.append(text)


def text_decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp932", "windows-1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def import_into(vault: Vault, source: Path) -> dict:
    source = Path(source).resolve()
    report = {"imported": 0, "skipped": 0, "attachments": 0, "positioned": 0, "warnings": []}
    with vault.mutex:
        seen = {record["note_id"] for record in vault.records.values()}

    def save_page(identity, title, content, notebook, section, files, runs=None):
        note_id = uuid.uuid5(uuid.NAMESPACE_URL, "twonote-import:" + identity).hex
        def positioned(attachments):
            return inline.encode(re.sub(r"\n{3,}", "\n\n", "".join(inline.token(attachments[r]["id"]) if isinstance(r, int) else r for r in runs)).strip())
        if note_id in seen:
            # Only restore layout when text and every original attachment still
            # match, and there is exactly one live branch. Never overwrite edits.
            if runs is not None and any(isinstance(r, int) for r in runs):
                with vault.mutex:
                    heads = [n for n in vault.heads(True) if n["note_id"] == note_id]
                    if len(heads) == 1 and not heads[0]["deleted"] and heads[0]["content"] == content:
                        old = heads[0]; attachments = old["attachments"]
                        if len(attachments) <= len(files) and all(f[1] == inline.BLOCK_MIME for f in files[len(attachments):]) and all(vault.read_blob(a["id"]) == f[2] for a, f in zip(attachments, files)):
                            attachments = attachments + [vault.add_blob(*f) for f in files[len(attachments):]]
                            vault.save(note_id, [old["revision"]], {**old, "content": positioned(attachments), "attachments": attachments})
                            report["positioned"] += 1
            report["skipped"] += 1
            return
        attachments = [vault.add_blob(name, mime, raw) for name, mime, raw in files]
        if runs is not None and any(isinstance(r, int) for r in runs): content = positioned(attachments)
        vault.save(note_id, [], {"title": title, "content": content, "notebook": notebook,
            "section": section, "source_path": str(source), "attachments": attachments})
        seen.add(note_id)
        report["imported"] += 1
        report["attachments"] += len(attachments)

    def parse_one(path, identity, notebook, section):
        try:
            from aspose.note import AttachedFile, Document, Image, RichText
        except ImportError as exc:
            raise VaultError("OneNote用パーサーがありません。requirements-onenote.txtの依存を導入してください") from exc
        document = Document(str(path))
        for index, page in enumerate(document):
            title = page.Title.TitleText.Text if page.Title and page.Title.TitleText else f"ページ {index + 1}"
            content = "\n".join(item.Text for item in page.GetChildNodes(RichText) if getattr(item, "Text", None))
            files = []
            incomplete = False
            for kind in (Image, AttachedFile):
                for number, item in enumerate(page.GetChildNodes(kind)):
                    name = getattr(item, "FileName", None) or f"attachment-{number + 1}.bin"
                    payload = getattr(item, "Bytes", None)
                    if payload is None or (kind is Image and not payload):
                        report["warnings"].append(f"{path.name}: {title} の添付 {name} を取得できないため、このページは保留しました")
                        incomplete = True
                        continue
                    files.append((name, mimetypes.guess_type(name)[0] or "application/octet-stream", bytes(payload)))
            if incomplete:
                continue
            # The parser exposes the ordered node tree; the previous importer
            # flattened all text first and all images afterward.
            images = list(page.GetChildNodes(Image)); image_indices = {id(item): i for i, item in enumerate(images)}
            runs = []
            def walk(node):
                if isinstance(node, RichText):
                    if getattr(node, "Text", None): runs.extend([node.Text, "\n"])
                elif isinstance(node, Image):
                    if id(node) in image_indices: runs.extend([image_indices[id(node)], "\n"])
                elif type(node).__name__ == "Table":
                    rows = [["\n".join(t.Text for t in cell.GetChildNodes(RichText) if t.Text) for cell in row] for row in node]
                    try:
                        value = blocks.validate({"version": 1, "kind": "table", "title": "", "rows": rows})
                        runs.extend([len(files), "\n"]); files.append(("表.json", inline.BLOCK_MIME, blocks.dump(value)))
                        for image in node.GetChildNodes(Image): walk(image)
                    except ValueError:
                        for child in node: walk(child)
                else:
                    try: children = list(node)
                    except TypeError: return
                    # Independent outlines use page coordinates; children within
                    # an outline already carry reading order (including tables).
                    if node is page:
                        children.sort(key=lambda n: (getattr(n, "VerticalOffset", None) or 0, getattr(n, "HorizontalOffset", None) or 0))
                    for child in children: walk(child)
            walk(page)
            save_page(f"{identity}:page:{index}", re.sub(r"\s+", " ", title).strip(), content, notebook, section, files, runs or None)

    def parse_file(path):
        suffix = path.suffix.lower()
        if suffix not in (".onepkg", ".one", ".html", ".htm", ".mht", ".mhtml", ".txt", ".md", ".markdown"):
            return
        raw = path.read_bytes()
        identity = hashlib.sha256(raw).hexdigest()
        if suffix == ".onepkg":
            expand = shutil.which("expand.exe")
            if expand is None:
                raise VaultError("OneNoteパッケージの展開はWindowsで実行してください")
            with tempfile.TemporaryDirectory(prefix="twonote-onepkg-") as directory:
                result = subprocess.run([expand, "-F:*", str(path), directory], capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=120)
                if result.returncode:
                    raise VaultError("OneNoteパッケージを展開できません")
                for index, section_path in enumerate(sorted(Path(directory).rglob("*"))):
                    if not section_path.is_file():
                        continue
                    try:
                        kind = onenote_file_kind(section_path)
                        if kind == "toc":
                            continue
                        if kind != "section":
                            raise VaultError("OneNoteのセクションとして識別できません")
                        parse_one(section_path, f"{identity}:section:{index}", path.stem, section_path.stem)
                    except Exception as exc:
                        report["warnings"].append(f"{section_path.name}: {type(exc).__name__}")
            return
        if suffix == ".one":
            parse_one(path, identity, path.parent.name, path.stem)
            return
        embedded = {}
        if suffix in (".mht", ".mhtml"):
            document = email.message_from_bytes(raw, policy=policy.default)
            text = None
            for part in document.walk():
                if part.is_multipart():
                    continue
                data = part.get_payload(decode=True) or b""
                if part.get_content_type() == "text/html" and text is None:
                    text = data.decode(part.get_content_charset() or "utf-8", errors="replace")
                elif part.get_content_type().startswith("image/"):
                    location = part.get("Content-Location", "")
                    name = part.get_filename() or Path(location).name or "image"
                    item = (name, part.get_content_type(), data)
                    embedded[location] = item
                    embedded["cid:" + part.get("Content-ID", "").strip("<>")] = item
            if text is None:
                raise VaultError("MHT内にHTMLページがありません")
        else:
            text = text_decode(raw)
        if suffix in (".html", ".htm", ".mht", ".mhtml"):
            parsed = HTMLText()
            parsed.feed(text)
            title = "".join(parsed.title).strip() or path.stem
            content = re.sub(r"\n{3,}", "\n\n", "".join(parsed.parts)).strip()
            references = parsed.images
            source_runs = parsed.runs
        else:
            title, content = path.stem, text
            references = re.findall(r"!\[[^]]*\]\(([^)]+)\)", text)
            source_runs = None
        files = []
        reference_indices = {}
        for reference in dict.fromkeys(references):
            before = len(files)
            embedded_item = embedded.get(reference) or embedded.get(unquote(reference))
            if embedded_item:
                if not embedded_item[2]:
                    report["warnings"].append(f"{path.name}: 埋め込み画像が空です ({reference})")
                else:
                    files.append(embedded_item)
                    reference_indices[reference] = before
                continue
            scheme = urlsplit(reference).scheme.lower()
            if scheme == "data":
                try:
                    metadata, payload = reference[5:].split(",", 1)
                    mime = metadata.split(";", 1)[0].lower()
                    if not mime.startswith("image/"):
                        raise ValueError("not an image")
                    if len(payload) > MAX_BLOB * 4:
                        raise ValueError("too large")
                    raw_image = base64.b64decode("".join(payload.split()), validate=True) if ";base64" in metadata.lower() else unquote_to_bytes(payload)
                    if not raw_image or len(raw_image) > MAX_BLOB:
                        raise ValueError("invalid image size")
                    files.append((f"inline-{len(files) + 1}" + (mimetypes.guess_extension(mime) or ".bin"), mime, raw_image))
                    reference_indices[reference] = before
                except (ValueError, TypeError):
                    report["warnings"].append(f"{path.name}: data URIの画像を読み取れません")
                continue
            if scheme or reference.startswith("//"):
                # Never fetch external URLs or silently claim a complete restore.
                report["warnings"].append(f"{path.name}: 画像を取得できません ({reference[:160]})")
                continue
            try:
                candidate = local_image_path(path.parent, reference)
            except (ValueError, OSError) as exc:
                report["warnings"].append(f"{path.name}: {exc}")
                continue
            if candidate.is_file():
                files.append((candidate.name, mimetypes.guess_type(candidate.name)[0] or "application/octet-stream", candidate.read_bytes()))
                reference_indices[reference] = before
            else:
                report["warnings"].append(f"{path.name}: 参照画像がありません ({reference[:160]})")
        # The same markup can refer to different images in another directory.
        # Include both source identity and actual attachments, while retries of
        # the same unchanged page remain idempotent. Keep .one/.onepkg IDs above.
        identity = "file-v2:" + hashlib.sha256(json_bytes({
            "source": os.path.normcase(str(path.resolve())), "document": identity,
            "attachments": [(name, mime, hashlib.sha256(data).hexdigest()) for name, mime, data in files],
        })).hexdigest()
        if source_runs is None:
            runs, end = [], 0
            for match in re.finditer(r"!\[[^]]*\]\(([^)]+)\)", text):
                runs.extend([text[end:match.start()], reference_indices.get(match[1], match[0])]); end = match.end()
            runs.append(text[end:])
        else:
            runs = []
            for run in source_runs:
                if isinstance(run, str): runs.append(run)
                elif run[0] == "image": runs.append(reference_indices.get(run[1], "[画像を取得できません]"))
                else:
                    rows = parsed.tables[run[1]]
                    try:
                        if run[1] in parsed.complex_tables: raise ValueError("complex table")
                        caption = "".join(parsed.table_captions[run[1]]).strip()
                        short = len(caption.encode("utf-16-le")) // 2 <= 100
                        value = blocks.validate({"version": 1, "kind": "table", "title": caption if short else "", "rows": rows})
                        if not short: runs.append(caption + "\n")
                        runs.append(len(files)); files.append(("表.json", inline.BLOCK_MIME, blocks.dump(value)))
                    except ValueError:
                        runs.append("\n" + "".join(parsed.table_plain[run[1]]) + "\n")
        save_page(identity, title, content, path.parent.name, "", files, runs)

    for path in ([source] if source.is_file() else sorted(source.rglob("*"))):
        if path.is_file():
            parse_file(path)
    return report
