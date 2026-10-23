package com.twonote.core;

import java.util.Arrays;
import java.util.UUID;
import org.json.JSONObject;

/** Raw, possibly incomplete form input. Never validate CSV before checkpointing. */
public final class BlockDraft {
  public static final int MAX_BYTES = 8 * 1024 * 1024;
  public static JSONObject create(JSONObject base, String target, int offset, String kind, String title, String text) throws Exception {
    return validate(new JSONObject().put("version", 1).put("draft_id", UUID.randomUUID().toString().replace("-", ""))
        .put("note_id", base.getString("note_id")).put("revision", base.getString("revision"))
        .put("target", target == null ? JSONObject.NULL : target).put("offset", offset)
        .put("kind", kind).put("title", title).put("text", text));
  }
  public static JSONObject validate(JSONObject value) throws Exception {
    if (value.getInt("version") != 1 || !value.getString("draft_id").matches("[0-9a-f]{32}")
        || !value.getString("note_id").matches("[0-9a-f]{32}") || !value.getString("revision").matches("[0-9a-f]{64}")
        || (!value.isNull("target") && !value.getString("target").matches("[0-9a-f]{64}"))
        || value.getInt("offset") < -1 || value.getInt("offset") > LocalVault.MAX_REVISION * 2
        || !Arrays.asList("table", "bar", "line").contains(value.getString("kind"))
        || !(value.get("title") instanceof String) || value.getString("title").length() > 4096
        || !(value.get("text") instanceof String) || value.getString("text").length() > 1000000)
      throw new IllegalArgumentException("表・グラフの下書き形式またはサイズが不正です");
    if (LocalVault.utf8(value.toString()).length > MAX_BYTES) throw new IllegalArgumentException("表・グラフの下書きは8MBまでです");
    return value;
  }
}
