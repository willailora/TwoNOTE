package com.twonote.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.security.KeyStore;
import java.util.Base64;
import java.util.Map;
import java.util.UUID;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;

/** Wraps the vault key with an authentication-bound Android Keystore key. */
final class BiometricVault {
  static final class Registration {
    final String id, keyAlias;
    final Cipher cipher;
    // Once publishing is attempted, retain this key even on an ambiguous disk
    // failure; either the previous or new preference file must remain readable.
    boolean retained;
    Registration(String id, String keyAlias, Cipher cipher) {
      this.id = id; this.keyAlias = keyAlias; this.cipher = cipher;
    }
  }

  private static String alias(String id) {
    return "twonote_v2_" + id;
  }

  private static String prefs(String id) {
    return "twonote_biometric_" + id;
  }

  static boolean saved(Context context, String id) {
    SharedPreferences stored = context.getSharedPreferences(prefs(id), 0);
    return stored.contains("cipher") && stored.contains("iv");
  }

  private static KeyStore store() throws Exception {
    KeyStore store = KeyStore.getInstance("AndroidKeyStore");
    store.load(null);
    return store;
  }

  private static String savedAlias(SharedPreferences stored, String id) {
    String name = stored.getString("alias", alias(id));
    if (name == null || !(name.equals(alias(id)) || name.matches(java.util.regex.Pattern.quote(alias(id)) + "_[0-9a-f]{32}")))
      throw new IllegalArgumentException("保存済みの認証設定が不正です。再登録してください");
    return name;
  }

  static Registration beginRegistration(String id) throws Exception {
    String candidate = alias(id) + "_" + UUID.randomUUID().toString().replace("-", "");
    try {
      KeyGenerator generator = KeyGenerator.getInstance("AES", "AndroidKeyStore");
      KeyGenParameterSpec.Builder builder = new KeyGenParameterSpec.Builder(candidate,
          KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
          .setKeySize(256).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
          .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setUserAuthenticationRequired(true);
      if (Build.VERSION.SDK_INT >= 30)
        builder.setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG | KeyProperties.AUTH_DEVICE_CREDENTIAL);
      else builder.setUserAuthenticationValidityDurationSeconds(-1);
      generator.init(builder.build());
      SecretKey key = generator.generateKey();
      Cipher cipher = BiometricCipher.prepare(Cipher.getInstance("AES/GCM/NoPadding"), Cipher.ENCRYPT_MODE, key, null);
      return new Registration(id, candidate, cipher);
    } catch (Exception e) {
      deleteUnused(candidate);
      throw e;
    }
  }

  static Cipher decrypt(Context context, String id) throws Exception {
    SharedPreferences stored = context.getSharedPreferences(prefs(id), 0);
    String name = savedAlias(stored, id);
    KeyStore store = store();
    if (!store.containsAlias(name)) throw new IllegalStateException("認証用の鍵がありません。パスワードで開いて再登録してください");
    SecretKey key = (SecretKey) store.getKey(name, null);
    if (key == null) throw new IllegalStateException("認証用の鍵を取得できません。再登録してください");
    byte[] iv = Base64.getDecoder().decode(stored.getString("iv", ""));
    // Never create a replacement key while trying to decrypt an old envelope.
    return BiometricCipher.prepare(Cipher.getInstance("AES/GCM/NoPadding"), Cipher.DECRYPT_MODE, key, iv);
  }

  static void save(Context context, Registration registration, Cipher cipher, byte[] vaultKey) throws Exception {
    if (registration == null || registration.cipher != cipher || vaultKey == null || vaultKey.length != 32)
      throw new IllegalStateException("認証の登録処理が中断されています。もう一度設定してください");
    byte[] encrypted = BiometricCipher.finish(cipher, registration.id, vaultKey);
    SharedPreferences stored = context.getSharedPreferences(prefs(registration.id), 0);
    Map<String, ?> previous = stored.getAll();
    String oldAlias = null;
    try { oldAlias = savedAlias(stored, registration.id); } catch (IllegalArgumentException ignored) {}
    registration.retained = true;
    if (!stored.edit().putString("alias", registration.keyAlias)
        .putString("iv", Base64.getEncoder().encodeToString(cipher.getIV()))
        .putString("cipher", Base64.getEncoder().encodeToString(encrypted))
        .commit()) {
      SharedPreferences.Editor restore = stored.edit().clear();
      for (Map.Entry<String, ?> item : previous.entrySet())
        if (item.getValue() instanceof String) restore.putString(item.getKey(), (String) item.getValue());
      restore.commit();
      throw new IllegalStateException("端末の認証設定を保存できません。以前の鍵とノートは保持しています");
    }
    if (oldAlias != null && !oldAlias.equals(registration.keyAlias)) deleteUnused(oldAlias);
  }

  static byte[] read(Context context, String id, Cipher cipher) throws Exception {
    byte[] encrypted = Base64.getDecoder().decode(context.getSharedPreferences(prefs(id), 0).getString("cipher", ""));
    if (encrypted.length != 48) throw new IllegalArgumentException("保存済みの認証設定が不正です。再登録してください");
    return BiometricCipher.finish(cipher, id, encrypted);
  }

  static void cancel(Registration registration) {
    if (registration != null && !registration.retained) deleteUnused(registration.keyAlias);
  }

  private static void deleteUnused(String name) {
    try { store().deleteEntry(name); } catch (Exception ignored) {
      // An orphan key cannot unlock anything without a published ciphertext.
    }
  }
}
