package com.twonote.core;

import java.util.*;
import org.json.*;

/** Bounded CSV/table/chart data, shared with the desktop format. No formula execution. */
public final class NoteBlock {
  public static final int MAX_BYTES = 2 * 1024 * 1024, MAX_INPUT_UNITS = 1000000;
  public static String numberText(String text, boolean legacy) {
    String from = "０１２３４５６７８９＋－．Ｅｅ", to = "0123456789+-.Ee";
    StringBuilder mapped = new StringBuilder();
    for (int i = 0; i < text.length(); i++) { char c = text.charAt(i); int p = from.indexOf(c); mapped.append(p < 0 ? c : to.charAt(p)); }
    String value = mapped.toString().replaceAll("^[ \\t\\r\\n\\f]+|[ \\t\\r\\n\\f]+$", "").replaceAll("(?<=[0-9])_(?=[0-9])", "");
    if (legacy) value = value.replaceFirst("[dDfF]$", "");
    boolean hex = legacy && value.matches("[+-]?0[xX](?:[0-9a-fA-F]+(?:\\.[0-9a-fA-F]*)?|\\.[0-9a-fA-F]+)[pP][+-]?[0-9]+");
    if (!hex && !value.matches("[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?"))
      throw new IllegalArgumentException("グラフの2列目は10進数を入力してください");
    double number = Double.parseDouble(value);
    if (!Double.isFinite(number) || Math.abs(number) > 1e15) throw new IllegalArgumentException("数値は有限の±10^15以内です");
    return hex ? java.math.BigDecimal.valueOf(number).stripTrailingZeros().toString() : value;
  }
  public static JSONObject validate(JSONObject value) throws Exception {
    return validate(value, false);
  }
  private static JSONObject validate(JSONObject input, boolean legacy) throws Exception {
    JSONObject value = LocalVault.copy(input);
    String kind = value.getString("kind");
    if (value.getInt("version") != 1 || !Arrays.asList("table", "bar", "line").contains(kind)) throw new IllegalArgumentException("表・グラフの形式が不正です");
    JSONArray rows = value.getJSONArray("rows");
    if (rows.length() < 1 || rows.length() > 50) throw new IllegalArgumentException("表は1〜50行です");
    int columns = rows.getJSONArray(0).length();
    if (columns < 1 || columns > 12) throw new IllegalArgumentException("表は1〜12列です");
    for (int i = 0; i < rows.length(); i++) {
      JSONArray row = rows.getJSONArray(i);
      if (row.length() != columns) throw new IllegalArgumentException("各行の列数を揃えてください");
      for (int j = 0; j < columns; j++) if (!(row.get(j) instanceof String) || row.getString(j).length() > 500)
        throw new IllegalArgumentException("セルは500文字以内です");
      if (!kind.equals("table")) {
        if (columns != 2 || rows.length() > 30) throw new IllegalArgumentException("グラフは項目名,数値の2列、30項目までです（見出し行不要）");
        row.put(1, numberText(row.getString(1), legacy));
      }
    }
    if (!(value.opt("title") instanceof String) || value.getString("title").length() > 100) throw new IllegalArgumentException("見出しは100文字以内です");
    return value;
  }
  public static JSONObject load(byte[] raw) throws Exception {
    if (raw.length > MAX_BYTES) throw new IllegalArgumentException("データが大きすぎます");
    return validate(new JSONObject(new String(raw, java.nio.charset.StandardCharsets.UTF_8)), true);
  }
  public static byte[] dump(JSONObject value) throws Exception {
    byte[] raw = LocalVault.utf8(validate(value).toString());
    if (raw.length > MAX_BYTES) throw new IllegalArgumentException("表・グラフの保存データは2MBまでです");
    return raw;
  }
  public static JSONObject parse(String kind, String title, String text) throws Exception {
    if (text.length() > MAX_INPUT_UNITS) throw new IllegalArgumentException("データが大きすぎます");
    char delimiter = ','; boolean inQuotes = false;
    for (int i = 0; i < text.length(); i++) {
      if (text.charAt(i) == '"') inQuotes = !inQuotes;
      else if (text.charAt(i) == '\t' && !inQuotes) { delimiter = '\t'; break; }
    }
    JSONArray rows = new JSONArray(), row = new JSONArray();
    StringBuilder cell = new StringBuilder(); boolean quoted = false, closed = false, started = false;
    for (int i = 0; i < text.length(); i++) {
      char c = text.charAt(i);
      if (quoted) {
        if (c == '"') {
          if (i + 1 < text.length() && text.charAt(i + 1) == '"') { cell.append('"'); i++; }
          else { quoted = false; closed = true; }
        } else cell.append(c);
      } else if (c == delimiter || c == '\n' || c == '\r') {
        row.put(cell.toString()); cell.setLength(0); closed = false; started = false;
        if (c != delimiter) {
          rows.put(row); row = new JSONArray();
          if (c == '\r' && i + 1 < text.length() && text.charAt(i + 1) == '\n') i++;
        }
      } else if (c == '"' && !started) { quoted = true; started = true; }
      else { if (closed) throw new IllegalArgumentException("CSVの引用符が不正です"); cell.append(c); started = true; }
    }
    if (quoted) throw new IllegalArgumentException("CSVの引用符を閉じてください");
    if (started || closed || cell.length() > 0 || row.length() > 0) { row.put(cell.toString()); rows.put(row); }
    JSONObject value = validate(new JSONObject().put("version", 1).put("kind", kind).put("title", title).put("rows", rows));
    dump(value);
    return value;
  }
  public static String csv(JSONObject value) throws Exception {
    JSONArray rows = validate(value).getJSONArray("rows"); StringBuilder out = new StringBuilder();
    for (int i = 0; i < rows.length(); i++) {
      JSONArray row = rows.getJSONArray(i);
      for (int j = 0; j < row.length(); j++) {
        if (j > 0) out.append(',');
        out.append('"').append(row.getString(j).replace("\"", "\"\"")).append('"');
      }
      out.append('\n');
    }
    return out.toString();
  }
}
