"""Explicit, pinned-TLS ciphertext exchange. Never used for local editing."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization

import tls
from .vault import MAX_BLOB, MAX_RECORDS, MAX_REVISION, LockedError, Vault, VaultError, digest, json_bytes

MAX_JSON = 16 * 1024 * 1024


class SyncServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, owner, context):
        self.owner = owner
        self.context = context
        self.connections: set[socket.socket] = set()
        self.connection_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.closing = False
        super().__init__(address, SyncHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        with self.connection_lock:
            if self.closing:
                request.close()
                self.slots.release()
                return
            self.connections.add(request)
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()

    def replace_connection(self, raw, encrypted):
        with self.connection_lock:
            self.connections.discard(raw)
            if self.closing:
                encrypted.close()
                raise OSError("Sync listener is closing")
            self.connections.add(encrypted)

    def handle_error(self, request, client_address):
        # No note contents, tokens, passwords or unauthenticated request bodies in logs.
        pass

    def close_connections(self):
        with self.connection_lock:
            self.closing = True
            for connection in list(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            self.connections.clear()


class SyncHandler(BaseHTTPRequestHandler):
    server: SyncServer
    protocol_version = "HTTP/1.0"

    def setup(self):
        # Handshake in the bounded worker, never in the listening / accept thread.
        raw, encrypted = self.request, None
        try:
            raw.settimeout(5)
            encrypted = self.server.context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            self.server.replace_connection(raw, encrypted)
            self.request = encrypted
            encrypted.do_handshake()
            encrypted.settimeout(30)
            super().setup()
        except BaseException:
            # BaseRequestHandler never calls finish() when setup() raises.
            for name in ("rfile", "wfile"):
                stream = getattr(self, name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            with self.server.connection_lock:
                self.server.connections.discard(raw)
                self.server.connections.discard(encrypted)
            if encrypted is not None:
                encrypted.close()
            raw.close()
            raise

    def finish(self):
        try:
            super().finish()
        finally:
            with self.server.connection_lock:
                self.server.connections.discard(self.request)
            self.request.close()

    def log_message(self, *args):
        pass

    def send_data(self, status: int, data: bytes, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def reply(self, status, value):
        self.send_data(status, json_bytes(value))

    def read_body(self, limit=MAX_JSON):
        if self.headers.get("Transfer-Encoding"):
            raise VaultError("Transfer-Encodingには対応していません")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise VaultError("サイズが不正です") from exc
        if not 0 <= length <= limit:
            raise VaultError("送信サイズが大きすぎます")
        result = self.rfile.read(length)
        if len(result) != length:
            raise VaultError("通信が途中で切れました")
        return result

    def token(self):
        header = self.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def dispatch(self, method):
        owner = self.server.owner
        path = urlparse(self.path).path
        try:
            if not owner.active or owner.server is not self.server:
                self.reply(503, {"error": "同期待受は終了しています"})
                return
            if self.headers.get("Origin"):
                self.reply(403, {"error": "ブラウザからの接続は受け付けません"})
                return
            if method == "POST" and path == "/v2/pair":
                request = json.loads(self.read_body(4096))
                result = owner.pair(self.token(), request)
                self.reply(200, result)
                return
            peer_id = owner.authorize(self.token())
            if peer_id is None:
                self.reply(401, {"error": "同期端末が認証されていません"})
                return
            vault = owner.vault
            if method == "GET" and path == "/v2/index":
                self.reply(200, vault.inventory())
                return
            pieces = path.strip("/").split("/")
            if len(pieces) == 4 and pieces[:2] == ["v2", "objects"]:
                kind, object_id = pieces[2:]
                vault.object_path(kind, object_id)
                if method == "GET":
                    inventory = vault.inventory()
                    if object_id not in inventory[kind]:
                        self.reply(404, {"error": "公開されていないオブジェクトです"})
                        return
                    self.send_data(200, vault.read_object(kind, object_id), "application/octet-stream")
                    return
                if method == "PUT":
                    limit = MAX_BLOB if kind == "blobs" else MAX_REVISION
                    vault.stage_object(kind, object_id, self.read_body(limit + 28))
                    self.reply(200, {"ok": True})
                    return
            if method == "POST" and path == "/v2/commit":
                body = json.loads(self.read_body())
                if body.get("vault_id") != vault.vault_id:
                    raise VaultError("違う保管庫への同期です")
                added = vault.commit(body["revisions"])
                if added:
                    owner.notify("changed")
                self.reply(200, {"added": added})
                return
            if method == "POST" and path == "/v2/done":
                self.reply(200, {"ok": True})
                # The reply is flushed before the listener and connections close.
                timer = threading.Timer(0.2, owner.stop, args=(self.server,))
                timer.daemon = True
                timer.start()
                return
            self.reply(404, {"error": "同期APIがありません"})
        except LockedError:
            self.reply(401, {"error": "PCの保管庫がロックされています"})
        except (KeyError, TypeError, ValueError, UnicodeError):
            self.reply(400, {"error": "同期データを検証できませんでした"})
        except FileNotFoundError:
            self.reply(404, {"error": "同期データがありません"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")


class SyncHost:
    def __init__(self, vault: Vault, notify=lambda event: None):
        self.vault = vault
        self.notify = notify
        self.mutex = threading.RLock()
        self.server: SyncServer | None = None
        self.thread = None
        self.timer = None
        self.pair_secret = ""
        self.expires = 0.0
        self.pin = ""

    @property
    def active(self):
        with self.mutex:
            return self.server is not None and time.monotonic() < self.expires

    def start(self, host="0.0.0.0", port=8001, seconds=900):
        with self.mutex:
            self.vault._key()
            if self.server is not None:
                return self.server.server_address[1]
            cert = self.vault.root / "tls.crt"
            key = self.vault.root / "tls.key"
            tls_password = self.vault._key().hex().encode("ascii")
            tls.ensure_certificate(cert, key, tls_password)
            certificate = x509.load_pem_x509_certificate(cert.read_bytes())
            self.pin = digest(certificate.public_bytes(serialization.Encoding.DER))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key, password=tls_password)
            self.server = SyncServer((host, port), self, context)
            self.pair_secret = secrets.token_urlsafe(32)
            self.expires = time.monotonic() + seconds
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self.thread.start()
            self.timer = threading.Timer(seconds, self.stop, args=(self.server,))
            self.timer.daemon = True
            self.timer.start()
            self.notify("started")
            return self.server.server_address[1]

    def stop(self, expected_server=None):
        with self.mutex:
            server = self.server
            if server is None or (expected_server is not None and server is not expected_server):
                return
            # Keep the mutex until closure so a restart cannot race with shutdown.
            if self.timer:
                self.timer.cancel()
            server.close_connections()
            server.shutdown()
            server.server_close()
            server.close_connections()
            self.server = None
            self.pair_secret = ""
            self.expires = 0.0
        self.notify("stopped")

    def pairing_uri(self, address):
        with self.mutex:
            if not self.active:
                raise VaultError("同期待受が開いていません")
            return "twonote://pair?" + urlencode({"v": 2, "host": address,
                "port": self.server.server_address[1], "vault": self.vault.vault_id,
                "pin": self.pin, "secret": self.pair_secret})

    def pair(self, secret: str, request: dict):
        with self.mutex:
            if not self.active or not self.pair_secret or not hmac.compare_digest(secret, self.pair_secret):
                raise VaultError("ペアリングコードが無効または使用済みです")
            device_id = request["device_id"]
            if not isinstance(device_id, str) or len(device_id) > 80:
                raise VaultError("端末IDが不正です")
            token = secrets.token_urlsafe(32)
            settings = self.vault.settings()
            peers = settings.setdefault("peers", {})
            peers[device_id] = {"label": str(request.get("label", "Android"))[:80], "token_hash": digest(token.encode("ascii"))}
            self.vault.set_settings(settings)
            self.pair_secret = ""
            self.notify("paired")
            return {"config": self.vault.config, "token": token, "device_id": device_id}

    def authorize(self, token):
        if not token or len(token) > 256:
            return None
        hashed = digest(token.encode("utf-8"))
        with self.mutex:
            for peer_id, peer in self.vault.settings().get("peers", {}).items():
                if hmac.compare_digest(hashed, peer["token_hash"]):
                    return peer_id
        return None

    def revoke_all(self):
        self.stop()
        settings = self.vault.settings()
        settings["peers"] = {}
        self.vault.set_settings(settings)


def parse_pairing(uri: str) -> dict:
    parsed = urlparse(uri.strip())
    if parsed.scheme != "twonote" or parsed.netloc != "pair":
        raise VaultError("TwoNoteのペアリングコードではありません")
    args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if args.get("v") != "2" or not 0 < int(args["port"]) < 65536:
        raise VaultError("ペアリングコードが不正です")
    if not (len(args["pin"]) == 64 and len(args["vault"]) == 32 and len(args["secret"]) >= 32):
        raise VaultError("ペアリングコードが不正です")
    return {"host": args["host"], "port": int(args["port"]), "pin": args["pin"],
            "vault_id": args["vault"], "secret": args["secret"]}


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, peer: dict):
        self.pin = peer["pin"]
        # The QR-provided exact certificate pin replaces CA / hostname trust.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(peer["host"], peer["port"], context=context, timeout=15)

    def connect(self):
        super().connect()
        if not hmac.compare_digest(digest(self.sock.getpeercert(binary_form=True)), self.pin):
            self.close()
            raise VaultError("同期相手の証明書が変わっています。PCで再ペアリングしてください")


def request(peer, method, path, body=None, token=None, binary=False):
    headers = {"Authorization": "Bearer " + (token if token is not None else peer["token"])}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
        if not binary:
            body = json_bytes(body)
    conn = PinnedConnection(peer)
    try:
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        raw = response.read(MAX_BLOB + 29)
        if len(raw) > MAX_BLOB + 28:
            raise VaultError("同期応答が大きすぎます")
        if response.status != 200:
            raise VaultError(f"同期に失敗しました (HTTP {response.status})")
        return raw if binary and method == "GET" else json.loads(raw)
    finally:
        conn.close()


def exchange(vault: Vault, peer: dict, finish=True, progress=lambda message: None):
    remote = request(peer, "GET", "/v2/index")
    if remote.get("vault_id") != vault.vault_id:
        raise VaultError("同期先の保管庫が異なります")
    local = vault.inventory()
    stats = {"uploaded": 0, "downloaded": 0}
    for kind in ("blobs", "revisions"):
        if not isinstance(remote.get(kind), list) or len(remote[kind]) > MAX_RECORDS:
            raise VaultError("同期一覧が不正です")
        for oid in sorted(set(local[kind]) - set(remote[kind])):
            progress("変更を送信中…")
            request(peer, "PUT", f"/v2/objects/{kind}/{oid}", vault.read_object(kind, oid), binary=True)
            stats["uploaded"] += 1
        for oid in sorted(set(remote[kind]) - set(local[kind])):
            progress("変更を受信中…")
            raw = request(peer, "GET", f"/v2/objects/{kind}/{oid}", binary=True)
            vault.stage_object(kind, oid, raw)
            stats["downloaded"] += 1
    # Publish only after both object phases are complete. Retries are idempotent.
    request(peer, "POST", "/v2/commit", {"vault_id": vault.vault_id, "revisions": local["revisions"]})
    vault.commit(remote["revisions"])
    if finish:
        request(peer, "POST", "/v2/done", {})
    return stats


def lan_addresses():
    try:
        values = socket.gethostbyname_ex(socket.gethostname())[2]
    except socket.gaierror:
        values = []
    addresses = [ip for ip in dict.fromkeys(values) if ipaddress.ip_address(ip).is_private and not ip.startswith("127.")]
    return sorted(addresses, key=lambda ip: (not ip.startswith("192.168."), ip)) or ["127.0.0.1"]
