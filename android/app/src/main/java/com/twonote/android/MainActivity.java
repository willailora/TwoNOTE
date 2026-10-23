package com.twonote.android;

import android.content.Intent;
import android.content.ClipData;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.graphics.Typeface;
import android.media.ExifInterface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.OpenableColumns;
import android.provider.MediaStore;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.view.View;
import android.view.ViewGroup;
import android.view.DragEvent;
import android.view.WindowManager;
import android.widget.*;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.PickVisualMediaRequest;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.app.AppCompatDelegate;
import androidx.biometric.BiometricManager;
import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;
import com.twonote.core.LocalVault;
import com.twonote.core.SyncClient;
import com.twonote.core.TreeNavigation;
import com.twonote.core.TreeNavigation.Node;
import com.twonote.core.PendingAttachment;
import com.twonote.core.CaptureFiles;
import com.twonote.core.InlineBody;
import com.twonote.core.NoteBlock;
import com.twonote.core.BlockDraft;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.crypto.Cipher;
import org.json.JSONArray;
import org.json.JSONObject;

/** Native offline editor. Only explicit pairing and sync use the network. */
public class MainActivity extends AppCompatActivity {
  private static final int BG = 0xff0b1524,
      PANEL = 0xff14273d,
      INK = 0xffe6edf5,
      MUTED = 0xff9fb3c8,
      PICK_FILE = 4102,
      TAKE_PHOTO = 4103;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final ExecutorService worker = Executors.newSingleThreadExecutor();
  private final List<Bitmap> bitmaps = new ArrayList<>();
  private AlertDialog imageDialog;
  private LocalVault vault;
  private File vaultRoot;
  private JSONObject current;
  private LinearLayout screen;
  private EditText title, content, notebook, section, tags;
  private TextView status;
  private boolean dirty, loading, busy, foreground, prompting, biometricSave, showDeleted;
  private volatile int session;
  private BiometricPrompt biometric;
  private BiometricVault.Registration pendingRegistration;
  private int biometricEpoch = -1;
  private byte[] pendingKey, sealedDraft;
  private String biometricId, pendingAttachmentRevision;
  private Uri pendingAttachment;
  private String pendingAttachmentVaultId;
  private boolean pendingAttachmentGrantOwned;
  private String pendingCaptureName;
  private int pendingAttachmentOffset = -1;
  private final Map<String, Bitmap> inlinePreviews = new HashMap<>();
  private long inlinePreviewBytes;
  private ActivityResultLauncher<PickVisualMediaRequest> photoPicker;
  private ActivityResultLauncher<String> cameraPermission;
  private String deferredAttachmentErrorTitle;
  private Exception deferredAttachmentError;
  private JSONObject activeBlockDraft;
  private EditText blockHeading, blockData;
  private AlertDialog blockDialog;
  private int blockKind;
  private boolean blockDraftDirty;
  private byte[] sealedBlockDraft;
  private static final String[] BLOCK_KINDS = {"table", "bar", "line"};
  private final Runnable blockAutosave = () -> checkpointBlockDraft();
  private JSONObject cachedPair;
  private String cachedCode;
  private TreeNavigation navigation;
  private Runnable treeRefresh;
  private String selectedBook = "", selectedSection = "", treeQuery = "";
  private int textSize = 16, bodyZoom = 100;
  private String fontFamily = "sans-serif";
  private final Map<TextView, Float> fontRatios = new WeakHashMap<>();
  private final Set<AlertDialog> dialogs = new HashSet<>();
  private static final class DragPayload {
    final Node node; final int epoch; final MainActivity owner; boolean consumed;
    DragPayload(Node node, int epoch, MainActivity owner) { this.node = node; this.epoch = epoch; this.owner = owner; }
  }
  private final Runnable autosave = () -> flush();

