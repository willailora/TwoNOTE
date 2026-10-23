package com.twonote.core;

import org.json.JSONObject;

/** Saved Activity state contains references only, never note text or vault keys. */
public final class PendingAttachment {
  public final String revision, vaultId, uri, captureName;
  public final boolean ownsGrant;
  public final int offset;
  public PendingAttachment(String revision, String vaultId, String uri, boolean ownsGrant) {
    this(revision, vaultId, uri, ownsGrant, null);
  }
  public PendingAttachment(String revision, String vaultId, String uri, boolean ownsGrant, String captureName) {
    this(revision, vaultId, uri, ownsGrant, captureName, -1);
  }
  public PendingAttachment(String revision, String vaultId, String uri, boolean ownsGrant, String captureName, int offset) {
    if (revision == null || !revision.matches("[0-9a-f]{64}") || vaultId == null || !vaultId.matches("[0-9a-f]{32}"))
      throw new IllegalArgumentException("添付先の識別情報が不正です");
    if (uri != null && (!uri.startsWith("content://") || uri.length() > 16384))
      throw new IllegalArgumentException("添付ファイルの参照が不正です");
    if (captureName != null && (!CaptureFiles.validName(captureName) || ownsGrant))
      throw new IllegalArgumentException("撮影状態が不正です");
    this.revision = revision; this.vaultId = vaultId; this.uri = uri; this.ownsGrant = ownsGrant;
    this.captureName = captureName;
    if (offset < -1 || offset > LocalVault.MAX_REVISION * 2) throw new IllegalArgumentException("挿入位置が不正です");
    this.offset = offset;
  }
  public String encode() throws Exception {
    return new JSONObject().put("version", 3).put("revision", revision).put("vault", vaultId).put("offset", offset)
        .put("uri", uri == null ? JSONObject.NULL : uri).put("ownsGrant", ownsGrant)
        .put("captureName", captureName == null ? JSONObject.NULL : captureName).toString();
  }
  public static PendingAttachment decode(String raw) throws Exception {
    if (raw == null) return null;
    if (raw.length() > 20000) throw new IllegalArgumentException("添付状態が大きすぎます");
    JSONObject value = new JSONObject(raw);
    int version = value.getInt("version");
    if (version < 1 || version > 3) throw new IllegalArgumentException("添付状態の形式が不正です");
    return new PendingAttachment(value.getString("revision"), value.getString("vault"),
        value.isNull("uri") ? null : value.getString("uri"), value.optBoolean("ownsGrant"),
        version == 1 || value.isNull("captureName") ? null : value.getString("captureName"), version < 3 ? -1 : value.getInt("offset"));
  }
}
