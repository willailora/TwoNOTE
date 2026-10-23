package com.twonote.android;

import java.security.*;
import java.security.spec.AlgorithmParameterSpec;
import java.util.Arrays;
import javax.crypto.*;
import javax.crypto.spec.SecretKeySpec;

/** Crypto-order regression test without Android UI, Keystore or real secrets. */
public final class BiometricCipherProbe {
  private static int checks;
  private static void check(boolean condition, String message) {
    if (!condition) throw new AssertionError(message);
    checks++;
  }

  private static final class GatedSpi extends CipherSpi {
    final Cipher delegate;
    boolean authorized, cachedFailure;
    int aadCalls, finalCalls;
    GatedSpi() throws Exception { delegate = Cipher.getInstance("AES/GCM/NoPadding"); }
    protected void engineSetMode(String mode) {}
    protected void engineSetPadding(String padding) {}
    protected int engineGetBlockSize() { return delegate.getBlockSize(); }
    protected int engineGetOutputSize(int length) { return delegate.getOutputSize(length); }
    protected byte[] engineGetIV() { return delegate.getIV(); }
    protected AlgorithmParameters engineGetParameters() { return delegate.getParameters(); }
    protected void engineInit(int mode, Key key, SecureRandom random) throws InvalidKeyException {
      delegate.init(mode, key, random);
    }
    protected void engineInit(int mode, Key key, AlgorithmParameterSpec params, SecureRandom random)
        throws InvalidKeyException, InvalidAlgorithmParameterException { delegate.init(mode, key, params, random); }
    protected void engineInit(int mode, Key key, AlgorithmParameters params, SecureRandom random)
        throws InvalidKeyException, InvalidAlgorithmParameterException { delegate.init(mode, key, params, random); }
    protected void engineUpdateAAD(byte[] input, int offset, int length) {
      aadCalls++;
      if (!authorized) { cachedFailure = true; return; }
      delegate.updateAAD(input, offset, length);
    }
    protected byte[] engineUpdate(byte[] input, int offset, int length) {
      if (!authorized) { cachedFailure = true; return null; }
      return delegate.update(input, offset, length);
    }
    protected int engineUpdate(byte[] input, int offset, int length, byte[] output, int outOffset) throws ShortBufferException {
      byte[] result = engineUpdate(input, offset, length);
      if (result == null) return 0;
      if (outOffset + result.length > output.length) throw new ShortBufferException();
      System.arraycopy(result, 0, output, outOffset, result.length); return result.length;
    }
    protected byte[] engineDoFinal(byte[] input, int offset, int length) throws IllegalBlockSizeException, BadPaddingException {
      finalCalls++;
      if (!authorized || cachedFailure) throw new IllegalBlockSizeException("cached authentication failure");
      return delegate.doFinal(input, offset, length);
    }
    protected int engineDoFinal(byte[] input, int offset, int length, byte[] output, int outOffset)
        throws ShortBufferException, IllegalBlockSizeException, BadPaddingException {
      byte[] result = engineDoFinal(input, offset, length);
      if (outOffset + result.length > output.length) throw new ShortBufferException();
      System.arraycopy(result, 0, output, outOffset, result.length); return result.length;
    }
  }

  private static final class AuthCipher extends Cipher {
    final GatedSpi gate;
    AuthCipher() throws Exception { this(new GatedSpi()); }
    private AuthCipher(GatedSpi gate) {
      super(gate, new Provider("SyntheticAuthGate", "1", "No Android or hardware") {}, "AES/GCM/NoPadding");
      this.gate = gate;
    }
  }

  public static void main(String[] args) throws Exception {
    SecretKey key = new SecretKeySpec(new byte[32], "AES");
    String id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    byte[] vaultKey = new byte[32]; Arrays.fill(vaultKey, (byte) 37);
    AuthCipher encrypt = new AuthCipher();
    check(BiometricCipher.prepare(encrypt, Cipher.ENCRYPT_MODE, key, null) == encrypt, "keep CryptoObject identity");
    check(encrypt.gate.aadCalls == 0 && encrypt.gate.finalCalls == 0, "no pre-auth crypto updates");
    encrypt.gate.authorized = true;
    byte[] sealed = BiometricCipher.finish(encrypt, id, vaultKey);
    check(encrypt.gate.aadCalls == 1 && encrypt.gate.finalCalls == 1, "AAD and payload after authentication");
    check(sealed.length == 48 && encrypt.getIV().length == 12, "existing envelope dimensions");
    check(!Arrays.equals(sealed, vaultKey), "key is encrypted");
    AuthCipher decrypt = new AuthCipher();
    BiometricCipher.prepare(decrypt, Cipher.DECRYPT_MODE, key, encrypt.getIV());
    check(decrypt.gate.aadCalls == 0, "decrypt setup performs no AAD update");
    decrypt.gate.authorized = true;
    check(Arrays.equals(vaultKey, BiometricCipher.finish(decrypt, id, sealed)), "authenticated roundtrip");

    AuthCipher blocked = new AuthCipher();
    BiometricCipher.prepare(blocked, Cipher.ENCRYPT_MODE, key, null);
    boolean rejected = false;
    try { BiometricCipher.finish(blocked, id, vaultKey); } catch (IllegalBlockSizeException expected) { rejected = true; }
    check(rejected, "no encryption without authentication");

    AuthCipher legacyOrder = new AuthCipher();
    BiometricCipher.prepare(legacyOrder, Cipher.ENCRYPT_MODE, key, null);
    legacyOrder.updateAAD(com.twonote.core.LocalVault.aad(id, "biometric"));
    legacyOrder.gate.authorized = true;
    rejected = false;
    try { legacyOrder.doFinal(vaultKey); } catch (IllegalBlockSizeException expected) { rejected = true; }
    check(rejected, "old pre-auth AAD order reproduces delayed IllegalBlockSizeException");

    Cipher legacy = Cipher.getInstance("AES/GCM/NoPadding");
    legacy.init(Cipher.ENCRYPT_MODE, key);
    legacy.updateAAD(com.twonote.core.LocalVault.aad(id, "biometric"));
    byte[] previous = legacy.doFinal(vaultKey);
    AuthCipher compatible = new AuthCipher();
    BiometricCipher.prepare(compatible, Cipher.DECRYPT_MODE, key, legacy.getIV()); compatible.gate.authorized = true;
    check(Arrays.equals(vaultKey, BiometricCipher.finish(compatible, id, previous)), "old successful registrations stay compatible");

    AuthCipher otherVault = new AuthCipher();
    BiometricCipher.prepare(otherVault, Cipher.DECRYPT_MODE, key, encrypt.getIV()); otherVault.gate.authorized = true;
    rejected = false;
    try { BiometricCipher.finish(otherVault, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", sealed); }
    catch (AEADBadTagException expected) { rejected = true; }
    check(rejected, "vault binding remains enforced");
    rejected = false;
    try { BiometricCipher.prepare(new AuthCipher(), Cipher.DECRYPT_MODE, key, new byte[2]); }
    catch (IllegalArgumentException expected) { rejected = true; }
    check(rejected, "invalid saved IV is rejected");
    System.out.println("Biometric cipher sequence checks passed: " + checks);
  }
}