  @Override
  public void onCreate(Bundle state) {
    AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_YES);
    super.onCreate(state);
    android.content.SharedPreferences display = getSharedPreferences("display", MODE_PRIVATE);
    textSize = Math.max(6, Math.min(48, display.getInt("textSize", 16)));
    bodyZoom = Math.max(50, Math.min(300, display.getInt("bodyZoom", 100)));
    fontFamily = display.getString("fontFamily", "sans-serif");
    if (!Arrays.asList("sans-serif", "serif", "monospace").contains(fontFamily)) fontFamily = "sans-serif";
    getWindow()
        .setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
    getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
    vaultRoot = new File(getFilesDir(), "vault-v2");
    if (state != null) {
      try {
        PendingAttachment pending = PendingAttachment.decode(state.getString("pendingAttachment"));
        if (pending != null) {
          if (pending.captureName != null && pending.uri != null
              && !captureUri(pending.captureName).toString().equals(pending.uri))
            throw new IOException("撮影結果の参照が一致しません");
          pendingAttachmentRevision = pending.revision; pendingAttachmentVaultId = pending.vaultId;
          pendingAttachment = pending.uri == null ? null : Uri.parse(pending.uri);
          pendingAttachmentGrantOwned = pending.ownsGrant;
          pendingCaptureName = pending.captureName;
          pendingAttachmentOffset = pending.offset;
        }
      } catch (Exception ignored) { /* Invalid state cannot select a different note. */ }
    }
    photoPicker = registerForActivityResult(new ActivityResultContracts.PickVisualMedia(), this::selectedAttachment);
    cameraPermission = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> {
      if (pendingCaptureName == null) return;
      if (granted) launchCamera();
      else { clearPendingAttachment(); notice("撮影を中止しました。写真を選ぶ機能は権限なしで使えます"); }
    });
    biometric =
        new BiometricPrompt(
            this,
            ContextCompat.getMainExecutor(this),
            new BiometricPrompt.AuthenticationCallback() {
              @Override
              public void onAuthenticationSucceeded(
                  @NonNull BiometricPrompt.AuthenticationResult result) {
                if (!prompting || biometricEpoch != session || isDestroyed() || isFinishing()) return;
                prompting = false;
                String phase = biometricSave ? "登録：認証後の鍵保存" : "解除：認証後の鍵復号";
                try {
                  BiometricPrompt.CryptoObject authenticated = result.getCryptoObject();
                  if (authenticated == null || authenticated.getCipher() == null)
                    throw new IllegalStateException("認証結果に暗号処理がありません。パスワードで開いて再登録してください");
                  Cipher cipher = authenticated.getCipher();
                  if (biometricSave) {
                    BiometricVault.save(MainActivity.this, pendingRegistration, cipher, pendingKey);
                    notice("次回から指紋・端末認証で開けます");
                  } else {
                    byte[] key = BiometricVault.read(MainActivity.this, biometricId, cipher);
                    try {
                      phase = "解除：保管庫を開く";
                      vault = LocalVault.openWithKey(vaultRoot, key);
                    } finally {
                      Arrays.fill(key, (byte) 0);
                    }
                    opened();
                  }
                } catch (Exception e) {
                  biometricError(phase, e);
                } finally {
                  clearPendingKey();
                  handler.postDelayed(
                      () -> {
                        if (!foreground) lockMemory();
                      },
                      500);
                }
              }

              @Override
              public void onAuthenticationError(int code, @NonNull CharSequence text) {
                if (!prompting || biometricEpoch != session) return;
                prompting = false;
                clearPendingKey();
                if (!foreground) lockMemory();
                else {
                  notice(text.toString());
                  if (code != BiometricPrompt.ERROR_CANCELED && code != BiometricPrompt.ERROR_USER_CANCELED
                      && code != BiometricPrompt.ERROR_NEGATIVE_BUTTON)
                    biometricError("端末認証の実行", new IllegalStateException("認証コード " + code + "：" + text));
                }
              }

              @Override public void onAuthenticationFailed() {
                if (prompting && biometricEpoch == session) notice("指紋が一致しません。もう一度触れてください");
              }
            });
    getOnBackPressedDispatcher()
        .addCallback(
            this,
            new OnBackPressedCallback(true) {
              @Override
              public void handleOnBackPressed() {
                if (busy) {
                  notice("処理中です");
                  return;
                }
                if (current != null) {
                  if (flush()) {
                    current = null;
                    showNotes();
                  }
                } else if (vault != null) {
                  lockMemory();
                  showUnlock();
                } else finish();
              }
            });
    showUnlock();
    try {
      for (String name : CaptureFiles.abandoned(captureDirectory(), pendingCaptureName)) deleteCapture(name);
    } catch (Exception e) { showAttachmentError("前回の撮影用一時ファイルを整理できません", e); }
  }

  private int dp(int value) {
    return Math.round(value * getResources().getDisplayMetrics().density);
  }

  private TextView label(String text, int size) {
    TextView v = new TextView(this);
    v.setText(text);
    v.setTextColor(INK);
    styleText(v, size > 20 ? size / 16f : 1f);
    v.setPadding(0, dp(6), 0, dp(6));
    return v;
  }

  private EditText input(String hint, boolean password) {
    EditText e = new EditText(this);
    e.setHint(hint);
    e.setTextColor(INK);
    e.setHintTextColor(MUTED);
    e.setSingleLine(true);
    e.setInputType(
        password
            ? InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
            : InputType.TYPE_CLASS_TEXT);
    styleText(e, 1f);
    return e;
  }

  private Button button(String text, Runnable action) {
    Button b = new Button(this);
    b.setText(text);
    b.setAllCaps(false);
    styleText(b, 1f);
    b.setOnClickListener(v -> action.run());
    return b;
  }

  private void releaseBitmaps() {
    inlinePreviews.clear(); inlinePreviewBytes = 0;
    for (Bitmap b : bitmaps) if (!b.isRecycled()) b.recycle();
    bitmaps.clear();
  }

  private void newScreen() {
    loading = true;
    treeRefresh = null;
    if (screen != null) screen.removeAllViews();
    releaseBitmaps();
    screen = new LinearLayout(this);
    screen.setOrientation(LinearLayout.VERTICAL);
    screen.setPadding(dp(16), dp(20), dp(16), dp(10));
    screen.setBackgroundColor(BG);
    setContentView(screen);
    ViewCompat.setOnApplyWindowInsetsListener(
        screen,
        (view, insets) -> {
          androidx.core.graphics.Insets bars =
              insets.getInsets(WindowInsetsCompat.Type.systemBars());
          view.setPadding(
              dp(16) + bars.left, dp(12) + bars.top, dp(16) + bars.right, dp(10) + bars.bottom);
          return insets;
        });
  }

  private LinearLayout row() {
    LinearLayout r = new LinearLayout(this);
    r.setOrientation(LinearLayout.HORIZONTAL);
    return r;
  }

  private void rowButton(LinearLayout r, String text, Runnable action) {
    Button control = button(text, action);
    control.setMinHeight(dp(48));
    r.addView(control, new LinearLayout.LayoutParams(-2, -2));
  }

  private void addActions(LinearLayout actions) {
    HorizontalScrollView scroll = new HorizontalScrollView(this);
    scroll.addView(actions); screen.addView(scroll);
  }

  private void styleText(TextView view, float ratio) {
    fontRatios.put(view, ratio);
    view.setTextSize(android.util.TypedValue.COMPLEX_UNIT_SP, textSize * ratio * (view == content ? bodyZoom / 100f : 1f));
    int style = view.getTypeface() == null ? Typeface.NORMAL : view.getTypeface().getStyle();
    view.setTypeface(Typeface.create(fontFamily, style));
  }

  private void applyFonts(View view) {
    if (view instanceof TextView) styleText((TextView) view, fontRatios.getOrDefault((TextView) view, 1f));
    if (view instanceof ViewGroup) {
      ViewGroup group = (ViewGroup) view;
      for (int i = 0; i < group.getChildCount(); i++) applyFonts(group.getChildAt(i));
    }
  }

  private final class StyledDialogBuilder extends AlertDialog.Builder {
    StyledDialogBuilder() { super(MainActivity.this); }
    @Override public AlertDialog show() {
      AlertDialog dialog = super.show();
      dialogs.add(dialog);
      dialog.setOnDismissListener(ignored -> dialogs.remove(dialog));
      applyFonts(dialog.getWindow().getDecorView());
      ListView list = dialog.getListView();
      if (list != null) list.setOnHierarchyChangeListener(new ViewGroup.OnHierarchyChangeListener() {
        public void onChildViewAdded(View parent, View child) { applyFonts(child); }
        public void onChildViewRemoved(View parent, View child) {}
      });
      return dialog;
    }
  }

  private void scrollScreen() {
    ViewGroup parent = (ViewGroup) screen.getParent();
    if (parent != null) parent.removeView(screen);
    ScrollView scroll = new ScrollView(this);
    scroll.setFillViewport(true); scroll.addView(screen); setContentView(scroll);
  }

  private void displaySettings() {
    LinearLayout form = new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL);
    form.setPadding(dp(20), dp(8), dp(20), dp(8));
    form.addView(label("画面全体の文字サイズ（6〜48sp）", 16));
    EditText size = input("文字サイズ", false); size.setInputType(InputType.TYPE_CLASS_NUMBER); size.setText(String.valueOf(textSize)); form.addView(size);
    form.addView(label("本文のみの表示倍率（50〜300%）", 16));
    EditText zoom = input("本文の倍率", false); zoom.setInputType(InputType.TYPE_CLASS_NUMBER); zoom.setText(String.valueOf(bodyZoom)); form.addView(zoom);
    String[] families = {"sans-serif", "serif", "monospace"};
    String[] names = {"標準", "明朝", "等幅"};
    int[] chosen = {Math.max(0, Arrays.asList(families).indexOf(fontFamily))};
    Button family = button("フォント：" + names[chosen[0]], () -> {});
    family.setOnClickListener(v -> new StyledDialogBuilder().setTitle("画面全体のフォント")
        .setItems(names, (d, which) -> { chosen[0] = which; family.setText("フォント：" + names[which]); }).show());
    form.addView(family);
    form.addView(label("ツリー・ボタン・設定画面にも反映します。端末の文字倍率にも対応します。本文や画像の再取得は行いません。", 14));
    ScrollView scroll = new ScrollView(this); scroll.addView(form);
    AlertDialog dialog = new StyledDialogBuilder().setTitle("表示・文字サイズ").setView(scroll)
        .setNegativeButton("キャンセル", null).setPositiveButton("保存", null).show();
    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
      try {
        int nextSize = Integer.parseInt(size.getText().toString()), nextZoom = Integer.parseInt(zoom.getText().toString());
        if (nextSize < 6 || nextSize > 48 || nextZoom < 50 || nextZoom > 300) throw new IllegalArgumentException("文字サイズは6〜48、倍率は50〜300で入力してください");
        if (!getSharedPreferences("display", MODE_PRIVATE).edit().putInt("textSize", nextSize)
            .putInt("bodyZoom", nextZoom).putString("fontFamily", families[chosen[0]]).commit()) throw new IOException("表示設定を保存できません");
        textSize = nextSize; bodyZoom = nextZoom; fontFamily = families[chosen[0]];
        boolean wasLoading = loading; loading = true;
        try {
          applyFonts(getWindow().getDecorView());
          for (AlertDialog opened : new ArrayList<>(dialogs)) applyFonts(opened.getWindow().getDecorView());
        } finally { loading = wasLoading; }
        dialog.dismiss();
      } catch (Exception e) { showError("設定できません", e); }
    });
  }

  private void notice(String text) {
    if (status != null) status.setText(text);
  }

  private void showError(String name, Exception e) {
    if (foreground)
      new StyledDialogBuilder()
          .setTitle(name)
          .setMessage(e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage())
          .setPositiveButton("OK", null)
          .show();
  }

  private boolean configured() {
    return new File(vaultRoot, "config.json").isFile();
  }

  private void complete(int epoch, Runnable action) {
    runOnUiThread(
        () -> {
          if (epoch != session) return;
          busy = false;
          action.run();
        });
  }

  private void showUnlock() {
    newScreen();
    current = null;
    dirty = false;
    screen.addView(label("2  TwoNote", 30));
    screen.addView(label("ノートはこの端末に暗号化して保存されます", 14));
    status = label("", 13);
    screen.addView(status);
    if (!configured()) {
      screen.addView(label("最初にPCのTwoNoteとペアリングしてください。\nその後は通信なしで閲覧・編集できます。", 16));
      screen.addView(button("QRを読み取る", this::scan));
      screen.addView(button("コードを貼り付ける", () -> showPairing("")));
    } else {
      EditText password = input("保管庫のパスワード", true);
      screen.addView(password);
      screen.addView(
          button(
              "パスワードで開く",
              () -> {
                if (busy) return;
                String value = password.getText().toString();
                password.setText("");
                busy = true;
                notice("保管庫を開いています…");
                int epoch = session;
                worker.execute(
                    () -> {
                      try {
                        LocalVault result = LocalVault.open(vaultRoot, value);
                        runOnUiThread(
                            () -> {
                              if (epoch != session) {
                                result.lock();
                                return;
                              }
                              busy = false;
                              vault = result;
                              opened();
                            });
                      } catch (Exception e) {
                        complete(epoch, () -> notice("開けません。パスワードを確認してください"));
                      }
                    });
              }));
      try {
        String id = LocalVault.readConfig(vaultRoot).getString("vault_id");
        if (BiometricVault.saved(this, id))
          screen.addView(button("指紋・端末認証で開く", () -> promptBiometric(false)));
      } catch (Exception ignored) {
      }
    }
    loading = false;
    scrollScreen();
  }

  private void scan() {
    if (!busy)
      new IntentIntegrator(this)
          .setDesiredBarcodeFormats(IntentIntegrator.QR_CODE)
          .setPrompt("PCのTwoNoteに表示されたQRコード")
          .setBeepEnabled(false)
          .setOrientationLocked(false)
          .initiateScan();
  }

  private void showPairing(String code) {
    newScreen();
    screen.addView(label("PCとペアリング", 25));
    screen.addView(label("PCで「スマホと同期」を開いてください。\nパスワードはPCで設定したものです。", 14));
    EditText codeInput = input("ペアリングコード", false);
    codeInput.setSingleLine(false);
    codeInput.setMaxLines(5);
    codeInput.setText(code);
    screen.addView(codeInput);
    EditText password = input("保管庫のパスワード", true);
    screen.addView(password);
    status = label("", 13);
    screen.addView(status);
    screen.addView(
        button(
            "接続して同期",
            () -> {
              if (busy) return;
              String pairing = codeInput.getText().toString().trim(),
                  secret = password.getText().toString();
              password.setText("");
              busy = true;
              notice("ペアリング中…");
              int epoch = session;
              worker.execute(
                  () -> {
                    LocalVault opened = null;
                    try {
                      JSONObject peer = SyncClient.parsePairing(pairing);
                      if (configured()
                          && !LocalVault.readConfig(vaultRoot)
                              .getString("vault_id")
                              .equals(peer.getString("vault_id")))
                        throw new IllegalArgumentException("別の保管庫です。既存のノートを上書きできません");
                      JSONObject paired;
                      if (pairing.equals(cachedCode) && cachedPair != null) paired = cachedPair;
                      else {
                        paired = SyncClient.pair(peer, UUID.randomUUID().toString());
                        cachedCode = pairing;
                        cachedPair = paired;
                      }
                      if (!peer.getString("vault_id")
                          .equals(paired.getJSONObject("config").getString("vault_id")))
                        throw new IllegalArgumentException("保管庫が一致しません");
                      opened =
                          configured()
                              ? LocalVault.open(vaultRoot, secret)
                              : LocalVault.create(
                                  vaultRoot, paired.getJSONObject("config"), secret);
                      peer.put("token", paired.getString("token"));
                      peer.remove("secret");
                      opened.setSettings(new JSONObject().put("peer", peer));
                      LocalVault result = opened;
                      SyncClient.exchange(
                          result,
                          peer,
                          true,
                          message -> {
                            if (epoch != session) throw new IllegalStateException("ロックされました");
                            runOnUiThread(
                                () -> {
                                  if (epoch == session) notice(message);
                                });
                          });
                      cachedPair = null;
                      cachedCode = null;
                      runOnUiThread(
                          () -> {
                            if (epoch != session) {
                              result.lock();
                              return;
                            }
                            busy = false;
                            vault = result;
                            opened();
                            new StyledDialogBuilder()
                                .setTitle("同期できました")
                                .setMessage("通信を切っても閲覧・編集できます。次回から指紋で開く設定にしますか？")
                                .setPositiveButton("設定する", (d, w) -> promptBiometric(true))
                                .setNegativeButton("後で", null)
                                .show();
                          });
                    } catch (Exception e) {
                      if (opened != null) opened.lock();
                      complete(epoch, () -> showError("接続できません", e));
                    }
                  });
            }));
    screen.addView(button("QRを読み取る", this::scan));
    screen.addView(button("戻る", this::showUnlock));
    loading = false;
    scrollScreen();
  }

  private void opened() {
    try {
      JSONObject recoveredDraft = null;
      if (sealedDraft != null) {
        byte[] key = vault.biometricKey();
        try {
          recoveredDraft =
              new JSONObject(
                  new String(
                      LocalVault.unseal(key, sealedDraft, LocalVault.aad(vault.vaultId(), "draft")),
                      StandardCharsets.UTF_8));
        } finally {
          Arrays.fill(key, (byte) 0);
        }
      } else recoveredDraft = vault.readDraft();
      if (recoveredDraft != null) {
        current = recoveredDraft;
        showEditor();
        dirty = true;
        sealedDraft = null;
        notice("未保存の下書きを復元しました。確認して保存してください");
        return;
      }
    } catch (Exception e) {
      showError("未保存データを復元できません。退避ファイルは保持しています", e);
    }
    try {
      JSONObject block = pendingBlockDraft();
      if (block != null) {
        resumeBlockDraft(block);
        notice("未保存の表・グラフを復元しました。確認して保存してください");
        return;
      }
    } catch (Exception e) { showAttachmentError("表・グラフの下書きを復元できません。退避データは保持しています", e); }
    if (pendingAttachment != null && pendingAttachmentRevision != null) {
      try {
        if (!vault.vaultId().equals(pendingAttachmentVaultId)) throw new IllegalStateException("添付先の保管庫が変わっています");
        current = vault.get(pendingAttachmentRevision);
        showEditor();
        Uri uri = pendingAttachment;
        importAttachment(uri);
        return;
      } catch (Exception e) {
        clearPendingAttachment();
        showError("添付先を復元できません。もう一度ファイルを選んでください", e);
      }
    }
    showNotes();
    if (vault.recoveredIndex()) notice("索引の破損を検出し、前回保存した索引から復元しました");
  }

  private void promptBiometric(boolean save) {
    if (prompting || busy) return;
    String phase = "認証方式の確認";
    try {
      int authenticators = BiometricManager.Authenticators.BIOMETRIC_STRONG;
      if (Build.VERSION.SDK_INT >= 30) authenticators |= BiometricManager.Authenticators.DEVICE_CREDENTIAL;
      int available = BiometricManager.from(this).canAuthenticate(authenticators);
      if (available != BiometricManager.BIOMETRIC_SUCCESS && available != BiometricManager.BIOMETRIC_STATUS_UNKNOWN) {
        String reason;
        switch (available) {
          case BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED:
            reason = "Androidの設定で指紋と画面ロック（PIN等）を登録してください"; break;
          case BiometricManager.BIOMETRIC_ERROR_HW_UNAVAILABLE:
            reason = "端末の認証機能が一時的に使用できません。少し待つか、端末の画面ロックを解除して再試行してください"; break;
          case BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE:
            reason = "この端末では対応する指紋・端末認証を利用できません"; break;
          case BiometricManager.BIOMETRIC_ERROR_SECURITY_UPDATE_REQUIRED:
            reason = "認証機能を利用するにはAndroidのセキュリティ更新が必要です"; break;
          default: reason = "このAndroid環境では指定した認証方式を利用できません";
        }
        throw new IllegalStateException(reason + "（確認コード " + available + "）");
      }
      clearPendingKey();
      biometricId = LocalVault.readConfig(vaultRoot).getString("vault_id");
      biometricSave = save;
      biometricEpoch = session;
      phase = save ? "登録：鍵の準備" : "解除：鍵の準備";
      Cipher cipher;
      if (save) {
        if (vault == null) throw new IllegalStateException("先にパスワードで保管庫を開いてください");
        pendingKey = vault.biometricKey();
        pendingRegistration = BiometricVault.beginRegistration(biometricId);
        cipher = pendingRegistration.cipher;
      } else cipher = BiometricVault.decrypt(this, biometricId);
      BiometricPrompt.PromptInfo.Builder builder =
          new BiometricPrompt.PromptInfo.Builder().setTitle(save ? "指紋・端末認証を登録" : "TwoNoteを開く")
              .setAllowedAuthenticators(authenticators);
      if (Build.VERSION.SDK_INT < 30) builder.setNegativeButtonText("キャンセル");
      phase = "認証画面を開く";
      prompting = true;
      biometric.authenticate(builder.build(), new BiometricPrompt.CryptoObject(cipher));
    } catch (Exception e) {
      prompting = false;
      clearPendingKey();
      biometricError(phase, e);
    }
  }

  private void biometricError(String phase, Exception error) {
    StringBuilder details = new StringBuilder("段階：").append(phase)
        .append("\nAndroid ").append(Build.VERSION.RELEASE).append(" / API ").append(Build.VERSION.SDK_INT)
        .append("\n端末：").append(Build.MANUFACTURER).append(" ").append(Build.MODEL);
    Set<Throwable> visited = Collections.newSetFromMap(new IdentityHashMap<Throwable, Boolean>());
    for (Throwable cause = error; cause != null && visited.size() < 5 && visited.add(cause); cause = cause.getCause()) {
      details.append("\n").append(cause.getClass().getSimpleName());
      String message = cause.getMessage();
      if (message != null && !message.isEmpty()) details.append(": ").append(message.substring(0, Math.min(400, message.length())));
    }
    details.append("\n\nノートは削除していません。パスワードで開いて「設定」から認証を再登録できます。");
    showError("指紋・端末認証を完了できません", new IllegalStateException(details.toString()));
  }

  private void clearPendingKey() {
    BiometricVault.cancel(pendingRegistration);
    pendingRegistration = null;
    biometricEpoch = -1;
    if (pendingKey != null) Arrays.fill(pendingKey, (byte) 0);
    pendingKey = null;
  }

  private void showNotes() {
    if (vault == null) {
      showUnlock();
      return;
    }
    newScreen();
    current = null;
    screen.addView(label("2  TwoNote", 27));
    LinearLayout actions = row();
    rowButton(actions, "新規", this::newNote);
    rowButton(actions, "同期", this::synchronize);
    rowButton(actions, "設定", this::settings);
    rowButton(actions, "分類＋", () -> createFolder(null));
    addActions(actions);
    EditText search = input("ノートを検索", false);
    screen.addView(search);
    search.setText(treeQuery);
    status = label("長押しドラッグで移動・並べ替え ／ ⋮ で操作", 12);
    screen.addView(status);
    ListView list = new ListView(this);
    list.setBackgroundColor(PANEL);
    screen.addView(list, new LinearLayout.LayoutParams(-1, 0, 1));
    final List<Node> displayed = new ArrayList<>();
    Runnable refresh =
        () -> {
          try {
            displayed.clear();
            int first = list.getFirstVisiblePosition();
            int top = list.getChildCount() == 0 ? 0 : list.getChildAt(0).getTop();
            treeQuery = search.getText().toString();
            displayed.addAll(navigation().rows(treeQuery, showDeleted));
            list.setAdapter(
                new ArrayAdapter<Node>(this, android.R.layout.simple_list_item_1, displayed) {
                  @Override
                  public View getView(int p, View convert, android.view.ViewGroup parent) {
                    return treeRow(displayed.get(p), list);
                  }
                });
            list.setSelectionFromTop(Math.min(first, Math.max(0, displayed.size() - 1)), top);
          } catch (Exception e) {
            showError("ノートを表示できません", e);
          }
        };
    search.addTextChangedListener(watcher(refresh));
    treeRefresh = refresh;
    refresh.run();
    loading = false;
  }

  private TreeNavigation navigation() throws Exception {
    if (vault == null) throw new IllegalStateException("保管庫はロックされています");
    if (navigation == null) {
      navigation = new TreeNavigation(vault);
      if (vault.recoveredNavigation()) notice("破損した表示設定を退避し、ツリーを再作成しました。本文・画像は保持しています");
    }
    return navigation;
  }

  private String nodeName(Node node) {
    if (node.kind.equals("book")) return node.book.isEmpty() ? "（未分類ノートブック）" : node.book;
    if (node.kind.equals("section")) return node.section.isEmpty() ? "（セクション未指定）" : node.section;
    return (node.note.optBoolean("deleted") ? "[削除] " : "")
        + (node.note.optInt("conflicts") > 1 ? "[競合] " : "") + node.note.optString("title")
        + (node.note.optInt("conflicts") > 1 ? " · " + node.note.optString("revision").substring(0, 6) : "");
  }

  private void refreshTree() { if (treeRefresh != null) treeRefresh.run(); }

  private void openTreeNode(Node node) {
    if (busy || vault == null) return;
    try {
      selectedBook = node.book; selectedSection = node.section;
      if (node.note == null) { navigation().toggle(node); refreshTree(); }
      else { current = vault.get(node.note.getString("revision")); showEditor(); }
    } catch (Exception e) { showError("開けません", e); }
  }

  private View treeRow(Node node, ListView list) {
    LinearLayout outer = new LinearLayout(this); outer.setOrientation(LinearLayout.VERTICAL);
    View above = new View(this), below = new View(this);
    outer.addView(above, new LinearLayout.LayoutParams(-1, dp(2)));
    LinearLayout row = row(); row.setGravity(android.view.Gravity.CENTER_VERTICAL);
    String prefix = "";
    try { if (node.note == null) prefix = navigation().collapsed(node) && treeQuery.isEmpty() ? "▶ " : "▼ "; }
    catch (Exception ignored) {}
    TextView caption = label(prefix + nodeName(node), 16);
    caption.setPadding(dp(12 + node.depth() * 14), dp(12), dp(4), dp(12));
    caption.setMinHeight(dp(48)); caption.setFocusable(true);
    caption.setOnClickListener(v -> openTreeNode(node));
    caption.setOnLongClickListener(v -> {
      if (busy || vault == null || node.blocked()) { notice("削除済み・競合中の記事は移動できません"); return true; }
      DragPayload payload = new DragPayload(node, session, this);
      boolean started = v.startDragAndDrop(ClipData.newPlainText("TwoNote内部移動", "node"), new View.DragShadowBuilder(v), payload, 0);
      if (started) notice("分類へドロップで移動／行の上半分・下半分で前後へ並べ替え");
      return started;
    });
    row.addView(caption, new LinearLayout.LayoutParams(0, -2, 1));
    Button menu = button("⋮", () -> treeMenu(node));
    menu.setContentDescription(nodeName(node) + "の操作"); menu.setMinWidth(dp(48));
    row.addView(menu, new LinearLayout.LayoutParams(-2, -2));
    outer.addView(row); outer.addView(below, new LinearLayout.LayoutParams(-1, dp(2)));
    outer.setOnDragListener((target, event) -> {
      if (!(event.getLocalState() instanceof DragPayload)) return false;
      DragPayload payload = (DragPayload) event.getLocalState();
      if (payload.owner != this || payload.epoch != session || vault == null) return false;
      boolean intoFolder = payload.node.note != null && node.note == null;
      boolean allowed = !busy && !node.blocked()
          && (!payload.node.kind.equals("section") || payload.node.book.equals(node.book));
      switch (event.getAction()) {
        case DragEvent.ACTION_DRAG_STARTED: return allowed;
        case DragEvent.ACTION_DRAG_ENTERED:
        case DragEvent.ACTION_DRAG_LOCATION:
          if (!allowed) return false;
          boolean after = event.getY() >= outer.getHeight() / 2f;
          outer.setBackgroundColor(intoFolder ? 0xff254b64 : PANEL);
          above.setBackgroundColor(!intoFolder && !after ? 0xff6dd3cf : android.graphics.Color.TRANSPARENT);
          below.setBackgroundColor(!intoFolder && after ? 0xff6dd3cf : android.graphics.Color.TRANSPARENT);
          float y = outer.getTop() + event.getY();
          if (y < dp(48)) list.smoothScrollBy(-dp(24), 100);
          else if (y > list.getHeight() - dp(48)) list.smoothScrollBy(dp(24), 100);
          return true;
        case DragEvent.ACTION_DROP:
          if (!allowed || payload.consumed) return false;
          payload.consumed = true;
          boolean insertAfter = event.getY() >= outer.getHeight() / 2f;
          // The native drag must unwind before replacing ListView rows.
          handler.post(() -> { if (payload.epoch == session && vault != null) moveNode(payload.node, node, insertAfter); });
          return true;
        case DragEvent.ACTION_DRAG_EXITED:
        case DragEvent.ACTION_DRAG_ENDED:
          outer.setBackgroundColor(PANEL);
          above.setBackgroundColor(android.graphics.Color.TRANSPARENT);
          below.setBackgroundColor(android.graphics.Color.TRANSPARENT);
          return true;
        default: return true;
      }
    });
    return outer;
  }

  private void moveNode(Node source, Node target, boolean after) {
    if (busy || vault == null || !flush()) return;
    try {
      JSONObject moved = navigation().move(source, target, after);
      if (moved != null) { selectedBook = moved.getString("notebook"); selectedSection = moved.getString("section"); }
      refreshTree(); notice("移動・表示順を保存しました");
    } catch (Exception e) { refreshTree(); showError("移動できません", e); }
  }

  private void treeMenu(Node node) {
    if (busy || vault == null) return;
    List<String> names = new ArrayList<>(); List<Runnable> actions = new ArrayList<>();
    selectedBook = node.book; selectedSection = node.section;
    if (node.note == null) {
      names.add("この場所に新しい記事"); actions.add(this::newNote);
      names.add("分類を追加"); actions.add(() -> createFolder(node));
      if (node.kind.equals("section")) { names.add("セクション名を変更"); actions.add(() -> renameSection(node)); }
    } else if (!node.blocked()) {
      names.add("別の分類へ移動"); actions.add(() -> chooseDestination(node));
    }
    if (!node.blocked()) for (int direction : new int[] {-1, 1}) {
      names.add(direction < 0 ? "上へ移動" : "下へ移動");
      actions.add(() -> {
        try { navigation().shift(node, direction); refreshTree(); }
        catch (Exception e) { showError("並べ替えできません", e); }
      });
    }
    if (node.note == null) {
      names.add("空の分類を削除");
      actions.add(() -> new StyledDialogBuilder().setMessage("記事がない分類だけを削除しますか？")
          .setNegativeButton("キャンセル", null).setPositiveButton("削除", (d, w) -> {
            try { navigation().removeEmpty(node); refreshTree(); }
            catch (Exception e) { showError("削除できません", e); }
          }).show());
    }
    if (names.isEmpty()) { notice("削除済み・競合中の記事は先に内容を確認してください"); return; }
    new StyledDialogBuilder().setTitle(nodeName(node)).setItems(names.toArray(new String[0]), (d, which) -> actions.get(which).run()).show();
  }

  private void chooseDestination(Node source) {
    try {
      List<Node> destinations = navigation().destinations(); List<String> names = new ArrayList<>();
      for (Node n : destinations) names.add((n.book.isEmpty() ? "（未分類ノートブック）" : n.book) + " / "
          + (n.kind.equals("book") || n.section.isEmpty() ? "（セクション未指定）" : n.section));
      new StyledDialogBuilder().setTitle("移動先").setItems(names.toArray(new String[0]),
          (d, which) -> moveNode(source, destinations.get(which), true)).setNegativeButton("キャンセル", null).show();
    } catch (Exception e) { showError("移動先を表示できません", e); }
  }

  private interface NamedAction { void run(String name) throws Exception; }
  private void askName(String caption, String initial, NamedAction action) {
    EditText name = input("名前", false); name.setText(initial); name.selectAll();
    LinearLayout form = new LinearLayout(this); form.setPadding(dp(18), 0, dp(18), 0); form.addView(name, new LinearLayout.LayoutParams(-1, -2));
    AlertDialog dialog = new StyledDialogBuilder().setTitle(caption).setView(form)
        .setNegativeButton("キャンセル", null).setPositiveButton("保存", null).show();
    int epoch = session;
    dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
      if (epoch != session || vault == null || busy) return;
      try { action.run(TreeNavigation.validName(name.getText().toString())); dialog.dismiss(); }
      catch (Exception e) { showError("保存できません", e); }
    });
  }

  private void createFolder(Node parent) {
    if (busy || vault == null) return;
    String book = parent == null ? selectedBook : parent.book;
    new StyledDialogBuilder().setTitle("分類を追加").setItems(new String[] {"ノートブック", "セクション"}, (d, which) -> {
      askName(which == 0 ? "新しいノートブック" : "新しいセクション", "", name -> {
        navigation().addFolder(which == 0 ? name : book, which == 0 ? null : name);
        selectedBook = which == 0 ? name : book; selectedSection = which == 0 ? "" : name;
        refreshTree();
      });
    }).show();
  }

  private void renameSection(Node node) {
    askName("セクション名を変更", node.section, name -> {
      if (!flush()) throw new IOException("編集中の記事を保存できません");
      Map<String, JSONObject> updates = navigation().renameSection(node.book, node.section, name);
      if (current != null && updates.containsKey(current.optString("revision"))) {
        current = updates.get(current.optString("revision"));
        boolean wasLoading = loading; loading = true;
        try { section.setText(current.optString("section")); } finally { loading = wasLoading; }
      }
      selectedBook = node.book; selectedSection = name; refreshTree(); notice("セクション名を変更しました");
    });
  }

  private TextWatcher watcher(Runnable runnable) {
    return new TextWatcher() {
      public void beforeTextChanged(CharSequence s, int start, int count, int after) {}

      public void onTextChanged(CharSequence s, int start, int before, int count) {
        if (!loading) runnable.run();
      }

      public void afterTextChanged(Editable e) {}
    };
  }

  private void newNote() {
    if (busy) return;
    try {
      if (!flush()) return;
      current =
          vault.save(
              UUID.randomUUID().toString().replace("-", ""),
              Collections.emptyList(),
              new JSONObject().put("title", "無題").put("notebook", selectedBook).put("section", selectedSection));
      showEditor();
      title.selectAll();
      title.requestFocus();
    } catch (Exception e) {
      showError("作成できません", e);
    }
  }

  private void showEditor() {
    newScreen();
    LinearLayout actions = row();
    rowButton(
        actions,
        "戻る",
        () -> {
          if (!busy && flush()) {
            current = null;
            showNotes();
          }
        });
    rowButton(actions, "保存", () -> flush());
    rowButton(actions, "添付", this::attachmentMenu);
    rowButton(actions, "その他", this::noteMenu);
    rowButton(actions, "設定", this::settings);
    addActions(actions);
    status = label("保存済み", 12);
    screen.addView(status);
    ScrollView scroll = new ScrollView(this);
    LinearLayout editor = new LinearLayout(this);
    editor.setOrientation(LinearLayout.VERTICAL);
    scroll.addView(editor);
    screen.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    title = input("タイトル", false);
    styleText(title, 1.5f);
    title.setText(current.optString("title"));
    editor.addView(title);
    notebook = input("ノートブック", false);
    notebook.setText(current.optString("notebook"));
    editor.addView(notebook);
    section = input("セクション", false);
    section.setText(current.optString("section"));
    editor.addView(section);
    tags = input("タグ", false);
    tags.setText(current.optString("tags"));
    editor.addView(tags);
    content = new InlineEditor(this);
    content.setTextColor(INK);
    content.setHintTextColor(MUTED);
    content.setHint("ここに書き始める…");
    styleText(content, 1f);
    content.setGravity(android.view.Gravity.TOP);
    content.setInputType(
        InputType.TYPE_CLASS_TEXT
            | InputType.TYPE_TEXT_FLAG_MULTI_LINE
            | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
    content.setMinLines(10);
    try {
      ((InlineEditor) content).load(current.optString("content"), current.getJSONArray("attachments"), this::inlinePreview,
          Math.max(dp(120), getResources().getDisplayMetrics().widthPixels - dp(64)));
    } catch (Exception e) { content.setText(current.optString("content")); showError("本文内の画像を読み取れません", e); }
    editor.addView(content, new LinearLayout.LayoutParams(-1, -2));
    for (EditText e : new EditText[] {title, notebook, section, tags, content})
      e.addTextChangedListener(
          watcher(
              () -> {
                dirty = true;
                notice("未保存");
                handler.removeCallbacks(autosave);
                handler.postDelayed(autosave, 700);
              }));
    editor.addView(label("添付の管理（本文中の画像・表・グラフ）", 18));
    try {
      JSONArray files = current.getJSONArray("attachments");
      for (int i = 0; i < files.length(); i++) {
        JSONObject a = files.getJSONObject(i);
        String id = a.getString("id");
        editor.addView(label(a.getString("filename"), 13));
        if (InlineBody.supported(a)) {
          editor.addView(button("大きく開く", () -> openImage(id)));
          editor.addView(button("カーソル位置に挿入", () -> {
            if (busy) return;
            try { ((InlineEditor) content).insertAttachment(a); dirty = true; flush(); content.requestFocus(); }
            catch (Exception e) { showError("本文に挿入できません", e); }
          }));
          if (a.getString("mime").equals(InlineBody.BLOCK_MIME)) editor.addView(button("表・グラフのデータを編集", () -> editBlock("table", a)));
        }
        editor.addView(button("この添付を外す", () -> removeAttachment(id)));
      }
    } catch (Exception e) {
      showError("添付を表示できません", e);
    }
    dirty = false;
    loading = false;
  }

  private JSONObject editorValues() throws Exception {
    return LocalVault.copy(current)
        .put("title", title.getText().toString())
        .put("notebook", notebook.getText().toString())
        .put("section", section.getText().toString())
        .put("tags", tags.getText().toString())
        .put("content", ((InlineEditor) content).savedContent())
        .put("deleted", false);
  }

  private void openImage(String id) {
    if (vault == null) return;
    try {
      byte[] raw = vault.readBlob(id);
      Bitmap bitmap = blockAttachment(id) ? BlockPreview.render(NoteBlock.load(raw)) : decodePhoto(raw, 2000);
      if (bitmap == null) throw new IOException("画像形式を読み取れません");
      if (imageDialog != null) imageDialog.dismiss();
      ImageView picture = new ImageView(this);
      picture.setAdjustViewBounds(true);
      picture.setImageBitmap(bitmap);
      ScrollView scroll = new ScrollView(this);
      scroll.addView(picture);
      imageDialog =
          new AlertDialog.Builder(this).setView(scroll).setPositiveButton("閉じる", null).create();
      imageDialog.setOnDismissListener(
          d -> {
            picture.setImageDrawable(null);
            bitmap.recycle();
            imageDialog = null;
          });
      imageDialog.show();
      applyFonts(imageDialog.getWindow().getDecorView());
    } catch (Exception e) {
      showError("画像を開けません", e);
    } catch (OutOfMemoryError e) {
      notice("メモリが足りません。他のアプリを閉じて再試行してください");
    }
  }

  /** Decode bounded previews and respect phone-camera EXIF orientation; keep original bytes. */
  private Bitmap decodePhoto(byte[] raw, int maxDimension) {
    BitmapFactory.Options bounds = new BitmapFactory.Options();
    bounds.inJustDecodeBounds = true;
    BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null;
    BitmapFactory.Options options = new BitmapFactory.Options();
    options.inSampleSize = 1;
    while (bounds.outWidth / options.inSampleSize > maxDimension
        || bounds.outHeight / options.inSampleSize > maxDimension) options.inSampleSize *= 2;
    Bitmap bitmap = BitmapFactory.decodeByteArray(raw, 0, raw.length, options);
    if (bitmap == null) return null;
    Matrix matrix = new Matrix();
    try {
      int orientation = new ExifInterface(new ByteArrayInputStream(raw))
          .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
      switch (orientation) {
        case ExifInterface.ORIENTATION_FLIP_HORIZONTAL: matrix.setScale(-1, 1); break;
        case ExifInterface.ORIENTATION_ROTATE_180: matrix.setRotate(180); break;
        case ExifInterface.ORIENTATION_FLIP_VERTICAL: matrix.setScale(1, -1); break;
        case ExifInterface.ORIENTATION_TRANSPOSE: matrix.setRotate(90); matrix.postScale(-1, 1); break;
        case ExifInterface.ORIENTATION_ROTATE_90: matrix.setRotate(90); break;
        case ExifInterface.ORIENTATION_TRANSVERSE: matrix.setRotate(270); matrix.postScale(-1, 1); break;
        case ExifInterface.ORIENTATION_ROTATE_270: matrix.setRotate(270); break;
        default: break;
      }
    } catch (IOException ignored) { /* Formats without EXIF still render normally. */ }
    if (matrix.isIdentity()) return bitmap;
    try {
      Bitmap oriented = Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), matrix, true);
      if (oriented != bitmap) bitmap.recycle();
      return oriented;
    } catch (RuntimeException | OutOfMemoryError e) { bitmap.recycle(); throw e; }
  }

  private boolean flush() {
    handler.removeCallbacks(autosave);
    if (vault == null || current == null || !dirty) return true;
    try {
      current =
          vault.save(
              current.getString("note_id"),
              Collections.singletonList(current.getString("revision")),
              editorValues());
      dirty = false;
      notice("保存済み");
      sealedDraft = null;
      try { vault.clearDraft(current.getString("note_id")); }
      catch (Exception cleanup) { notice("ノートは保存済みです。下書きの整理は次回に再試行します"); }
      return true;
    } catch (Exception e) {
      try {
        vault.saveDraft(editorValues());
        notice("通常保存できません。編集内容は暗号化した下書きに退避しました");
      } catch (Exception draftError) {
        notice("保存・下書き退避とも失敗しました。空き容量を確保し、アプリを閉じずに再保存してください");
        e.addSuppressed(draftError);
      }
      showError("保存できません", e);
      return false;
    }
  }

  private void noteMenu() {
    if (busy || !flush()) return;
    new StyledDialogBuilder()
        .setItems(
            new String[] {current.optBoolean("deleted") ? "この編集版を復元" : "この編集版を削除（復元可能）", "競合する本文・添付を統合", "今すぐ同期", "この版を採用（他の版は履歴へ）"},
            (dialog, index) -> {
              if (index == 2) {
                synchronize();
                return;
              }
              if (index == 0) changeDeletion();
              if (index == 1) resolve();
              if (index == 3) resolve(false);
            })
        .show();
  }

  private void changeDeletion() {
    if (vault == null || current == null || busy || !flush()) return;
    String selected = current.optString("revision");
    boolean deleted = !current.optBoolean("deleted");
    new StyledDialogBuilder()
        .setMessage(deleted ? "選択中の編集版だけを削除しますか？他の版は変更しません。削除した版は削除済み表示から復元できます。" : "選択中の編集版を復元しますか？")
        .setNegativeButton("キャンセル", null)
        .setPositiveButton(deleted ? "削除" : "復元", (d, w) -> {
          try {
            vault.setDeleted(selected, deleted);
            current = null; showNotes();
          } catch (Exception e) { showError(deleted ? "削除できません" : "復元できません", e); }
        }).show();
  }

  private void resolve() {
    resolve(true);
  }

  private void resolve(boolean merge) {
    try {
      List<JSONObject> heads = new ArrayList<>();
      for (JSONObject n : vault.heads(true))
        if (n.getString("note_id").equals(current.getString("note_id"))) heads.add(n);
      if (heads.size() < 2) {
        notice("競合する編集版はありません");
        return;
      }
      if (current.getBoolean("deleted")) {
        notice("残す編集版を選んでください。削除済みの版が必要な場合は先に復元してください");
        return;
      }
      String selected = current.getString("revision");
      List<String> expected = new ArrayList<>();
      for (JSONObject n : heads) expected.add(n.getString("revision"));
      new StyledDialogBuilder()
          .setMessage(merge ? "削除されていない版の本文と添付をまとめます。削除済みの内容は復活させません。" : "表示中の本文・画像・表・グラフだけを残します。他の版は履歴に残り、一覧から消えます。")
          .setNegativeButton("キャンセル", null)
          .setPositiveButton(
              merge ? "統合" : "この版を採用",
              (d, w) -> {
                try {
                  current = vault.resolveVersions(selected, expected, merge);
                  showEditor();
                } catch (Exception e) {
                  showError("編集版を確定できません", e);
                }
              })
          .show();
    } catch (Exception e) {
      showError("統合できません", e);
    }
  }

  private void removeAttachment(String id) {
    if (busy || !flush()) return;
    try {
      JSONArray remaining = new JSONArray(), files = current.getJSONArray("attachments");
      for (int i = 0; i < files.length(); i++)
        if (!files.getJSONObject(i).getString("id").equals(id))
          remaining.put(files.getJSONObject(i));
      current =
          vault.save(
              current.getString("note_id"),
              Collections.singletonList(current.getString("revision")),
              LocalVault.copy(current).put("content", InlineBody.encode(InlineBody.body(current.getString("content"), files).replace(InlineBody.token(id), ""))).put("attachments", remaining));
      showEditor();
    } catch (Exception e) {
      showError("外せません", e);
    }
  }

  private boolean blockAttachment(String id) throws Exception {
    JSONArray files = current.getJSONArray("attachments");
    for (int i = 0; i < files.length(); i++) if (files.getJSONObject(i).getString("id").equals(id))
      return files.getJSONObject(i).getString("mime").equals(InlineBody.BLOCK_MIME);
    return false;
  }

  private Bitmap inlinePreview(String id) {
    if (inlinePreviews.containsKey(id)) return inlinePreviews.get(id);
    Bitmap bitmap = null;
    try {
      if (inlinePreviewBytes < 24L * 1024 * 1024) {
        byte[] raw = vault.readBlob(id);
        bitmap = blockAttachment(id) ? BlockPreview.render(NoteBlock.load(raw)) : decodePhoto(raw, 1000);
      }
    } catch (Exception | OutOfMemoryError ignored) {}
    if (bitmap == null) {
      bitmap = Bitmap.createBitmap(400, 48, Bitmap.Config.ARGB_8888);
      android.graphics.Canvas canvas = new android.graphics.Canvas(bitmap); canvas.drawColor(PANEL);
      android.graphics.Paint paint = new android.graphics.Paint(); paint.setColor(INK); paint.setTextSize(22);
      canvas.drawText("画像・表（添付の管理から開けます）", 4, 32, paint);
    }
    inlinePreviewBytes += bitmap.getAllocationByteCount(); bitmaps.add(bitmap); inlinePreviews.put(id, bitmap);
    return bitmap;
  }

  private void editBlock(String kind, JSONObject item) {
    if (busy || vault == null || current == null || !flush()) return;
    try {
      JSONObject pending = pendingBlockDraft();
      if (pending != null) {
        new StyledDialogBuilder().setTitle("未保存の表・グラフがあります")
            .setPositiveButton("再開", (d, w) -> { try { resumeBlockDraft(pending); } catch (Exception e) { showError("再開できません", e); } })
            .setNeutralButton("破棄して新規", (d, w) -> {
              try { vault.clearBlockDraft(pending.getString("draft_id")); sealedBlockDraft = null; editBlock(kind, item); }
              catch (Exception e) { showError("下書きを破棄できません", e); }
            }).setNegativeButton("戻る", null).show();
        return;
      }
      JSONObject value = item == null ? NoteBlock.parse(kind, "", kind.equals("table") ? "項目,内容\n例,値" : "A,10\nB,20\nC,15") : NoteBlock.load(vault.readBlob(item.getString("id")));
      showBlockEditor(BlockDraft.create(current, item == null ? null : item.getString("id"),
          ((InlineEditor) content).insertionOffset(), value.getString("kind"), value.getString("title"), NoteBlock.csv(value)));
    } catch (Exception e) { showError("表・グラフを開けません", e); }
  }

  private JSONObject pendingBlockDraft() throws Exception {
    if (sealedBlockDraft == null) return vault.readBlockDraft();
    byte[] key = vault.biometricKey();
    try {
      JSONObject draft = BlockDraft.validate(new JSONObject(new String(LocalVault.unseal(key, sealedBlockDraft,
          LocalVault.aad(vault.vaultId(), "block-draft")), StandardCharsets.UTF_8)));
      // Prefer the most recent RAM checkpoint if a disk write had failed.
      try { vault.saveBlockDraft(draft); sealedBlockDraft = null; return vault.readBlockDraft(); }
      catch (IOException e) {
        showAttachmentError("表の下書きはメモリから復元しました。端末への退避は空き容量を確保して再試行してください", e);
        return draft;
      }
    } finally { Arrays.fill(key, (byte) 0); }
  }

  private void resumeBlockDraft(JSONObject draft) throws Exception {
    current = vault.get(draft.getString("revision"));
    showEditor(); showBlockEditor(draft);
  }

  private void markBlockDraftDirty() {
    if (activeBlockDraft == null) return;
    blockDraftDirty = true;
    handler.removeCallbacks(blockAutosave); handler.postDelayed(blockAutosave, 700);
  }

  private JSONObject blockFormValues() throws Exception {
    return LocalVault.copy(activeBlockDraft).put("kind", BLOCK_KINDS[blockKind])
        .put("title", blockHeading.getText().toString()).put("text", blockData.getText().toString());
  }

  private void checkpointBlockDraft() {
    handler.removeCallbacks(blockAutosave);
    if (vault == null || activeBlockDraft == null || !blockDraftDirty) return;
    try {
      JSONObject state = blockFormValues();
      vault.saveBlockDraft(state); activeBlockDraft = state; blockDraftDirty = false; sealedBlockDraft = null;
    } catch (Exception error) {
      try {
        byte[] key = vault.biometricKey();
        try { sealedBlockDraft = LocalVault.seal(key, LocalVault.utf8(blockFormValues().toString()), LocalVault.aad(vault.vaultId(), "block-draft")); }
        finally { Arrays.fill(key, (byte) 0); }
      } catch (Exception fallback) { error.addSuppressed(fallback); }
      showAttachmentError("表・グラフの下書きを端末へ保存できません。アプリを終了せず空き容量を確認してください", error);
    }
  }

  private void clearBlockForm() {
    handler.removeCallbacks(blockAutosave); activeBlockDraft = null; blockDraftDirty = false;
    if (blockHeading != null) blockHeading.setText("");
    if (blockData != null) blockData.setText("");
    blockHeading = null; blockData = null; blockDialog = null;
  }

  private void showBlockEditor(JSONObject draft) throws Exception {
      activeBlockDraft = LocalVault.copy(BlockDraft.validate(draft)); blockDraftDirty = true;
      LinearLayout form = new LinearLayout(this); form.setOrientation(LinearLayout.VERTICAL); form.setPadding(dp(16), dp(8), dp(16), dp(8));
      form.addView(label("カンマ区切り、またはタブ区切りで貼り付け。表は50行×12列、グラフは項目名,数値の30行まで（見出し行不要）。", 14));
      blockHeading = input("見出し（任意）", false); blockHeading.setText(draft.getString("title")); form.addView(blockHeading);
      String[] names = {"表", "棒グラフ", "折れ線グラフ"}; blockKind = Arrays.asList(BLOCK_KINDS).indexOf(draft.getString("kind"));
      Button type = button(names[blockKind], () -> {});
      type.setOnClickListener(v -> new StyledDialogBuilder().setItems(names, (d, which) -> { blockKind = which; type.setText(names[which]); markBlockDraftDirty(); }).show()); form.addView(type);
      blockData = new EditText(this); blockData.setTextColor(INK); styleText(blockData, 1f); blockData.setMinLines(5);
      blockData.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE); blockData.setText(draft.getString("text")); form.addView(blockData);
      blockHeading.addTextChangedListener(watcher(this::markBlockDraftDirty)); blockData.addTextChangedListener(watcher(this::markBlockDraftDirty));
      ScrollView scroll = new ScrollView(this); scroll.addView(form);
      AlertDialog dialog = new StyledDialogBuilder().setTitle("表・グラフのデータ").setView(scroll).setNegativeButton("下書きを破棄", null).setPositiveButton("保存", null).show();
      blockDialog = dialog;
      dialog.setOnDismissListener(d -> {
        dialogs.remove(dialog);
        if (blockDialog == dialog) { checkpointBlockDraft(); clearBlockForm(); }
      });
      dialog.getButton(AlertDialog.BUTTON_NEGATIVE).setOnClickListener(v -> {
        try {
          vault.clearBlockDraft(activeBlockDraft.getString("draft_id")); sealedBlockDraft = null;
          clearBlockForm(); dialog.dismiss();
        } catch (Exception e) { showError("下書きを破棄できません", e); }
      });
      checkpointBlockDraft();
      dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
        if (vault == null || activeBlockDraft == null || busy) return;
        try {
          current = vault.saveBlockEdit(blockFormValues()); sealedBlockDraft = null; clearBlockForm();
          dialog.dismiss(); showEditor();
        } catch (Exception e) { checkpointBlockDraft(); showError("表・グラフを保存できません", e); }
      });
  }

  private void attachmentMenu() {
    if (busy || current == null || vault == null) return;
    if (pendingAttachment != null) {
      new StyledDialogBuilder().setTitle("取り込み待ちの添付があります")
          .setItems(new String[] {"取り込みを再試行", "取り消して別の画像・ファイルを選ぶ"}, (d, which) -> {
            if (!flush()) return;
            if (which == 0) opened();
            else if (clearPendingAttachment()) attachmentMenu();
          }).setNegativeButton("戻る", null).show();
      return;
    }
    new StyledDialogBuilder().setTitle("画像・ファイルを追加")
        .setItems(new String[] {"写真を選ぶ", "カメラで撮影", "ファイルを選ぶ", "表を挿入", "棒グラフを挿入", "折れ線グラフを挿入"}, (d, which) -> {
          if (which == 0) pickPhoto();
          else if (which == 1) takePhoto();
          else if (which == 2) pickAttachment();
          else editBlock(which == 3 ? "table" : which == 4 ? "bar" : "line", null);
        }).setNegativeButton("キャンセル", null).show();
  }

  private void showAttachmentError(String message, Exception error) {
    // Camera/picker results and startup cleanup may run before onResume().
    if (foreground) showError(message, error);
    else { deferredAttachmentErrorTitle = message; deferredAttachmentError = error; }
  }

  private boolean beginAttachment() {
    if (busy || current == null || vault == null || !flush() || !clearPendingAttachment()) return false;
    pendingAttachmentRevision = current.optString("revision");
    pendingAttachmentVaultId = vault.vaultId();
    pendingAttachmentOffset = ((InlineEditor) content).insertionOffset();
    return true;
  }

  private void pickPhoto() {
    if (!beginAttachment()) return;
    try {
      // AndroidX uses the system photo picker, with OPEN_DOCUMENT fallback on older phones.
      photoPicker.launch(new PickVisualMediaRequest.Builder()
          .setMediaType(ActivityResultContracts.PickVisualMedia.ImageOnly.INSTANCE).build());
    } catch (RuntimeException e) { clearPendingAttachment(); showAttachmentError("写真の選択を開始できません", e); }
  }

  private void pickAttachment() {
    if (!beginAttachment()) return;
    try { startActivityForResult(
        new Intent(Intent.ACTION_OPEN_DOCUMENT)
            .setType("*/*")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
            .addCategory(Intent.CATEGORY_OPENABLE),
        PICK_FILE);
    } catch (RuntimeException e) { clearPendingAttachment(); showAttachmentError("ファイルの選択を開始できません", e); }
  }

  private File captureDirectory() { return new File(getCacheDir(), "twonote-camera"); }

  private Uri captureUri(String name) throws IOException {
    return FileProvider.getUriForFile(this, getPackageName() + ".capture", CaptureFiles.resolve(captureDirectory(), name));
  }

  private void deleteCapture(String name) throws IOException {
    revokeUriPermission(captureUri(name), Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    CaptureFiles.delete(captureDirectory(), name);
  }

  private void takePhoto() {
    if (!beginAttachment()) return;
    try {
      pendingCaptureName = CaptureFiles.create(captureDirectory()).getName();
      // CAMERA is declared by QR scanning, so Android also requires its runtime grant for this intent.
      if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.CAMERA)
          == android.content.pm.PackageManager.PERMISSION_GRANTED) launchCamera();
      else cameraPermission.launch(android.Manifest.permission.CAMERA);
    } catch (Exception e) { clearPendingAttachment(); showAttachmentError("撮影を開始できません", e); }
  }

  private void launchCamera() {
    if (pendingCaptureName == null || pendingAttachmentRevision == null) return;
    try {
      Uri output = captureUri(pendingCaptureName);
      Intent intent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE)
          .putExtra(MediaStore.EXTRA_OUTPUT, output)
          .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
      intent.setClipData(ClipData.newRawUri("TwoNote camera", output));
      startActivityForResult(intent, TAKE_PHOTO);
    } catch (Exception e) { clearPendingAttachment(); showAttachmentError("カメラを開けません。写真を選ぶ機能も使えます", e); }
  }

  private void cameraResult(int resultCode) {
    if (pendingCaptureName == null || pendingAttachmentRevision == null) {
      notice("撮影先を復元できません。記事からもう一度撮影してください");
      return;
    }
    try {
      Uri output = captureUri(pendingCaptureName);
      revokeUriPermission(output, Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
      if (resultCode != RESULT_OK) { clearPendingAttachment(); notice("撮影を中止しました"); return; }
      CaptureFiles.validatePhoto(captureDirectory(), pendingCaptureName);
      pendingAttachment = output;
      if (vault != null) opened();
      else { showUnlock(); notice("ロックを解除すると撮影した写真を記事に追加します"); }
    } catch (Exception e) { clearPendingAttachment(); showAttachmentError("撮影結果を読み取れません", e); }
  }

  private void selectedAttachment(Uri uri) {
    if (uri == null) { clearPendingAttachment(); return; }
    if (pendingAttachmentRevision == null || pendingCaptureName != null || !"content".equals(uri.getScheme())) {
      clearPendingAttachment();
      notice("添付先を復元できません。記事からもう一度選んでください");
      return;
    }
    pendingAttachment = uri;
    boolean alreadyGranted = false;
    for (android.content.UriPermission grant : getContentResolver().getPersistedUriPermissions())
      if (grant.getUri().equals(uri) && grant.isReadPermission()) alreadyGranted = true;
    if (!alreadyGranted) {
      try {
        getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        pendingAttachmentGrantOwned = true;
      } catch (SecurityException ignored) { /* Some providers offer transient access only. */ }
    }
    if (vault != null) opened();
    else { showUnlock(); notice("ロックを解除すると選んだ写真・ファイルを記事に追加します"); }
  }

  private void importAttachment(Uri uri) {
    if (busy || current == null) return;
    busy = true;
    notice("添付を読み込み中…");
    for (EditText e : new EditText[] {title, content, notebook, section, tags}) e.setEnabled(false);
    int epoch = session;
    LocalVault target = vault;
    JSONObject note = current;
    int insertion = pendingAttachmentOffset;
    worker.execute(
        () -> {
          try {
            String requestId = LocalVault.hash(LocalVault.utf8(note.getString("revision") + "\n" + uri));
            for (JSONObject head : target.heads(true)) {
              if (!head.getString("note_id").equals(note.getString("note_id"))) continue;
              JSONArray existing = head.getJSONArray("attachments");
              for (int i = 0; i < existing.length(); i++) if (requestId.equals(existing.getJSONObject(i).optString("import_request"))) {
                complete(epoch, () -> { current = head; clearPendingAttachment(); showEditor(); notice("選択した添付は追加済みです"); });
                return;
              }
            }
            String name = "attachment", mime = getContentResolver().getType(uri);
            try (Cursor c =
                getContentResolver()
                    .query(uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
              if (c != null && c.moveToFirst()) name = c.getString(0);
            }
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            try (InputStream input = getContentResolver().openInputStream(uri)) {
              if (input == null) throw new IOException("ファイルを開けません");
              byte[] buffer = new byte[65536];
              int count;
              while ((count = input.read(buffer)) != -1) {
                if (output.size() + count > LocalVault.MAX_BLOB)
                  throw new IOException("添付は64MBまでです");
                output.write(buffer, 0, count);
              }
            }
            if (epoch != session) throw new IOException("ロックされました");
            JSONObject a = target.addBlob(name, mime, output.toByteArray());
            a.put("import_request", requestId);
            JSONObject values = LocalVault.copy(note);
            if (InlineBody.supported(a)) values.put("content", InlineBody.insert(note.getString("content"), note.getJSONArray("attachments"), a.getString("id"), insertion));
            values.getJSONArray("attachments").put(a);
            JSONObject saved =
                target.save(
                    note.getString("note_id"),
                    Collections.singletonList(note.getString("revision")),
                    values);
            complete(
                epoch,
                () -> {
                  current = saved;
                  clearPendingAttachment();
                  showEditor();
                });
          } catch (Exception e) {
            complete(
                epoch,
                () -> {
                  showEditor();
                  showError("添付できません", e);
                });
          }
        });
  }

  private void synchronize() {
    if (busy || !flush()) return;
    try {
      JSONObject peer = vault.settings().optJSONObject("peer");
      if (peer == null) {
        lockMemory();
        showPairing("");
        return;
      }
      busy = true;
      notice("同期中…PCの同期待受が必要です");
      int epoch = session;
      LocalVault target = vault;
      worker.execute(
          () -> {
            try {
              JSONObject result =
                  SyncClient.exchange(
                      target,
                      peer,
                      true,
                      message -> {
                        if (epoch != session) throw new IllegalStateException("ロックされました");
                        runOnUiThread(
                            () -> {
                              if (epoch == session) notice(message);
                            });
                      });
              complete(epoch, () -> syncFinished(result));
            } catch (Exception e) {
              complete(
                  epoch,
                  () -> {
                    notice("同期できません。ローカルのノートは保持しています");
                    showError("同期できません", e);
                  });
            }
          });
    } catch (Exception e) {
      showError("同期できません", e);
    }
  }

  private void settings() {
    if (busy) return;
    new StyledDialogBuilder()
        .setItems(
            new String[] {"表示・文字サイズ", "ロックする", "指紋・端末認証を設定", "PCと再ペアリング", "削除したノートの表示を切替"},
            (d, index) -> {
              if (index == 0) displaySettings();
              if (index == 1) {
                lockMemory();
                showUnlock();
              }
              if (index == 2) promptBiometric(true);
              if (index == 3) {
                lockMemory();
                scan();
              }
              if (index == 4 && flush()) {
                showDeleted = !showDeleted;
                showNotes();
              }
            })
        .show();
  }

  private void syncFinished(JSONObject result) {
    try {
      if (current == null) showNotes();
      else if (!dirty) {
        List<JSONObject> heads = new ArrayList<>();
        for (JSONObject note : vault.heads(showDeleted && current.optBoolean("deleted")))
          if (note.getString("note_id").equals(current.getString("note_id"))) heads.add(note);
        if (heads.size() == 1
            && !heads.get(0).getString("revision").equals(current.getString("revision"))) {
          current = heads.get(0);
          showEditor();
        }
        else if (heads.isEmpty()) { current = null; showNotes(); }
      }
      notice(
          "同期完了：送信 "
              + result.optInt("uploaded")
              + " / 受信 "
              + result.optInt("downloaded")
              + "。通信は終了しました");
    } catch (Exception e) {
      showError("同期後の表示を更新できません", e);
    }
  }

  @Override
  protected void onActivityResult(int requestCode, int resultCode, Intent data) {
    IntentResult scan = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
    if (scan != null) {
      if (scan.getContents() != null) showPairing(scan.getContents());
      else showUnlock();
      return;
    }
    super.onActivityResult(requestCode, resultCode, data);
    if (requestCode == TAKE_PHOTO) cameraResult(resultCode);
    else if (requestCode == PICK_FILE) selectedAttachment(resultCode == RESULT_OK && data != null ? data.getData() : null);
  }

  private boolean clearPendingAttachment() {
    if (pendingCaptureName != null) {
      try { deleteCapture(pendingCaptureName); }
      catch (Exception e) {
        showAttachmentError("撮影用一時ファイルを削除できません。再度取り消すかアプリを開き直してください", e);
        return false;
      }
    }
    if (pendingAttachment != null && pendingAttachmentGrantOwned) {
      try { getContentResolver().releasePersistableUriPermission(pendingAttachment, Intent.FLAG_GRANT_READ_URI_PERMISSION); }
      catch (SecurityException ignored) {}
    }
    pendingAttachment = null; pendingAttachmentRevision = null; pendingAttachmentVaultId = null;
    pendingAttachmentGrantOwned = false;
    pendingCaptureName = null;
    pendingAttachmentOffset = -1;
    return true;
  }

  @Override
  protected void onSaveInstanceState(@NonNull Bundle state) {
    checkpointBlockDraft();
    if (vault != null && current != null && dirty) {
      try { vault.saveDraft(editorValues()); }
      catch (Exception e) { notice("下書きを退避できません。空き容量を確認して保存してください"); }
    }
    if (pendingAttachmentRevision != null && pendingAttachmentVaultId != null) {
      try {
        state.putString("pendingAttachment", new PendingAttachment(pendingAttachmentRevision, pendingAttachmentVaultId,
            pendingAttachment == null ? null : pendingAttachment.toString(), pendingAttachmentGrantOwned, pendingCaptureName, pendingAttachmentOffset).encode());
      } catch (Exception ignored) {}
    }
    super.onSaveInstanceState(state);
  }

  private void lockMemory() {
    checkpointBlockDraft();
    if (vault != null) {
      if (!flush() && current != null) {
        try {
          byte[] key = vault.biometricKey();
          try {
            sealedDraft =
                LocalVault.seal(
                    key,
                    LocalVault.utf8(editorValues().toString()),
                    LocalVault.aad(vault.vaultId(), "draft"));
          } finally {
            Arrays.fill(key, (byte) 0);
          }
        } catch (Exception ignored) {
        }
      }
      vault.lock();
      vault = null;
    }
    handler.removeCallbacks(autosave);
    session++;
    navigation = null;
    treeRefresh = null;
    treeQuery = selectedBook = selectedSection = "";
    for (AlertDialog dialog : new ArrayList<>(dialogs)) dialog.dismiss();
    dialogs.clear();
    clearBlockForm();
    busy = false;
    current = null;
    dirty = false;
    loading = true;
    for (EditText e : new EditText[] {title, content, notebook, section, tags})
      if (e != null) e.setText("");
    title = null;
    content = null;
    notebook = null;
    section = null;
    tags = null;
    if (imageDialog != null) {
      imageDialog.dismiss();
      imageDialog = null;
    }
    if (screen != null) screen.removeAllViews();
    releaseBitmaps();
    clearPendingKey();
  }

  @Override
  protected void onResume() {
    super.onResume();
    foreground = true;
    if (vault == null && !busy && !prompting && screen != null && screen.getChildCount() == 0)
      showUnlock();
    if (deferredAttachmentError != null) {
      showError(deferredAttachmentErrorTitle, deferredAttachmentError);
      deferredAttachmentErrorTitle = null; deferredAttachmentError = null;
    }
  }

  @Override
  protected void onStop() {
    foreground = false;
    if (!prompting) lockMemory();
    super.onStop();
  }

  @Override
  protected void onDestroy() {
    if (prompting) {
      prompting = false;
      biometric.cancelAuthentication();
    }
    lockMemory();
    worker.shutdownNow();
    super.onDestroy();
  }
}
