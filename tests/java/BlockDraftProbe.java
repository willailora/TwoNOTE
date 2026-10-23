import com.twonote.core.*;
import java.io.File;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import java.util.*;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.json.*;

/** Raw form checkpoint / process recreation / concurrent edit checks without Android UI. */
public final class BlockDraftProbe {
  static int checks;
  static void check(boolean condition, String message) { if (!condition) throw new AssertionError(message); checks++; }
  public static void main(String[] args) throws Exception {
    File root = new File(args[0]); String password = "synthetic-block-draft-password", id = UUID.randomUUID().toString().replace("-", "");
    byte[] key = new byte[32], salt = new byte[16]; Arrays.fill(key, (byte) 17); Arrays.fill(salt, (byte) 3);
    byte[] wrapping = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(new PBEKeySpec(password.toCharArray(), salt, LocalVault.ITERATIONS, 256)).getEncoded();
    JSONObject config = new JSONObject().put("version", 2).put("vault_id", id).put("iterations", LocalVault.ITERATIONS)
        .put("salt", Base64.getEncoder().encodeToString(salt)).put("wrapped_key", Base64.getEncoder().encodeToString(LocalVault.seal(wrapping, key, LocalVault.aad(id, "key"))));
    LocalVault vault = LocalVault.create(root, config, password);
    try {
      JSONObject base = vault.save(UUID.randomUUID().toString().replace("-", ""), Collections.emptyList(), new JSONObject().put("title", "Draft fixture").put("content", "before😀after"));
      String marker = "SYNTHETIC-UNFINISHED-TABLE";
      JSONObject draft = BlockDraft.create(base, null, 8, "bar", marker, "A,\"unfinished");
      vault.saveBlockDraft(draft);
      check(!new String(Files.readAllBytes(new File(root, "block-draft.enc").toPath()), StandardCharsets.UTF_8).contains(marker), "form checkpoint is ciphertext");
      vault.lock(); vault = LocalVault.open(root, password);
      JSONObject recovered = vault.readBlockDraft();
      check(recovered.getString("text").equals("A,\"unfinished"), "invalid partial CSV survives process recreation");
      check(recovered.getString("title").equals(marker) && recovered.getString("kind").equals("bar"), "heading and type survive");
      check(recovered.getInt("offset") == 8 && recovered.getString("revision").equals(base.getString("revision")), "target and UTF-16 insertion offset survive");
      vault.clearBlockDraft("f".repeat(32)); check(vault.readBlockDraft() != null, "unrelated action cannot discard a draft");
      boolean rejected = false;
      try { vault.saveBlockDraft(BlockDraft.create(base, null, 0, "table", "", "x")); } catch (IllegalStateException expected) { rejected = true; }
      check(rejected, "another form cannot silently replace pending input");
      draft.put("text", "A,１２\nB,-1"); vault.saveBlockDraft(draft);
      JSONObject remote = vault.save(base.getString("note_id"), Collections.singletonList(base.getString("revision")), LocalVault.copy(base).put("content", "remote edit"));
      JSONObject saved = vault.saveBlockEdit(draft);
      check(vault.heads(false).size() == 2, "remote edit survives local form save");
      check(saved.getJSONArray("parents").getString(0).equals(base.getString("revision")), "form uses original base, not latest mutable selection");
      JSONObject attachment = saved.getJSONArray("attachments").getJSONObject(0);
      check(saved.getString("content").equals(InlineBody.encode("before😀" + InlineBody.token(attachment.getString("id")) + "after")), "saved block stays at its original insertion point");
      check(NoteBlock.load(vault.readBlob(attachment.getString("id"))).getJSONArray("rows").getJSONArray(0).getString(1).equals("12"), "fullwidth numeric input normalized");
      check(vault.readBlockDraft() == null, "successful save clears transient draft");
      check(vault.saveBlockEdit(draft).getString("revision").equals(saved.getString("revision")), "save callback retry is idempotent");
      vault.saveBlockDraft(draft); vault.lock(); vault = LocalVault.open(root, password);
      check(vault.readBlockDraft() == null, "crash after commit before cleanup does not replay saved form");
      JSONObject other = BlockDraft.create(saved, attachment.getString("id"), 0, "line", "next", "A,2");
      vault.saveBlockDraft(other); vault.lock(); vault = LocalVault.open(root, password);
      check(vault.readBlockDraft().getString("target").equals(attachment.getString("id")), "existing target ID survives recreation");
      vault.clearBlockDraft(other.getString("draft_id")); check(vault.readBlockDraft() == null, "explicit discard removes only matching draft");
      rejected = false;
      try { vault.saveBlockDraft(BlockDraft.create(saved, "e".repeat(64), 0, "table", "", "a")); } catch (IllegalArgumentException expected) { rejected = true; }
      check(rejected, "invalid target cannot create a form checkpoint");
      check(vault.get(remote.getString("revision")).getString("content").equals("remote edit"), "remote content never overwritten");
      System.out.println("Block draft checks passed: " + checks);
    } finally { vault.lock(); }
  }
}
