package com.twonote.android;

import static org.junit.Assert.*;

import android.os.Looper;
import android.widget.EditText;
import com.twonote.core.LocalVault;
import java.io.File;
import java.time.Duration;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.UUID;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.Robolectric;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.android.controller.ActivityController;
import org.robolectric.annotation.Config;
import org.robolectric.util.ReflectionHelpers;
import org.robolectric.util.ReflectionHelpers.ClassParameter;

/** Synthetic in-process Android lifecycle tests; no user's phone or credentials. */
@RunWith(RobolectricTestRunner.class)
@Config(sdk = {28, 35})
public class MainActivityTest {
  private static final String PASSWORD = "synthetic-日本語-password";
  private ActivityController<MainActivity> controller;
  private MainActivity activity;
  private LocalVault vault;
  private File root;
  private JSONObject note;

  @Before
  public void setup() throws Exception {
    root = new File(RuntimeEnvironment.getApplication().getFilesDir(), "vault-v2");
    String id = UUID.randomUUID().toString().replace("-", "");
    byte[] salt = new byte[16], key = new byte[32];
    Arrays.fill(salt, (byte) 7);
    Arrays.fill(key, (byte) 19);
    byte[] wrapping =
        SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
            .generateSecret(
                new PBEKeySpec(PASSWORD.toCharArray(), salt, LocalVault.ITERATIONS, 256))
            .getEncoded();
    JSONObject config =
        new JSONObject()
            .put("version", 2)
            .put("vault_id", id)
            .put("iterations", LocalVault.ITERATIONS)
            .put("salt", Base64.getEncoder().encodeToString(salt))
            .put(
                "wrapped_key",
                Base64.getEncoder()
                    .encodeToString(LocalVault.seal(wrapping, key, LocalVault.aad(id, "key"))));
    vault = LocalVault.create(root, config, PASSWORD);
    note =
        vault.save(
            UUID.randomUUID().toString().replace("-", ""),
            Collections.emptyList(),
            new JSONObject().put("title", "合成テストノート").put("content", "初期本文"));
    controller = Robolectric.buildActivity(MainActivity.class).setup();
    activity = controller.get();
    ReflectionHelpers.setField(activity, "vault", vault);
    ReflectionHelpers.setField(activity, "current", note);
    ReflectionHelpers.callInstanceMethod(activity, "showEditor");
  }

  @After
  public void cleanup() {
    if (controller != null) controller.pause().stop().destroy();
    if (vault != null) vault.lock();
  }

  @Test
  public void japaneseEditAutosavesOffline() throws Exception {
    EditText editor = ReflectionHelpers.getField(activity, "content");
    editor.setText("日本語をオフラインで編集しました");
    Shadows.shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(800));
    assertFalse(ReflectionHelpers.<Boolean>getField(activity, "dirty"));
    LocalVault reopened = LocalVault.open(root, PASSWORD);
    assertEquals("日本語をオフラインで編集しました", reopened.heads(false).get(0).getString("content"));
    reopened.lock();
  }

  @Test
  public void backgroundSavesThenClearsViewsAndVaultKey() throws Exception {
    EditText editor = ReflectionHelpers.getField(activity, "content");
    editor.setText("バックグラウンド直前の編集");
    controller.pause().stop();
    assertNull(ReflectionHelpers.getField(activity, "vault"));
    assertNull(ReflectionHelpers.getField(activity, "current"));
    assertNull(ReflectionHelpers.getField(activity, "content"));
    assertEquals("", editor.getText().toString());
    try {
      vault.heads(false);
      fail("key was retained after locking");
    } catch (IllegalStateException expected) {
    }
    LocalVault reopened = LocalVault.open(root, PASSWORD);
    assertEquals("バックグラウンド直前の編集", reopened.heads(false).get(0).getString("content"));
    reopened.lock();
    controller.restart().start().resume().visible();
  }

  @Test
  public void simultaneousEditRetainsBothBranches() throws Exception {
    EditText editor = ReflectionHelpers.getField(activity, "content");
    editor.setText("スマホの未保存入力");
    vault.save(
        note.getString("note_id"),
        Collections.singletonList(note.getString("revision")),
        LocalVault.copy(note).put("content", "PC側の編集"));
    Shadows.shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(800));
    assertEquals(2, vault.heads(false).size());
  }

  @Test
  public void completedSyncRefreshesOnlyAnUneditedNote() throws Exception {
    vault.save(
        note.getString("note_id"),
        Collections.singletonList(note.getString("revision")),
        LocalVault.copy(note).put("content", "受信したPCの編集"));
    ReflectionHelpers.callInstanceMethod(
        activity, "syncFinished", ClassParameter.from(JSONObject.class, new JSONObject()));
    EditText editor = ReflectionHelpers.getField(activity, "content");
    assertEquals("受信したPCの編集", editor.getText().toString());
    editor.setText("まだ保存していない入力");
    ReflectionHelpers.callInstanceMethod(
        activity, "syncFinished", ClassParameter.from(JSONObject.class, new JSONObject()));
    assertEquals("まだ保存していない入力", editor.getText().toString());
  }
}
