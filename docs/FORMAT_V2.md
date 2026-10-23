# TwoNote vault / sync format 2

Both clients keep an independent encrypted vault. No socket is opened for local
viewing or editing. The desktop opens an HTTPS listener only for explicit sync.

`config.json`: version=2, vault_id (32 hex), iterations=600000, salt (standard
Base64, 16 bytes), wrapped_key (standard Base64). PBKDF2-HMAC-SHA256 of the UTF-8
password derives a 32-byte wrapping key. AES-256-GCM wraps a random 32-byte vault
key with AAD `twonote:2:<vault_id>:key`. Every encrypted byte sequence is a random
12-byte nonce followed by ciphertext and the 16-byte GCM authentication tag.

Objects live in `blobs/<SHA256(ciphertext)>` and `revisions/<SHA256(ciphertext)>`.
AAD is `twonote:2:<vault_id>:blob` or `twonote:2:<vault_id>:revision`.
Revisions are UTF-8 JSON with `note_id`, `parents` (revision hashes), `title`,
`content`, `notebook`, `section`, `tags`, `source_path`, `modified` (UTC string),
`deleted` (boolean), and `attachments` [{id, filename, mime, size}]. Objects are
immutable. A revision's parents belong to the same note. Concurrent revisions
remain separate heads until explicitly resolved by a revision naming both as
parents. Deletions are revisions, not immediate destruction of history.

`index.enc` uses AAD `twonote:2:<vault_id>:index`; plaintext is
`{"version":2,"revisions":[hashes...]}`. Atomic replacement publishes a complete
set after referenced objects and parents have been validated. A failed transfer
cannot advance the visible index. An encrypted backup of the prior index is kept.
Objects not referenced by an index are not displayed. Titles and attachment names
are never used as identities. No plaintext note content is written to SQLite.

Sync `/v2/index` exchanges inventories of committed revision / blob hashes.
Missing blobs, then revisions, are transferred unchanged. `/v2/commit` validates
and atomically unions revisions with the receiver's current index. Retries are
idempotent. The receiver rejects wrong-vault objects, invalid hashes, invalid
GCM tags, missing references and oversized objects. A local edit during sync
remains committed locally and is sent on the next exchange if outside the snapshot.

The first pairing uses a short-lived random pairing secret and an exact SHA-256
certificate pin carried in a `twonote://pair` QR code. Pairing issues a revocable
random device token; it does not disclose the vault key. The user enters the vault
password once on Android, then may wrap the vault key with Android Keystore under
biometric authentication. Device credentials are stored encrypted inside the
local vault. No WebView or JavaScript token bridge is used. Transport tokens alone
cannot decrypt a copied vault.
