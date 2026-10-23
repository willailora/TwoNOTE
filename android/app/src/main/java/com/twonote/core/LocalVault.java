package com.twonote.core;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.*;
import javax.crypto.Cipher;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.PBEKeySpec;
import javax.crypto.spec.SecretKeySpec;
import org.json.JSONArray;
import org.json.JSONObject;

/** Platform-independent encrypted vault; Android and JVM contract tests use it. */
public final class LocalVault {
  public static final int ITERATIONS = 600000,
      MAX_BLOB = 64 * 1024 * 1024,
      MAX_REVISION = 4 * 1024 * 1024,
      MAX_RECORDS = 100000;
  private static final String[] FIELDS = {
    "title", "content", "notebook", "section", "tags", "source_path"
  };
  private final File root;
  private final JSONObject config;
  private final String vaultId;
  private byte[] key;
  private Map<String, JSONObject> records = new HashMap<>();
  private boolean recoveredIndex;
  private boolean recoveredNavigation;

  private LocalVault(File root, JSONObject config, byte[] key) throws Exception {
    this.root = root;
    this.config = copy(config);
    this.vaultId = config.getString("vault_id");
    this.key = key.clone();
    validateConfig(config);
    List<String> ids;
    try {
      ids = readIndex(new File(root, "index.enc"));
    } catch (Exception failure) {
      File backup = new File(root, "index.backup.enc");
      if (!backup.isFile()) throw failure;
      ids = readIndex(backup);
      recoveredIndex = true;
    }
    for (String id : ids) records.put(id, readRevision(id));
    validateGraph(records);
  }

  public static JSONObject copy(JSONObject object) throws Exception {
    return new JSONObject(object.toString());
  }

  public static byte[] utf8(String value) {
    return value.getBytes(StandardCharsets.UTF_8);
  }

  public static String hex(byte[] data) {
    StringBuilder out = new StringBuilder();
    for (byte b : data) out.append(String.format(Locale.ROOT, "%02x", b & 255));
    return out.toString();
  }

  public static String hash(byte[] data) throws Exception {
    return hex(MessageDigest.getInstance("SHA-256").digest(data));
  }

  public static byte[] aad(String vaultId, String purpose) {
    return utf8("twonote:2:" + vaultId + ":" + purpose);
  }

