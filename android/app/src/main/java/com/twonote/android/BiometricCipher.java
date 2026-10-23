package com.twonote.android;

import com.twonote.core.LocalVault;
import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Keep all Keystore updates, including AAD, after the authentication callback. */
final class BiometricCipher {
  static Cipher prepare(Cipher cipher, int mode, SecretKey key, byte[] iv) throws Exception {
    if (mode == Cipher.ENCRYPT_MODE) cipher.init(mode, key);
    else {
      if (iv == null || iv.length != 12) throw new IllegalArgumentException("保存済みの認証設定が不正です。再登録してください");
      cipher.init(mode, key, new GCMParameterSpec(128, iv));
    }
    // updateAAD can submit a Keystore operation and cache an authentication
    // error. Initializing a CryptoObject is the only pre-authentication step.
    return cipher;
  }

  static byte[] finish(Cipher authenticated, String id, byte[] input) throws Exception {
    if (authenticated == null) throw new IllegalStateException("認証後の暗号処理を取得できません");
    authenticated.updateAAD(LocalVault.aad(id, "biometric"));
    return authenticated.doFinal(input);
  }
}
