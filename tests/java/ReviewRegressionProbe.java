import com.twonote.core.LocalVault;
import com.twonote.core.PendingAttachment;
import com.twonote.core.TreeNavigation;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.json.JSONArray;
import org.json.JSONObject;

/** Storage and saved-state regressions on synthetic data; no Android GUI. */
public final class ReviewRegressionProbe {
  private static int checks;
  private static void check(boolean condition, String message) {
    if (!condition) throw new AssertionError(message); checks++;
  }
  public static void main(String[] args) throws Exception {
    File root = new File(args[0]);
    String password = "synthetic-review-regressions", id = UUID.randomUUID().toString().replace("-", "");
    byte[] key = new byte[32], salt = new byte[16]; Arrays.fill(key, (byte) 13); Arrays.fill(salt, (byte) 9);
    byte[] wrapping = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        .generateSecret(new PBEKeySpec(password.toCharArray(), salt, LocalVault.ITERATIONS, 256)).getEncoded();
    JSONObject config = new JSONObject().put("version", 2).put("vault_id", id).put("iterations", LocalVault.ITERATIONS)
        .put("salt", Base64.getEncoder().encodeToString(salt))
        .put("wrapped_key", Base64.getEncoder().encodeToString(LocalVault.seal(wrapping, key, LocalVault.aad(id, "key"))));
    LocalVault vault = LocalVault.create(root, config, password);
    try {
      JSONObject image = vault.addBlob("fixture.png", "image/png", LocalVault.utf8("fixture-image"));
      JSONObject note = vault.save(UUID.randomUUID().toString().replace("-", ""), Collections.emptyList(),
          new JSONObject().put("title", "Draft fixture").put("content", "original").put("notebook", "Book").put("section", "Ideas")
              .put("attachments", new JSONArray().put(image)));
      String marker = "PRIVATE-SYNTHETIC-UNSAVED-DRAFT";
      JSONObject draft = LocalVault.copy(note).put("content", marker);
      vault.saveDraft(draft);
      byte[] encrypted = Files.readAllBytes(new File(root, "draft.enc").toPath());
      check(!new String(encrypted, StandardCharsets.UTF_8).contains(marker), "draft is ciphertext");
      vault.lock(); vault = LocalVault.open(root, password);
      JSONObject recovered = vault.readDraft();
      check(recovered.getString("content").equals(marker), "draft survives new vault instance");
      check(recovered.getString("revision").equals(note.getString("revision")), "draft preserves edit base");
      check(recovered.getJSONArray("attachments").toString().equals(note.getJSONArray("attachments").toString()), "draft retains images");
      note = vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")), recovered);
      // Simulate process death after committing a note but before draft cleanup.
      vault.lock(); vault = LocalVault.open(root, password);
      check(vault.readDraft() == null, "already committed draft is not replayed");

      draft = LocalVault.copy(note).put("content", "local draft after remote edit"); vault.saveDraft(draft);
      vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")), LocalVault.copy(note).put("content", "remote update"));
      recovered = vault.readDraft();
      check(recovered.getString("content").equals("local draft after remote edit"), "remote update does not erase draft");
      note = vault.save(note.getString("note_id"), Collections.singletonList(recovered.getString("revision")), recovered);
      check(vault.heads(false).size() == 2, "recovering draft preserves both concurrent branches");
      vault.clearDraft(note.getString("note_id"));

      String large = "x".repeat(LocalVault.MAX_REVISION + 32);
      draft = LocalVault.copy(note).put("content", large);
      boolean rejected = false;
      try { vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")), draft); }
      catch (IllegalArgumentException expected) { rejected = true; }
      check(rejected, "oversize ordinary save rejected");
      vault.saveDraft(draft); vault.lock(); vault = LocalVault.open(root, password);
      check(vault.readDraft().getString("content").length() == large.length(), "failed oversize save can still recover editable draft");
      vault.clearDraft("f".repeat(32));
      check(new File(root, "draft.enc").isFile(), "other article cannot clear a draft");
      vault.clearDraft(note.getString("note_id"));
      check(vault.readDraft() == null, "matching saved article can clear draft");

      PendingAttachment selecting = new PendingAttachment(note.getString("revision"), id, null, false);
      PendingAttachment restored = PendingAttachment.decode(selecting.encode());
      check(restored.revision.equals(note.getString("revision")) && restored.uri == null, "picker target survives recreation before result");
      PendingAttachment chosen = new PendingAttachment(note.getString("revision"), id, "content://synthetic-provider/document/42", true);
      restored = PendingAttachment.decode(chosen.encode());
      check(restored.uri.equals(chosen.uri) && restored.ownsGrant && restored.vaultId.equals(id), "selected URI and grant ownership survive recreation");
      check(!chosen.encode().contains(marker), "saved Activity state contains no note text");
      rejected = false;
      try { new PendingAttachment("invalid", id, null, false); } catch (IllegalArgumentException expected) { rejected = true; }
      check(rejected, "invalid attachment identity rejected");

      Files.write(new File(root, "navigation.enc").toPath(), LocalVault.utf8("damaged display state"));
      String inventory = vault.inventory().toString();
      TreeNavigation tree = new TreeNavigation(vault); tree.rows("", false);
      check(vault.recoveredNavigation(), "corrupt layout detected");
      check(vault.inventory().toString().equals(inventory), "layout recovery never changes notes");
      check(Arrays.stream(root.list()).anyMatch(n -> n.startsWith("navigation.invalid-")), "damaged ciphertext retained");

      File blob = vault.objectPath("blobs", image.getString("id")); byte[] bytes = Files.readAllBytes(blob.toPath());
      Files.delete(blob.toPath());
      try {
        vault.save(UUID.randomUUID().toString().replace("-", ""), Collections.emptyList(), new JSONObject().put("title", "unrelated note"));
        check(true, "saving an unrelated revision does not stat historical blobs");
        rejected = false;
        try { vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")), LocalVault.copy(note).put("content", "must validate referenced blob")); }
        catch (IllegalArgumentException expected) { rejected = true; }
        check(rejected, "new revision still requires all its own blobs");
        rejected = false;
        try { LocalVault.open(root, password); } catch (IllegalArgumentException expected) { rejected = true; }
        check(rejected, "opening still validates full persisted graph");
      } finally { Files.write(blob.toPath(), bytes); }
      System.out.println("Review regression checks passed: " + checks);
    } finally { vault.lock(); }
  }
}
