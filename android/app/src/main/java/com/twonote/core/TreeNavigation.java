package com.twonote.core;

import java.util.*;
import org.json.JSONArray;
import org.json.JSONObject;

/** Encrypted, device-local tree order. Classification changes are ordinary synced revisions. */
public final class TreeNavigation {
  public static final int MAX_LAYOUT = 8 * 1024 * 1024;
  private final LocalVault vault;
  private JSONObject state;

  public static final class Node {
    public final String kind, book, section;
    public final JSONObject note;
    public Node(String kind, String book, String section, JSONObject note) {
      this.kind = kind; this.book = book; this.section = section; this.note = note;
    }
    public int depth() { return kind.equals("book") ? 0 : kind.equals("section") ? 1 : 2; }
    public boolean blocked() { return note != null && (note.optBoolean("deleted") || note.optInt("conflicts") > 1); }
    public String key() { return note == null ? groupKey(kind, book, section) : note.optString("note_id"); }
  }

  public TreeNavigation(LocalVault vault) throws Exception { this.vault = vault; state = vault.recoverNavigation(); }
  public static JSONObject empty() throws Exception {
    return new JSONObject().put("version", 1).put("books", new JSONArray()).put("sections", new JSONObject())
        .put("notes", new JSONArray()).put("collapsed", new JSONArray());
  }
  private static List<String> unique(JSONArray array) throws Exception {
    List<String> list = LocalVault.strings(array);
    if (list.size() > 100000 || new HashSet<>(list).size() != list.size())
      throw new IllegalArgumentException("ツリー設定が不正です");
    return list;
  }
  public static JSONObject validate(JSONObject value) throws Exception {
    if (value.getInt("version") != 1) throw new IllegalArgumentException("ツリー設定が不正です");
    List<String> books = unique(value.getJSONArray("books"));
    for (String id : unique(value.getJSONArray("notes")))
      if (!id.matches("[0-9a-f]{32}")) throw new IllegalArgumentException("記事IDが不正です");
    unique(value.getJSONArray("collapsed"));
    JSONObject sections = value.getJSONObject("sections");
    Iterator<String> keys = sections.keys();
    while (keys.hasNext()) {
      String book = keys.next();
      if (!books.contains(book)) throw new IllegalArgumentException("分類が不正です");
      unique(sections.getJSONArray(book));
    }
    return LocalVault.copy(value);
  }
  public static String groupKey(String kind, String book, String section) {
    return new JSONArray(Arrays.asList(kind, book, kind.equals("book") ? "" : section)).toString();
  }
  public static String validName(String value) {
    if (value == null || value.trim().isEmpty() || value.trim().length() > 256)
      throw new IllegalArgumentException("名前は1〜256文字で入力してください");
    return value.trim();
  }
  private void publish(JSONObject next) throws Exception {
    if (state.toString().equals(next.toString())) return;
    vault.setNavigation(next);
    state = next;
  }
  private static void folder(JSONObject layout, String book, String section) throws Exception {
    JSONArray books = layout.getJSONArray("books");
    if (!LocalVault.strings(books).contains(book)) books.put(book);
    JSONObject sections = layout.getJSONObject("sections");
    if (!sections.has(book)) sections.put(book, new JSONArray());
    JSONArray names = sections.getJSONArray(book);
    if (section != null && !LocalVault.strings(names).contains(section)) names.put(section);
  }
  private void ensure(List<JSONObject> heads) throws Exception {
    JSONObject next = LocalVault.copy(state);
    Set<String> known = new HashSet<>(LocalVault.strings(next.getJSONArray("notes")));
    List<JSONObject> sorted = new ArrayList<>(heads);
    sorted.sort(Comparator.comparing((JSONObject n) -> n.optString("notebook").toLowerCase(Locale.ROOT))
        .thenComparing(n -> n.optString("section").toLowerCase(Locale.ROOT))
        .thenComparing(n -> n.optString("title").toLowerCase(Locale.ROOT)).thenComparing(n -> n.optString("note_id")));
    for (JSONObject note : sorted) {
      folder(next, note.getString("notebook"), note.getString("section"));
      if (known.add(note.getString("note_id"))) next.getJSONArray("notes").put(note.getString("note_id"));
    }
    publish(next);
  }
  public boolean collapsed(Node node) throws Exception {
    synchronized (vault) { return LocalVault.strings(state.getJSONArray("collapsed")).contains(node.key()); }
  }
  public void toggle(Node node) throws Exception {
    synchronized (vault) {
      JSONObject next = LocalVault.copy(state);
      List<String> keys = LocalVault.strings(next.getJSONArray("collapsed"));
      if (!keys.remove(node.key())) keys.add(node.key());
      next.put("collapsed", new JSONArray(keys)); publish(next);
    }
  }
  public void addFolder(String book, String section) throws Exception {
    synchronized (vault) {
      JSONObject next = LocalVault.copy(state); folder(next, book, section); publish(next);
    }
  }
  public List<Node> rows(String query, boolean deleted) throws Exception { return rows(query, deleted, false); }
  private List<Node> rows(String query, boolean deleted, boolean expandAll) throws Exception {
    synchronized (vault) {
      List<JSONObject> heads = vault.heads(true); ensure(heads);
      Map<String, Integer> ranks = new HashMap<>();
      List<String> ids = LocalVault.strings(state.getJSONArray("notes"));
      for (int i = 0; i < ids.size(); i++) ranks.put(ids.get(i), i);
      heads.sort(Comparator.comparingInt((JSONObject n) -> ranks.get(n.optString("note_id")))
          .thenComparing(n -> n.optString("revision")));
      String q = query.trim().toLowerCase(Locale.ROOT);
      List<Node> result = new ArrayList<>();
      for (String book : LocalVault.strings(state.getJSONArray("books"))) {
        Node bookNode = new Node("book", book, "", null);
        List<Node> children = new ArrayList<>();
        JSONArray sections = state.getJSONObject("sections").optJSONArray(book);
        if (sections == null) sections = new JSONArray();
        for (String section : LocalVault.strings(sections)) {
          Node sectionNode = new Node("section", book, section, null);
          List<Node> notes = new ArrayList<>();
          for (JSONObject n : heads) {
            if (!n.getString("notebook").equals(book) || !n.getString("section").equals(section)) continue;
            if (n.getBoolean("deleted") && !deleted) continue;
            String text = n.optString("title") + " " + n.optString("content") + " " + n.optString("tags") + " " + book + " " + section;
            if (!text.toLowerCase(Locale.ROOT).contains(q)) continue;
            notes.add(new Node("note", book, section, n));
          }
          if (q.isEmpty() || !notes.isEmpty()) {
            children.add(sectionNode);
            if (expandAll || !q.isEmpty() || !collapsed(sectionNode)) children.addAll(notes);
          }
        }
        if (q.isEmpty() || !children.isEmpty()) {
          result.add(bookNode);
          if (expandAll || !q.isEmpty() || !collapsed(bookNode)) result.addAll(children);
        }
      }
      return result;
    }
  }
  public List<Node> destinations() throws Exception {
    List<Node> result = new ArrayList<>();
    for (Node n : rows("", true, true)) if (n.note == null) result.add(n);
    return result;
  }
  private static JSONArray reordered(JSONArray values, String source, String anchor, boolean after) throws Exception {
    List<String> list = LocalVault.strings(values);
    if (!list.contains(source) || (anchor != null && !list.contains(anchor)))
      throw new IllegalArgumentException("分類が更新されています。もう一度選んでください");
    if (source.equals(anchor)) return new JSONArray(list);
    list.remove(source);
    list.add(anchor == null ? list.size() : list.indexOf(anchor) + (after ? 1 : 0), source);
    return new JSONArray(list);
  }
  private JSONObject singleHead(String id, List<JSONObject> heads) throws Exception {
    JSONObject result = null;
    for (JSONObject n : heads) if (n.getString("note_id").equals(id) && !n.getBoolean("deleted")) {
      if (result != null || n.getBoolean("deleted")) throw new IllegalArgumentException("削除済み・競合中の記事は移動できません");
      result = n;
    }
    if (result == null) throw new IllegalArgumentException("記事がありません");
    return result;
  }
  public JSONObject move(Node source, Node target, boolean after) throws Exception {
    synchronized (vault) {
      List<JSONObject> heads = vault.heads(true); ensure(heads);
      if (source.blocked() || (target != null && target.blocked())) throw new IllegalArgumentException("削除済み・競合中の記事は移動できません");
      JSONObject next = LocalVault.copy(state);
      if (source.kind.equals("book")) {
        next.put("books", reordered(next.getJSONArray("books"), source.book, target == null ? null : target.book, after));
        publish(next); return null;
      }
      if (source.kind.equals("section")) {
        if (target != null && !source.book.equals(target.book)) throw new IllegalArgumentException("セクションの並べ替えは同じノートブック内です");
        JSONObject sections = next.getJSONObject("sections");
        sections.put(source.book, reordered(sections.getJSONArray(source.book), source.section,
            target == null || target.kind.equals("book") ? null : target.section, after));
        publish(next); return null;
      }
      JSONObject note = singleHead(source.note.getString("note_id"), heads);
      String book = target == null ? note.getString("notebook") : target.book;
      String section = target == null ? note.getString("section") : target.section;
      JSONObject sections = next.getJSONObject("sections");
      if (!sections.has(book) || (target != null && !target.kind.equals("book")
          && !LocalVault.strings(sections.getJSONArray(book)).contains(section))) throw new IllegalArgumentException("移動先の分類がありません");
      String anchor = null;
      if (target != null && target.note != null) {
        JSONObject other = singleHead(target.note.getString("note_id"), heads);
        if (!other.getString("notebook").equals(book) || !other.getString("section").equals(section))
          throw new IllegalArgumentException("移動先が更新されています");
        anchor = other.getString("note_id");
      }
      folder(next, book, section);
      next.put("notes", reordered(next.getJSONArray("notes"), note.getString("note_id"), anchor, after));
      JSONObject old = state; publish(next);
      try { return vault.save(note.getString("note_id"), Collections.singletonList(note.getString("revision")),
          LocalVault.copy(note).put("notebook", book).put("section", section)); }
      catch (Exception e) { try { publish(old); } catch (Exception rollback) { e.addSuppressed(rollback); } throw e; }
    }
  }
  public void shift(Node source, int direction) throws Exception {
    synchronized (vault) {
      List<Node> siblings = new ArrayList<>();
      for (Node n : rows("", true, true)) if (n.kind.equals(source.kind)
          && (source.kind.equals("book") || n.book.equals(source.book))
          && (!source.kind.equals("note") || n.section.equals(source.section))) siblings.add(n);
      for (int i = 0; i < siblings.size(); i++) if (siblings.get(i).key().equals(source.key())) {
        int other = i + direction;
        if (other >= 0 && other < siblings.size()) move(source, siblings.get(other), direction > 0);
        return;
      }
      throw new IllegalArgumentException("項目がありません");
    }
  }
  public Map<String, JSONObject> renameSection(String book, String section, String name) throws Exception {
    synchronized (vault) {
      name = validName(name); ensure(vault.heads(true));
      List<String> names = LocalVault.strings(state.getJSONObject("sections").getJSONArray(book));
      if (!names.contains(section)) throw new IllegalArgumentException("セクションがありません");
      if (name.equals(section)) return new LinkedHashMap<>();
      if (names.contains(name)) throw new IllegalArgumentException("同じ名前のセクションが既にあります");
      JSONObject next = LocalVault.copy(state);
      names.set(names.indexOf(section), name); next.getJSONObject("sections").put(book, new JSONArray(names));
      String oldKey = groupKey("section", book, section), newKey = groupKey("section", book, name);
      List<String> collapsed = LocalVault.strings(next.getJSONArray("collapsed")); collapsed.remove(newKey);
      if (collapsed.contains(oldKey)) collapsed.set(collapsed.indexOf(oldKey), newKey);
      next.put("collapsed", new JSONArray(collapsed));
      JSONObject old = state; publish(next);
      try { return vault.renameSection(book, section, name); }
      catch (Exception e) { try { publish(old); } catch (Exception rollback) { e.addSuppressed(rollback); } throw e; }
    }
  }
  public void removeEmpty(Node source) throws Exception {
    synchronized (vault) {
      for (JSONObject n : vault.heads(true)) if (n.getString("notebook").equals(source.book)
          && (source.kind.equals("book") || n.getString("section").equals(source.section)))
        throw new IllegalArgumentException("記事がある分類は削除できません（削除済みを含む）");
      JSONObject next = LocalVault.copy(state);
      if (source.kind.equals("book")) {
        List<String> names = LocalVault.strings(next.getJSONArray("books")); names.remove(source.book);
        next.put("books", new JSONArray(names)); next.getJSONObject("sections").remove(source.book);
      } else if (source.kind.equals("section")) {
        List<String> names = LocalVault.strings(next.getJSONObject("sections").getJSONArray(source.book)); names.remove(source.section);
        next.getJSONObject("sections").put(source.book, new JSONArray(names));
      } else throw new IllegalArgumentException("空の分類だけ削除できます");
      publish(next);
    }
  }
}
