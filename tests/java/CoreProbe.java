import com.twonote.core.LocalVault;
import com.twonote.core.SyncClient;
import com.twonote.core.InlineBody;
import com.twonote.core.NoteBlock;
import com.twonote.core.TreeNavigation;
import java.io.File;
import java.util.Collections;
import org.json.JSONObject;

/** JVM interoperability driver; uses synthetic test credentials only. */
public final class CoreProbe {
  public static void main(String[] args) throws Exception {
    System.setOut(
        new java.io.PrintStream(
            new java.io.FileOutputStream(java.io.FileDescriptor.out),
            true,
            java.nio.charset.StandardCharsets.UTF_8));
    String command = args[0];
    File path = new File(args[1]);
    String password = args[2];
    if (command.equals("pair")) {
      JSONObject peer = SyncClient.parsePairing(args[3]);
      JSONObject paired = SyncClient.pair(peer, "java-contract-device");
      peer.put("token", paired.getString("token"));
      peer.remove("secret");
      LocalVault vault = LocalVault.create(path, paired.getJSONObject("config"), password);
      vault.setSettings(new JSONObject().put("peer", peer));
      System.out.println(SyncClient.exchange(vault, peer, false, message -> {}));
      vault.lock();
      return;
    }
    LocalVault vault = LocalVault.open(path, password);
    if (command.equals("heads-json")) {
      int visible = 0;
      for (TreeNavigation.Node n : new TreeNavigation(vault).rows("", false)) if (n.note != null) visible++;
      System.out.println(new JSONObject().put("live", new org.json.JSONArray(vault.heads(false)))
          .put("all", new org.json.JSONArray(vault.heads(true))).put("visible", visible));
      vault.lock(); return;
    }
    if (command.equals("resolve-versions")) {
      System.out.println(vault.resolveVersions(args[3], LocalVault.strings(new org.json.JSONArray(args[4])), args[5].equals("merge")));
      vault.lock(); return;
    }
    if (command.equals("set-deleted")) {
      System.out.println(vault.setDeleted(args[3], Boolean.parseBoolean(args[4])));
      vault.lock(); return;
    }
    if (command.equals("inline-edit")) {
      JSONObject note = vault.heads(false).get(0);
      JSONObject chart = NoteBlock.parse("bar", "同期グラフ", "A,-2\nB,3");
      JSONObject attachment = vault.addBlob("chart.json", InlineBody.BLOCK_MIME, LocalVault.utf8(chart.toString()));
      String image = note.getJSONArray("attachments").getJSONObject(0).getString("id");
      String content = InlineBody.encode("前😀" + InlineBody.token(image) + "中" + InlineBody.token(attachment.getString("id")) + "後");
      note.getJSONArray("attachments").put(attachment);
      vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")), LocalVault.copy(note).put("content", content));
      System.out.println(new JSONObject().put("content", content).put("chart", attachment.getString("id")));
      vault.lock(); return;
    }
    if (command.equals("read")) {
      JSONObject note = vault.heads(false).get(0);
      String blob = note.getJSONArray("attachments").getJSONObject(0).getString("id");
      System.out.println(
          new JSONObject()
              .put("title", note.getString("title"))
              .put("content", note.getString("content"))
              .put("blob", new String(vault.readBlob(blob), "UTF-8"))
              .put("heads", vault.heads(false).size()));
    } else if (command.equals("edit")) {
      JSONObject note = vault.heads(false).get(0);
      note.put("content", "Androidの編集");
      note.getJSONArray("attachments")
          .put(vault.addBlob("Android画像.png", "image/png", LocalVault.utf8("ANDROID-IMAGE")));
      JSONObject saved =
          vault.save(
              note.getString("note_id"),
              Collections.singletonList(note.getString("revision")),
              note);
      System.out.println(new JSONObject().put("revision", saved.getString("revision")));
    } else if (command.equals("sync")) {
      System.out.println(
          SyncClient.exchange(vault, vault.settings().getJSONObject("peer"), false, message -> {}));
    } else if (command.equals("lock")) {
      vault.lock();
      try {
        vault.heads(false);
        throw new AssertionError("locked vault readable");
      } catch (IllegalStateException expected) {
        System.out.println("{\"locked\":true}");
      }
      return;
    } else throw new IllegalArgumentException("unknown command");
    vault.lock();
  }
}
