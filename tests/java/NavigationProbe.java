import com.twonote.core.LocalVault;
import com.twonote.core.TreeNavigation;
import com.twonote.core.TreeNavigation.Node;
import java.io.File;
import java.nio.file.Files;
import java.util.*;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.json.JSONObject;

/** Small synthetic storage smoke test; no Android runtime, graphics or real notes. */
public final class NavigationProbe {
  private static int checks;
  private static void check(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
    checks++;
  }
  private static Node node(JSONObject n) {
    return new Node("note", n.optString("notebook"), n.optString("section"), n);
  }
  private static JSONObject note(LocalVault vault, String title, String book, String section) throws Exception {
    return vault.save(UUID.randomUUID().toString().replace("-", ""), Collections.emptyList(),
        new JSONObject().put("title", title).put("content", "synthetic body " + title).put("notebook", book).put("section", section));
  }
  private static List<String> ids(TreeNavigation tree, String book, String section) throws Exception {
    List<String> result = new ArrayList<>();
    for (Node n : tree.rows("", true)) if (n.note != null && n.book.equals(book) && n.section.equals(section)) result.add(n.note.getString("note_id"));
    return result;
  }
  public static void main(String[] args) throws Exception {
    File root = new File(args[0]);
    String password = "synthetic-navigation-only", id = UUID.randomUUID().toString().replace("-", "");
    byte[] salt = new byte[16], key = new byte[32]; Arrays.fill(salt, (byte) 7); Arrays.fill(key, (byte) 19);
    byte[] wrapping = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        .generateSecret(new PBEKeySpec(password.toCharArray(), salt, LocalVault.ITERATIONS, 256)).getEncoded();
    JSONObject config = new JSONObject().put("version", 2).put("vault_id", id).put("iterations", LocalVault.ITERATIONS)
        .put("salt", Base64.getEncoder().encodeToString(salt))
        .put("wrapped_key", Base64.getEncoder().encodeToString(LocalVault.seal(wrapping, key, LocalVault.aad(id, "key"))));
    LocalVault vault = LocalVault.create(root, config, password);
    try {
      JSONObject a = note(vault, "A", "Book", "Ideas"), b = note(vault, "B", "Book", "Ideas"), c = note(vault, "C", "Work", "Plans");
      JSONObject image = vault.addBlob("fixture.png", "image/png", LocalVault.utf8("fixture-image"));
      a = vault.save(a.getString("note_id"), Collections.singletonList(a.getString("revision")), LocalVault.copy(a).put("attachments", new org.json.JSONArray().put(image)));
      TreeNavigation tree = new TreeNavigation(vault);
      check(tree.rows("", false).size() == 7, "hierarchy");
      String inventory = vault.inventory().toString();
      tree.move(node(b), node(a), false);
      check(vault.inventory().toString().equals(inventory), "order must not revise text");
      check(ids(tree, "Book", "Ideas").equals(Arrays.asList(b.getString("note_id"), a.getString("note_id"))), "manual order");
      JSONObject latest = vault.save(a.getString("note_id"), Collections.singletonList(a.getString("revision")), LocalVault.copy(a).put("content", "latest unsynced text"));
      Node section = new Node("section", "Book", "Ideas", null);
      tree.toggle(section);
      check(new TreeNavigation(vault).collapsed(section), "collapse persistence");
      check(tree.rows("latest unsynced", false).stream().filter(n -> n.note != null).count() == 1, "search opens parents");
      tree.renameSection("Book", "Ideas", "日本語資料");
      Node renamed = new Node("section", "Book", "日本語資料", null);
      check(tree.collapsed(renamed), "rename keeps collapse"); tree.toggle(renamed);
      check(ids(tree, "Book", "日本語資料").equals(Arrays.asList(b.getString("note_id"), a.getString("note_id"))), "rename keeps order");
      JSONObject moved = tree.move(node(a), node(c), false);
      check(moved.getString("content").equals("latest unsynced text"), "move latest draft");
      check(moved.getJSONArray("attachments").getJSONObject(0).getString("id").equals(image.getString("id")), "images unchanged");
      check(vault.get(a.getString("revision")).getString("section").equals("Ideas"), "history retained");
      check(ids(tree, "Work", "Plans").equals(Arrays.asList(a.getString("note_id"), c.getString("note_id"))), "move before target");
      tree.addFolder("Work", "Taken");
      boolean refused = false;
      try { tree.renameSection("Work", "Plans", "Taken"); } catch (IllegalArgumentException expected) { refused = true; }
      check(refused, "duplicate refusal");
      String before = vault.inventory().toString(), layout = vault.navigation().toString();
      File index = new File(root, "index.enc"), saved = new File(root, "index.test-saved.enc");
      Files.move(index.toPath(), saved.toPath()); Files.createDirectory(index.toPath());
      boolean failed = false;
      try { tree.renameSection("Work", "Plans", "ShouldNotPublish"); } catch (Exception expected) { failed = true; }
      finally { Files.delete(index.toPath()); Files.move(saved.toPath(), index.toPath()); }
      check(failed && vault.inventory().toString().equals(before), "failed batch publishes no article");
      check(vault.navigation().toString().equals(layout), "failed batch rolls back layout");
      JSONObject branch = null;
      for (JSONObject n : vault.heads(true)) if (n.getString("note_id").equals(b.getString("note_id"))) branch = n;
      vault.save(branch.getString("note_id"), Collections.singletonList(branch.getString("revision")), LocalVault.copy(branch).put("content", "branch 1"));
      vault.save(branch.getString("note_id"), Collections.singletonList(branch.getString("revision")), LocalVault.copy(branch).put("content", "branch 2").put("deleted", true));
      tree.renameSection("Book", "日本語資料", "ConflictsKept");
      List<JSONObject> heads = vault.heads(true); int branches = 0, tombstones = 0;
      for (JSONObject n : heads) if (n.getString("note_id").equals(b.getString("note_id"))) {
        branches++; if (n.getBoolean("deleted")) tombstones++;
        check(n.optInt("conflicts") == 1 && n.getString("section").equals("ConflictsKept"), "rename retains deleted history without counting it as a live conflict");
      }
      check(branches == 2 && tombstones == 1, "deleted branch retained");
      Node liveBranch = null; int visibleBranches = 0;
      for (Node n : tree.rows("", false)) if (n.note != null && n.note.getString("note_id").equals(b.getString("note_id"))) {
        liveBranch = n; visibleBranches++;
      }
      check(visibleBranches == 1 && !liveBranch.blocked(), "deleted branch is hidden and does not block the survivor");
      tree.addFolder("Book", "MoveTarget");
      JSONObject movedLive = tree.move(liveBranch, new Node("section", "Book", "MoveTarget", null), true);
      check(movedLive.getString("section").equals("MoveTarget"), "survivor can be moved despite a deleted historical head");
      check(!new String(Files.readAllBytes(new File(root, "navigation.enc").toPath()), java.nio.charset.StandardCharsets.UTF_8).contains("ConflictsKept"), "encrypted layout");
      vault.lock();
      vault = LocalVault.open(root, password);
      check(new TreeNavigation(vault).destinations().stream().anyMatch(n -> n.section.equals("ConflictsKept")), "restart persistence");
      System.out.println("Navigation storage checks passed: " + checks);
    } finally { vault.lock(); }
  }
}
