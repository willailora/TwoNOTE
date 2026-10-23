import com.twonote.core.CaptureFiles;
import com.twonote.core.LocalVault;
import com.twonote.core.PendingAttachment;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.UUID;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.json.JSONArray;
import org.json.JSONObject;

/** Synthetic camera-output/state/storage checks. No camera, Android UI, or real photos. */
public final class PhotoAttachmentProbe {
  private static int checks;
  private static void check(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
    checks++;
  }
  private interface Action { void run() throws Exception; }
  private static void rejects(Action action, String message) throws Exception {
    try { action.run(); } catch (IOException | IllegalArgumentException expected) { checks++; return; }
    throw new AssertionError(message);
  }

  public static void main(String[] args) throws Exception {
    File directory = new File(args[0], "camera-cache");
    File first = CaptureFiles.create(directory), second = CaptureFiles.create(directory);
    check(!first.equals(second) && first.length() == 0, "unique private output files");
    rejects(() -> CaptureFiles.validatePhoto(directory, first.getName()), "empty camera output must fail");
    rejects(() -> CaptureFiles.resolve(directory, "../vault-v2/config.json"), "no traversal");
    rejects(() -> CaptureFiles.resolve(directory, first.getAbsolutePath()), "no absolute paths");
    rejects(() -> CaptureFiles.resolve(directory, "capture-not-a-token.jpg"), "only issued token format");
    byte[] photo = LocalVault.utf8("PRIVATE-SYNTHETIC-CAMERA-PHOTO");
    Files.write(first.toPath(), photo);
    CaptureFiles.validatePhoto(directory, first.getName());
    check(first.length() == photo.length, "full sized file retained, not thumbnail intent");
    File unrelated = new File(directory, "unrelated.txt"); Files.write(unrelated.toPath(), new byte[] {1});
    check(CaptureFiles.abandoned(directory, first.getName()).equals(Collections.singletonList(second.getName())),
        "recreation keeps in-flight file and never cleans unrelated paths");
    for (String name : CaptureFiles.abandoned(directory, first.getName())) CaptureFiles.delete(directory, name);
    check(first.isFile() && !second.exists() && unrelated.isFile(), "targeted orphan cleanup");
    File fakeDirectory = new File(directory, "capture-" + "a".repeat(32) + ".jpg");
    if (!fakeDirectory.mkdir()) throw new IOException("fixture directory failed");
    rejects(() -> CaptureFiles.delete(directory, fakeDirectory.getName()), "never deletes directories");

    String id = UUID.randomUUID().toString().replace("-", ""), revision = "b".repeat(64);
    String uri = "content://com.twonote.android.capture/camera/" + first.getName();
    PendingAttachment waiting = new PendingAttachment(revision, id, null, false, first.getName());
    PendingAttachment restored = PendingAttachment.decode(waiting.encode());
    check(restored.uri == null && restored.captureName.equals(first.getName()), "waiting for camera result survives recreation");
    PendingAttachment ready = new PendingAttachment(revision, id, uri, false, first.getName());
    restored = PendingAttachment.decode(ready.encode());
    check(restored.uri.equals(uri) && restored.revision.equals(revision) && restored.vaultId.equals(id), "ready photo retains target while vault locked");
    check(!ready.encode().contains("PRIVATE-SYNTHETIC"), "Bundle contains references, not photo bytes");
    rejects(() -> new PendingAttachment(revision, id, uri, true, first.getName()), "our camera never takes a persistable gallery grant");
    rejects(() -> new PendingAttachment(revision, id, uri, false, "../photo.jpg"), "reject forged saved capture path");
    restored = PendingAttachment.decode(new JSONObject().put("version", 1).put("revision", revision).put("vault", id)
        .put("uri", "content://gallery/photo/1").put("ownsGrant", true).toString());
    check(restored.captureName == null && restored.ownsGrant, "old file selection state still supported");

    byte[] key = new byte[32], salt = new byte[16]; Arrays.fill(key, (byte) 21); Arrays.fill(salt, (byte) 8);
    String password = "synthetic-photo-password";
    byte[] wrapping = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        .generateSecret(new PBEKeySpec(password.toCharArray(), salt, LocalVault.ITERATIONS, 256)).getEncoded();
    JSONObject config = new JSONObject().put("version", 2).put("vault_id", id).put("iterations", LocalVault.ITERATIONS)
        .put("salt", Base64.getEncoder().encodeToString(salt))
        .put("wrapped_key", Base64.getEncoder().encodeToString(LocalVault.seal(wrapping, key, LocalVault.aad(id, "key"))));
    File root = new File(args[0], "vault");
    LocalVault vault = LocalVault.create(root, config, password);
    try {
      JSONObject attachment = vault.addBlob(first.getName(), "image/jpeg", Files.readAllBytes(first.toPath()));
      JSONObject note = vault.save(UUID.randomUUID().toString().replace("-", ""), Collections.emptyList(),
          new JSONObject().put("title", "Photo fixture").put("content", "unchanged text")
              .put("attachments", new JSONArray().put(attachment)));
      check(!Arrays.equals(Files.readAllBytes(vault.objectPath("blobs", attachment.getString("id")).toPath()), photo), "vault stores ciphertext");
      CaptureFiles.delete(directory, first.getName());
      CaptureFiles.delete(directory, first.getName());
      check(!first.exists(), "successful import/cancel cleanup is idempotent");
      vault.lock(); vault = LocalVault.open(root, password);
      JSONObject reread = vault.get(note.getString("revision"));
      check(reread.getString("content").equals("unchanged text") && reread.getJSONArray("attachments").length() == 1,
          "article content and photo survive restart");
      check(Arrays.equals(vault.readBlob(attachment.getString("id")), photo), "photo stays available offline after temp deletion");
      check(unrelated.isFile() && fakeDirectory.isDirectory(), "cleanup leaves other user/app data untouched");
    } finally { vault.lock(); }
    System.out.println("Photo attachment checks passed: " + checks);
  }
}
