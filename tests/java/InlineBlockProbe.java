import com.twonote.core.*;
import org.json.*;
import java.util.*;

/** Pure JVM format assertions and fixtures for the Python interoperability tests. */
public final class InlineBlockProbe {
  static int checks;
  static void check(boolean condition) { if (!condition) throw new AssertionError("inline check " + checks); checks++; }
  public static void main(String[] args) throws Exception {
    System.setOut(new java.io.PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true, java.nio.charset.StandardCharsets.UTF_8));
    String id = "a".repeat(64), vault = "b".repeat(32);
    JSONArray files = new JSONArray().put(new JSONObject().put("id", id).put("mime", "image/png"));
    String legacy = InlineBody.body("前😀後", files);
    check(legacy.equals("前😀後\n\n" + InlineBody.token(id)));
    check(InlineBody.body(InlineBody.encode(legacy), files).equals(legacy));
    check(InlineBody.body(InlineBody.encode("前後"), files).equals("前後"));
    String inserted = InlineBody.insert(InlineBody.encode("前😀後"), files, id, 3);
    check(inserted.equals(InlineBody.encode("前😀" + InlineBody.token(id) + "後")));
    check(InlineBody.insert(InlineBody.encode("前😀後"), files, id, 2).equals(InlineBody.encode("前" + InlineBody.token(id) + "😀後")));
    PendingAttachment state = new PendingAttachment(id, vault, null, false, null, 3);
    check(PendingAttachment.decode(state.encode()).offset == 3);
    JSONObject oldState = new JSONObject(state.encode()).put("version", 2); oldState.remove("offset");
    check(PendingAttachment.decode(oldState.toString()).offset == -1);
    JSONObject table = NoteBlock.parse("table", "日本語", "\"a,b\",\"line\n2\"\n\"引用\"\"符\",😀\n");
    check(NoteBlock.csv(NoteBlock.parse("table", "日本語", NoteBlock.csv(table))).equals(NoteBlock.csv(table)));
    check(NoteBlock.parse("table", "", "A\tB\n1\t2").getJSONArray("rows").length() == 2);
    for (String bad : new String[] {"NaN", "Infinity", "1e16", "not-number"}) {
      boolean rejected = false; try { NoteBlock.parse("bar", "", "A," + bad); } catch (Exception expected) { rejected = true; }
      check(rejected);
    }
    boolean rejected = false; try { NoteBlock.parse("table", "", "A,B\nC"); } catch (Exception expected) { rejected = true; } check(rejected);
    check(NoteBlock.parse("bar", "", "A,-2\nB,3").getJSONArray("rows").length() == 2);
    JSONArray normalized = new JSONArray();
    for (String number : new String[] {"１２", "1_000", "－１．５", "1e2"}) normalized.put(NoteBlock.parse("bar", "", "A," + number).getJSONArray("rows").getJSONArray(0).getString(1));
    check(normalized.toString().equals(new JSONArray(Arrays.asList("12", "1000", "-1.5", "1e2")).toString()));
    for (String number : new String[] {"1d", "1f", "0x1.0p0", "1__0"}) {
      rejected = false; try { NoteBlock.parse("bar", "", "A," + number); } catch (Exception expected) { rejected = true; } check(rejected);
    }
    for (String number : new String[] {"1d", "1f", "0x1.0p0"}) {
      JSONObject old = new JSONObject().put("version", 1).put("kind", "bar").put("title", "").put("rows", new JSONArray().put(new JSONArray().put("A").put(number)));
      JSONObject fixed = NoteBlock.load(LocalVault.utf8(old.toString()));
      check(Double.parseDouble(fixed.getJSONArray("rows").getJSONArray(0).getString(1)) == 1);
      check(NoteBlock.load(NoteBlock.dump(fixed)).getString("kind").equals("bar"));
    }
    String japanese = String.join("\n", Collections.nCopies(28, String.join(",", Collections.nCopies(10, "あ".repeat(500)))));
    JSONObject large = NoteBlock.parse("table", "", japanese); byte[] raw = NoteBlock.dump(large);
    check(raw.length > 400000 && NoteBlock.load(raw).getJSONArray("rows").length() == 28);
    JSONArray quoteRows = new JSONArray();
    for (int r = 0; r < 50; r++) { JSONArray quoteRow = new JSONArray(); for (int c = 0; c < 12; c++) quoteRow.put("\"".repeat(500)); quoteRows.put(quoteRow); }
    JSONObject quotes = new JSONObject().put("version", 1).put("kind", "table").put("title", "").put("rows", quoteRows);
    String quotedCsv = NoteBlock.csv(quotes);
    check(quotedCsv.length() > 400000 && NoteBlock.parse("table", "", quotedCsv).getJSONArray("rows").getJSONArray(0).getString(0).length() == 500);
    System.out.println(new JSONObject().put("checks", checks).put("legacy", legacy).put("inserted", inserted).put("table", table).put("normalized", normalized).put("large_bytes", raw.length));
  }
}
