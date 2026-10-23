package com.twonote.core;

import java.util.*;
import java.util.regex.*;
import org.json.*;

/** Portable references only; HTML, file URLs and remote URLs are never rendered. */
public final class InlineBody {
  public static final String HEADER = "<!--TwoNote:inline:v1-->\n";
  public static final String BLOCK_MIME = "application/vnd.twonote.block+json";
  public static final Pattern TOKEN = Pattern.compile("!\\[TwoNote\\]\\(twonote-embed://([0-9a-f]{64})\\)");
  public static boolean supported(JSONObject item) {
    String mime = item.optString("mime");
    return mime.equals(BLOCK_MIME) || (mime.startsWith("image/") && !mime.equals("image/svg+xml"));
  }
  public static String token(String id) {
    if (!id.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("invalid inline ID");
    return "![TwoNote](twonote-embed://" + id + ")";
  }
  public static String body(String content, JSONArray files) throws JSONException {
    if (content.startsWith(HEADER)) return content.substring(HEADER.length());
    StringBuilder result = new StringBuilder(content); boolean first = true;
    for (int i = 0; i < files.length(); i++) if (supported(files.getJSONObject(i))) {
      if (result.length() > 0) result.append(first ? "\n\n" : "\n");
      result.append(token(files.getJSONObject(i).getString("id"))); first = false;
    }
    return result.toString();
  }
  public static String encode(String body) { return HEADER + body; }
  public static String insert(String content, JSONArray oldFiles, String id, int offset) throws JSONException {
    String text = body(content, oldFiles);
    int at = offset < 0 ? text.length() : Math.max(0, Math.min(offset, text.length()));
    // Offsets are UTF-16 (Android selection/Bundle), never split emoji or an embed token.
    if (at > 0 && at < text.length() && Character.isLowSurrogate(text.charAt(at)) && Character.isHighSurrogate(text.charAt(at - 1))) at--;
    Matcher matcher = TOKEN.matcher(text);
    while (matcher.find()) if (at > matcher.start() && at < matcher.end()) { at = matcher.start(); break; }
    return encode(text.substring(0, at) + token(id) + text.substring(at));
  }
  public static String merge(List<JSONObject> notes) throws JSONException {
    Set<String> bodies = new LinkedHashSet<>();
    for (JSONObject n : notes) bodies.add(body(n.getString("content"), n.getJSONArray("attachments")));
    return encode(String.join("\n\n--- 別の編集版 ---\n\n", bodies));
  }
}