  public static byte[] seal(byte[] key, byte[] data, byte[] aad) throws Exception {
    byte[] nonce = new byte[12];
    new SecureRandom().nextBytes(nonce);
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(
        Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, nonce));
    cipher.updateAAD(aad);
    byte[] ciphertext = cipher.doFinal(data), result = new byte[12 + ciphertext.length];
    System.arraycopy(nonce, 0, result, 0, 12);
    System.arraycopy(ciphertext, 0, result, 12, ciphertext.length);
    return result;
  }

  public static byte[] unseal(byte[] key, byte[] data, byte[] aad) throws Exception {
    if (data.length < 28) throw new IllegalArgumentException("暗号化データが短すぎます");
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(
        Cipher.DECRYPT_MODE,
        new SecretKeySpec(key, "AES"),
        new GCMParameterSpec(128, Arrays.copyOf(data, 12)));
    cipher.updateAAD(aad);
    return cipher.doFinal(data, 12, data.length - 12);
  }

  public static void validateConfig(JSONObject config) throws Exception {
    if (config.getInt("version") != 2
        || !config.getString("vault_id").matches("[0-9a-f]{32}")
        || config.getInt("iterations") != ITERATIONS
        || Base64.getDecoder().decode(config.getString("salt")).length != 16
        || Base64.getDecoder().decode(config.getString("wrapped_key")).length != 60)
      throw new IllegalArgumentException("対応していない保管庫です");
  }

  public static byte[] unlockKey(JSONObject config, String password) throws Exception {
    validateConfig(config);
    PBEKeySpec spec =
        new PBEKeySpec(
            password.toCharArray(),
            Base64.getDecoder().decode(config.getString("salt")),
            ITERATIONS,
            256);
    byte[] wrapping =
        SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded();
    spec.clearPassword();
    try {
      return unseal(
          wrapping,
          Base64.getDecoder().decode(config.getString("wrapped_key")),
          aad(config.getString("vault_id"), "key"));
    } finally {
      Arrays.fill(wrapping, (byte) 0);
    }
  }

  public static JSONObject readConfig(File root) throws Exception {
    return new JSONObject(
        new String(
            Files.readAllBytes(new File(root, "config.json").toPath()), StandardCharsets.UTF_8));
  }

  public static LocalVault open(File root, String password) throws Exception {
    JSONObject config = readConfig(root);
    byte[] key = unlockKey(config, password);
    try {
      return new LocalVault(root, config, key);
    } finally {
      Arrays.fill(key, (byte) 0);
    }
  }

  public static LocalVault openWithKey(File root, byte[] key) throws Exception {
    return new LocalVault(root, readConfig(root), key);
  }

  public static LocalVault create(File root, JSONObject config, String password) throws Exception {
    String[] existing = root.list();
    if (root.exists() && (existing == null || existing.length > 0))
      throw new IllegalStateException("作成先が空ではありません。既存ファイルは変更していません");
    byte[] key = unlockKey(config, password);
    try {
      if (!root.isDirectory() && !root.mkdirs()) throw new IllegalStateException("保存先を作成できません");
      writeAtomic(
          new File(root, "index.enc"),
          seal(
              key,
              utf8(new JSONObject().put("version", 2).put("revisions", new JSONArray()).toString()),
              aad(config.getString("vault_id"), "index")));
      writeAtomic(new File(root, "config.json"), utf8(config.toString()));
      return new LocalVault(root, config, key);
    } finally {
      Arrays.fill(key, (byte) 0);
    }
  }

  public static void writeAtomic(File target, byte[] bytes) throws Exception {
    File parent = target.getParentFile();
    if (!parent.isDirectory() && !parent.mkdirs()) throw new IllegalStateException("保存先を作成できません");
    File temp = File.createTempFile(".write-", ".tmp", parent);
    try {
      try (FileOutputStream output = new FileOutputStream(temp)) {
        output.write(bytes);
        output.getFD().sync();
      }
      Files.move(
          temp.toPath(),
          target.toPath(),
          StandardCopyOption.ATOMIC_MOVE,
          StandardCopyOption.REPLACE_EXISTING);
    } finally {
      if (temp.exists()) temp.delete();
    }
  }

  private void requireKey() {
    if (key == null) throw new IllegalStateException("保管庫はロックされています");
  }

  public synchronized byte[] biometricKey() {
    requireKey();
    return key.clone();
  }

  public synchronized void lock() {
    if (key != null) Arrays.fill(key, (byte) 0);
    key = null;
    records.clear();
  }

  public String vaultId() {
    return vaultId;
  }

  public synchronized JSONObject config() throws Exception {
    return copy(config);
  }

  public boolean recoveredIndex() {
    return recoveredIndex;
  }

  public File root() {
    return root;
  }

  public static List<String> strings(JSONArray array) throws Exception {
    List<String> result = new ArrayList<>();
    for (int i = 0; i < array.length(); i++) {
      Object value = array.get(i);
      if (!(value instanceof String)) throw new IllegalArgumentException("IDが不正です");
      result.add((String) value);
    }
    return result;
  }

  private List<String> readIndex(File file) throws Exception {
    JSONObject object =
        new JSONObject(
            new String(
                unseal(key, Files.readAllBytes(file.toPath()), aad(vaultId, "index")),
                StandardCharsets.UTF_8));
    List<String> ids = strings(object.getJSONArray("revisions"));
    if (object.getInt("version") != 2
        || ids.size() > MAX_RECORDS
        || new HashSet<>(ids).size() != ids.size()) throw new IllegalArgumentException("索引が壊れています");
    for (String id : ids)
      if (!id.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("索引が壊れています");
    return ids;
  }

  public File objectPath(String kind, String id) {
    if (!(kind.equals("blobs") || kind.equals("revisions")) || !id.matches("[0-9a-f]{64}"))
      throw new IllegalArgumentException("IDが不正です");
    return new File(new File(root, kind), id);
  }

  public synchronized byte[] readObject(String kind, String id) throws Exception {
    requireKey();
    File file = objectPath(kind, id);
    int limit = kind.equals("blobs") ? MAX_BLOB : MAX_REVISION;
    if (file.length() > limit + 28L) throw new IllegalArgumentException("ファイルが大きすぎます");
    byte[] raw = Files.readAllBytes(file.toPath());
    if (!hash(raw).equals(id)) throw new IllegalArgumentException("保存ファイルが破損しています");
    return raw;
  }

  private JSONObject readRevision(String id) throws Exception {
    return validateRevision(
        new JSONObject(
            new String(
                unseal(key, readObject("revisions", id), aad(vaultId, "revision")),
                StandardCharsets.UTF_8)));
  }

  private JSONObject validateRevision(JSONObject record) throws Exception {
    if (!record.getString("note_id").matches("[0-9a-f]{32}"))
      throw new IllegalArgumentException("ノートIDが不正です");
    List<String> parents = strings(record.getJSONArray("parents"));
    if (parents.size() > 512 || new HashSet<>(parents).size() != parents.size())
      throw new IllegalArgumentException("履歴が不正です");
    for (String parent : parents)
      if (!parent.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("履歴IDが不正です");
    for (String field : FIELDS)
      if (!(record.get(field) instanceof String)) throw new IllegalArgumentException("ノート形式が不正です");
    if (!(record.get("modified") instanceof String) || !(record.get("deleted") instanceof Boolean))
      throw new IllegalArgumentException("ノート形式が不正です");
    JSONArray attachments = record.getJSONArray("attachments");
    if (attachments.length() > 1000) throw new IllegalArgumentException("添付が多すぎます");
    for (int i = 0; i < attachments.length(); i++) {
      JSONObject item = attachments.getJSONObject(i);
      if (!item.getString("id").matches("[0-9a-f]{64}")
          || !(item.get("filename") instanceof String)
          || !(item.get("mime") instanceof String)
          || !(item.get("size") instanceof Number)
          || item.getLong("size") < 0
          || item.getLong("size") > MAX_BLOB) throw new IllegalArgumentException("添付形式が不正です");
    }
    return record;
  }

  private void validateGraph(Map<String, JSONObject> candidate) throws Exception {
    validateGraph(candidate, candidate.keySet());
  }

  private void validateGraph(Map<String, JSONObject> candidate, Set<String> checking) throws Exception {
    if (candidate.size() > MAX_RECORDS) throw new IllegalArgumentException("履歴が多すぎます");
    Map<String, Integer> degrees = new HashMap<>();
    Map<String, List<String>> children = new HashMap<>();
    Set<String> blobs = new HashSet<>();
    for (String revision : checking) {
      JSONObject record = candidate.get(revision);
      List<String> parents = strings(record.getJSONArray("parents"));
      int degree = 0;
      for (String parent : parents) {
        if (!candidate.containsKey(parent)
            || !candidate.get(parent).getString("note_id").equals(record.getString("note_id")))
          throw new IllegalArgumentException("履歴が不足しています。同期を再試行してください");
        if (checking.contains(parent)) {
          degree++;
          children.computeIfAbsent(parent, k -> new ArrayList<>()).add(revision);
        }
      }
      degrees.put(revision, degree);
      JSONArray attachments = record.getJSONArray("attachments");
      for (int i = 0; i < attachments.length(); i++)
        blobs.add(attachments.getJSONObject(i).getString("id"));
    }
    for (String blob : blobs) if (!objectPath("blobs", blob).isFile())
      throw new IllegalArgumentException("添付が不足しています。同期を再試行してください");
    ArrayDeque<String> ready = new ArrayDeque<>();
    for (String id : degrees.keySet()) if (degrees.get(id) == 0) ready.add(id);
    int visited = 0;
    while (!ready.isEmpty()) {
      visited++;
      String id = ready.remove();
      for (String child : children.getOrDefault(id, Collections.emptyList())) {
        int count = degrees.get(child) - 1;
        degrees.put(child, count);
        if (count == 0) ready.add(child);
      }
    }
    if (visited != checking.size()) throw new IllegalArgumentException("履歴が循環しています");
  }

  public synchronized JSONObject inventory() throws Exception {
    requireKey();
    TreeSet<String> revisions = new TreeSet<>(records.keySet()), blobs = new TreeSet<>();
    for (JSONObject record : records.values()) {
      JSONArray a = record.getJSONArray("attachments");
      for (int i = 0; i < a.length(); i++) blobs.add(a.getJSONObject(i).getString("id"));
    }
    return new JSONObject()
        .put("vault_id", vaultId)
        .put("revisions", new JSONArray(revisions))
        .put("blobs", new JSONArray(blobs));
  }

  public synchronized void stageObject(String kind, String id, byte[] data) throws Exception {
    requireKey();
    File target = objectPath(kind, id);
    int limit = kind.equals("blobs") ? MAX_BLOB : MAX_REVISION;
    if (data.length > limit + 28 || !hash(data).equals(id))
      throw new IllegalArgumentException("同期データのハッシュが不正です");
    byte[] plain = unseal(key, data, aad(vaultId, kind.equals("blobs") ? "blob" : "revision"));
    if (kind.equals("revisions"))
      validateRevision(new JSONObject(new String(plain, StandardCharsets.UTF_8)));
    if (target.isFile()) {
      if (!hash(Files.readAllBytes(target.toPath())).equals(id))
        throw new IllegalArgumentException("既存ファイルが破損しています");
      return;
    }
    writeAtomic(target, data);
  }

  public synchronized int commit(List<String> ids) throws Exception {
    requireKey();
    if (ids.size() > MAX_RECORDS) throw new IllegalArgumentException("変更数が多すぎます");
    Map<String, JSONObject> candidate = new HashMap<>(records);
    Set<String> added = new HashSet<>();
    for (String id : ids)
      if (!candidate.containsKey(id)) {
        candidate.put(id, readRevision(id));
        added.add(id);
      }
    if (added.isEmpty()) return 0;
    validateGraph(candidate, added);
    byte[] encrypted =
        seal(
            key,
            utf8(
                new JSONObject()
                    .put("version", 2)
                    .put("revisions", new JSONArray(new TreeSet<>(candidate.keySet())))
                    .toString()),
            aad(vaultId, "index"));
    if (!recoveredIndex)
      writeAtomic(
          new File(root, "index.backup.enc"),
          Files.readAllBytes(new File(root, "index.enc").toPath()));
    writeAtomic(new File(root, "index.enc"), encrypted);
    records = candidate;
    recoveredIndex = false;
    return added.size();
  }

  public synchronized JSONObject get(String revision) throws Exception {
    requireKey();
    if (!records.containsKey(revision)) throw new IllegalArgumentException("ノートがありません");
    return copy(records.get(revision)).put("revision", revision);
  }

  public synchronized List<JSONObject> heads(boolean deleted) throws Exception {
    requireKey();
    Set<String> parents = new HashSet<>();
    for (JSONObject record : records.values())
      parents.addAll(strings(record.getJSONArray("parents")));
    Map<String, Integer> counts = new HashMap<>();
    for (String id : records.keySet())
      if (!parents.contains(id) && !records.get(id).getBoolean("deleted")) {
        String nid = records.get(id).getString("note_id");
        counts.put(nid, counts.getOrDefault(nid, 0) + 1);
      }
    List<JSONObject> result = new ArrayList<>();
    for (String id : records.keySet())
      if (!parents.contains(id)) {
        JSONObject r = records.get(id);
        int count = r.getBoolean("deleted") ? 1 : counts.get(r.getString("note_id"));
        if (!deleted && r.getBoolean("deleted")) continue;
        result.add(copy(r).put("revision", id).put("conflicts", count));
      }
    result.sort(
        (a, b) -> {
          int order = b.optString("modified").compareTo(a.optString("modified"));
          return order != 0 ? order : b.optString("revision").compareTo(a.optString("revision"));
        });
    return result;
  }

  public synchronized JSONObject save(String noteId, List<String> parents, JSONObject values)
      throws Exception {
    requireKey();
    String rid = stageRevision(noteId, parents, values);
    commit(Collections.singletonList(rid));
    return get(rid);
  }

  /** Resolve the reviewed snapshot only, never resurrect content from deleted branches. */
  public synchronized JSONObject resolveVersions(String revision, List<String> expected, boolean merge) throws Exception {
    JSONObject chosen = get(revision);
    List<JSONObject> heads = new ArrayList<>(), live = new ArrayList<>();
    Set<String> actual = new HashSet<>();
    for (JSONObject n : heads(true)) if (n.getString("note_id").equals(chosen.getString("note_id"))) {
      heads.add(n); actual.add(n.getString("revision")); if (!n.getBoolean("deleted")) live.add(n);
    }
    if (!actual.contains(revision) || !actual.equals(new HashSet<>(expected)))
      throw new IllegalArgumentException("別の端末で編集版が更新されました。表示を確認してからやり直してください");
    if (chosen.getBoolean("deleted")) throw new IllegalArgumentException("削除済みの版は採用できません。必要な場合は先に復元してください");
    if (merge) {
      JSONArray files = new JSONArray(); Set<String> ids = new HashSet<>();
      for (JSONObject n : live) {
        JSONArray attachments = n.getJSONArray("attachments");
        for (int i = 0; i < attachments.length(); i++) {
          JSONObject a = attachments.getJSONObject(i);
          if (ids.add(a.getString("id"))) files.put(a);
        }
      }
      chosen.put("content", InlineBody.merge(live)).put("attachments", files);
    }
    List<String> parents = new ArrayList<>(actual); Collections.sort(parents);
    return save(chosen.getString("note_id"), parents, chosen);
  }

  public synchronized JSONObject setDeleted(String revision, boolean deleted) throws Exception {
    JSONObject chosen = get(revision); boolean found = false;
    for (JSONObject n : heads(true)) if (n.getString("revision").equals(revision)) found = true;
    if (!found) throw new IllegalArgumentException("選択した編集版は更新されています。表示を確認してからやり直してください");
    return save(chosen.getString("note_id"), Collections.singletonList(revision), chosen.put("deleted", deleted));
  }

  private String stageRevision(String noteId, List<String> parents, JSONObject values)
      throws Exception {
    JSONObject record = new JSONObject();
    for (String field : FIELDS) record.put(field, values.optString(field, ""));
    if (record.getString("title").trim().isEmpty()) record.put("title", "無題");
    String modified =
        java.time.format.DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'")
            .withZone(java.time.ZoneOffset.UTC)
            .format(Instant.now());
    record
        .put("note_id", noteId)
        .put("parents", new JSONArray(parents))
        .put("modified", modified)
        .put("deleted", values.optBoolean("deleted", false));
    record.put(
        "attachments",
        new JSONArray(
            values.optJSONArray("attachments") == null
                ? "[]"
                : values.getJSONArray("attachments").toString()));
    validateRevision(record);
    for (String parent : parents)
      if (!records.containsKey(parent) || !records.get(parent).getString("note_id").equals(noteId))
        throw new IllegalArgumentException("保存元のノートが違います");
    if (parents.size() == 1) {
      JSONObject previous = records.get(parents.get(0));
      boolean same = true;
      for (String field : FIELDS) same &= previous.getString(field).equals(record.getString(field));
      same &= previous.getBoolean("deleted") == record.getBoolean("deleted");
      same &=
          previous
              .getJSONArray("attachments")
              .toString()
              .equals(record.getJSONArray("attachments").toString());
      if (same) return parents.get(0);
    }
    byte[] plain = utf8(record.toString());
    if (plain.length > MAX_REVISION) throw new IllegalArgumentException("ノートが大きすぎます");
    byte[] encrypted = seal(key, plain, aad(vaultId, "revision"));
    String rid = hash(encrypted);
    stageObject("revisions", rid, encrypted);
    return rid;
  }

  public synchronized Map<String, JSONObject> renameSection(String book, String section, String name)
      throws Exception {
    requireKey();
    name = TreeNavigation.validName(name);
    Map<String, String> changes = new LinkedHashMap<>();
    if (name.equals(section)) return new LinkedHashMap<>();
    List<JSONObject> heads = heads(true);
    for (JSONObject note : heads)
      if (note.getString("notebook").equals(book) && note.getString("section").equals(name))
        throw new IllegalArgumentException("同じ名前のセクションが既にあります");
    for (JSONObject note : heads)
      if (note.getString("notebook").equals(book) && note.getString("section").equals(section)) {
        String previous = note.getString("revision");
        changes.put(previous, stageRevision(note.getString("note_id"),
            Collections.singletonList(previous), copy(note).put("section", name)));
      }
    commit(new ArrayList<>(changes.values()));
    Map<String, JSONObject> result = new LinkedHashMap<>();
    for (Map.Entry<String, String> change : changes.entrySet()) result.put(change.getKey(), get(change.getValue()));
    return result;
  }

  public synchronized JSONObject navigation() throws Exception {
    requireKey();
    File file = new File(root, "navigation.enc");
    if (!file.isFile()) return TreeNavigation.empty();
    if (file.length() > TreeNavigation.MAX_LAYOUT + 28L) throw new IllegalArgumentException("ツリー設定が大きすぎます");
    return TreeNavigation.validate(new JSONObject(new String(
        unseal(key, Files.readAllBytes(file.toPath()), aad(vaultId, "navigation")), StandardCharsets.UTF_8)));
  }

  public synchronized JSONObject recoverNavigation() throws Exception {
    requireKey();
    try { return navigation(); }
    catch (org.json.JSONException | IllegalArgumentException | java.security.GeneralSecurityException error) {
      File original = new File(root, "navigation.enc");
      File saved = new File(root, "navigation.invalid-" + UUID.randomUUID().toString().replace("-", "") + ".enc");
      Files.move(original.toPath(), saved.toPath());
      recoveredNavigation = true;
      return TreeNavigation.empty();
    }
  }

  public synchronized boolean recoveredNavigation() { return recoveredNavigation; }

  public synchronized void setNavigation(JSONObject value) throws Exception {
    requireKey();
    byte[] raw = utf8(TreeNavigation.validate(value).toString());
    if (raw.length > TreeNavigation.MAX_LAYOUT) throw new IllegalArgumentException("ツリー設定が大きすぎます");
    writeAtomic(new File(root, "navigation.enc"), seal(key, raw, aad(vaultId, "navigation")));
  }

  public synchronized JSONObject addBlob(String filename, String mime, byte[] raw)
      throws Exception {
    requireKey();
    if (raw.length > MAX_BLOB) throw new IllegalArgumentException("添付は64MBまでです");
    byte[] encrypted = seal(key, raw, aad(vaultId, "blob"));
    String id = hash(encrypted);
    stageObject("blobs", id, encrypted);
    return new JSONObject()
        .put("id", id)
        .put("filename", new File(filename).getName())
        .put("mime", mime == null ? "application/octet-stream" : mime)
        .put("size", raw.length);
  }

  public synchronized byte[] readBlob(String id) throws Exception {
    requireKey();
    return unseal(key, readObject("blobs", id), aad(vaultId, "blob"));
  }

  private JSONObject validateBlockDraft(JSONObject draft) throws Exception {
    BlockDraft.validate(draft);
    JSONObject base = get(draft.getString("revision"));
    if (!base.getString("note_id").equals(draft.getString("note_id")) || base.getBoolean("deleted"))
      throw new IllegalArgumentException("表・グラフの編集元を確認できません");
    if (!draft.isNull("target")) {
      boolean found = false; JSONArray files = base.getJSONArray("attachments");
      for (int i = 0; i < files.length(); i++) {
        JSONObject a = files.getJSONObject(i);
        if (a.getString("id").equals(draft.getString("target")) && a.getString("mime").equals(InlineBody.BLOCK_MIME)) found = true;
      }
      if (!found) throw new IllegalArgumentException("編集元の表・グラフがありません");
    }
    return draft;
  }

  public synchronized void saveBlockDraft(JSONObject draft) throws Exception {
    requireKey(); validateBlockDraft(draft);
    JSONObject previous = blockDraftFile();
    if (previous != null && !previous.getString("draft_id").equals(draft.getString("draft_id")))
      throw new IllegalStateException("別の表・グラフの下書きがあります。先に再開または破棄してください");
    writeAtomic(new File(root, "block-draft.enc"), seal(key, utf8(draft.toString()), aad(vaultId, "block-draft")));
  }

  private JSONObject blockDraftFile() throws Exception {
    File file = new File(root, "block-draft.enc");
    if (!file.isFile()) return null;
    if (file.length() > BlockDraft.MAX_BYTES + 28L) throw new IllegalArgumentException("表の下書きが大きすぎます");
    return validateBlockDraft(new JSONObject(new String(unseal(key, Files.readAllBytes(file.toPath()), aad(vaultId, "block-draft")), StandardCharsets.UTF_8)));
  }

  private JSONObject committedBlockEdit(JSONObject draft) throws Exception {
    String base = draft.getString("revision"), request = draft.getString("draft_id");
    for (Map.Entry<String, JSONObject> entry : records.entrySet()) {
      JSONObject note = entry.getValue();
      if (!note.getString("note_id").equals(draft.getString("note_id")) || !strings(note.getJSONArray("parents")).contains(base)) continue;
      JSONArray files = note.getJSONArray("attachments");
      for (int i = 0; i < files.length(); i++) if (request.equals(files.getJSONObject(i).optString("edit_request"))) return get(entry.getKey());
    }
    return null;
  }

  public synchronized JSONObject readBlockDraft() throws Exception {
    requireKey(); JSONObject draft = blockDraftFile();
    if (draft != null && committedBlockEdit(draft) != null) {
      try { clearBlockDraft(draft.getString("draft_id")); } catch (java.io.IOException ignored) {}
      return null;
    }
    return draft;
  }

  public synchronized void clearBlockDraft(String request) throws Exception {
    requireKey(); JSONObject draft = blockDraftFile();
    if (draft != null && draft.getString("draft_id").equals(request)) Files.deleteIfExists(new File(root, "block-draft.enc").toPath());
  }

  /** Use the pinned edit base even when another device has published newer data. */
  public synchronized JSONObject saveBlockEdit(JSONObject draft) throws Exception {
    requireKey(); validateBlockDraft(draft);
    JSONObject committed = committedBlockEdit(draft);
    if (committed != null) return committed;
    JSONObject block = NoteBlock.parse(draft.getString("kind"), draft.getString("title"), draft.getString("text"));
    JSONObject base = get(draft.getString("revision"));
    JSONObject added = addBlob((block.getString("title").isEmpty() ? "表・グラフ" : block.getString("title")) + ".json",
        InlineBody.BLOCK_MIME, NoteBlock.dump(block)).put("edit_request", draft.getString("draft_id"));
    JSONArray files = base.getJSONArray("attachments"), next = new JSONArray();
    String target = draft.isNull("target") ? null : draft.getString("target");
    String body = target == null ? InlineBody.insert(base.getString("content"), files, added.getString("id"), draft.getInt("offset"))
        : InlineBody.encode(InlineBody.body(base.getString("content"), files).replace(InlineBody.token(target), InlineBody.token(added.getString("id"))));
    for (int i = 0; i < files.length(); i++) if (!files.getJSONObject(i).getString("id").equals(target)) next.put(files.getJSONObject(i));
    next.put(added);
    JSONObject saved = save(base.getString("note_id"), Collections.singletonList(base.getString("revision")),
        base.put("content", body).put("attachments", next));
    try { clearBlockDraft(draft.getString("draft_id")); } catch (java.io.IOException ignored) {}
    return saved;
  }

  /** A failed normal save must survive Activity/process recreation, still encrypted. */
  public synchronized void saveDraft(JSONObject values) throws Exception {
    requireKey();
    validateDraft(values);
    byte[] raw = utf8(values.toString());
    if (raw.length > 16 * 1024 * 1024) throw new IllegalArgumentException("退避できる下書きは16MBまでです");
    writeAtomic(new File(root, "draft.enc"), seal(key, raw, aad(vaultId, "draft")));
  }

  private JSONObject validateDraft(JSONObject draft) throws Exception {
    validateRevision(draft);
    String revision = draft.getString("revision");
    if (!revision.matches("[0-9a-f]{64}") || !records.containsKey(revision)
        || !records.get(revision).getString("note_id").equals(draft.getString("note_id")))
      throw new IllegalArgumentException("下書きの元記事を確認できません。退避ファイルは保持しています");
    return draft;
  }

  private JSONObject draftFile() throws Exception {
    File file = new File(root, "draft.enc");
    if (!file.isFile()) return null;
    if (file.length() > 16 * 1024 * 1024 + 28L) throw new IllegalArgumentException("下書きが大きすぎます");
    return validateDraft(new JSONObject(new String(
        unseal(key, Files.readAllBytes(file.toPath()), aad(vaultId, "draft")), StandardCharsets.UTF_8)));
  }

  public synchronized JSONObject readDraft() throws Exception {
    requireKey();
    JSONObject draft = draftFile();
    if (draft == null) return null;
    // Saving a note and deleting its draft are separate operations. If cleanup
    // failed or the process died between them, do not replay an already-saved draft.
    String base = draft.getString("revision");
    for (Map.Entry<String, JSONObject> entry : records.entrySet()) {
      JSONObject saved = entry.getValue();
      if (!saved.getString("note_id").equals(draft.getString("note_id"))) continue;
      if (!entry.getKey().equals(base) && !strings(saved.getJSONArray("parents")).contains(base)) continue;
      boolean same = saved.getBoolean("deleted") == draft.getBoolean("deleted")
          && saved.getJSONArray("attachments").toString().equals(draft.getJSONArray("attachments").toString());
      for (String field : FIELDS) {
        String expected = draft.getString(field);
        if (field.equals("title") && expected.trim().isEmpty()) expected = "無題";
        same &= saved.getString(field).equals(expected);
      }
      if (same) {
        try { Files.deleteIfExists(new File(root, "draft.enc").toPath()); } catch (java.io.IOException ignored) {}
        return null;
      }
    }
    return draft;
  }

  public synchronized void clearDraft(String noteId) throws Exception {
    requireKey();
    JSONObject draft = draftFile();
    if (draft != null && draft.getString("note_id").equals(noteId))
      Files.deleteIfExists(new File(root, "draft.enc").toPath());
  }

  public synchronized JSONObject settings() throws Exception {
    requireKey();
    File file = new File(root, "settings.enc");
    return file.isFile()
        ? new JSONObject(
            new String(
                unseal(key, Files.readAllBytes(file.toPath()), aad(vaultId, "settings")),
                StandardCharsets.UTF_8))
        : new JSONObject();
  }

  public synchronized void setSettings(JSONObject settings) throws Exception {
    requireKey();
    writeAtomic(
        new File(root, "settings.enc"),
        seal(key, utf8(settings.toString()), aad(vaultId, "settings")));
  }
}
