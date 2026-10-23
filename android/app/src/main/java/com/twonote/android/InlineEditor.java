package com.twonote.android;

import android.content.Context;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.graphics.Bitmap;
import android.graphics.drawable.BitmapDrawable;
import android.text.*;
import android.text.style.ImageSpan;
import androidx.appcompat.widget.AppCompatEditText;
import com.twonote.core.InlineBody;
import java.util.*;
import java.util.regex.Matcher;
import org.json.*;

/** Native editable text with attachment-backed spans, never HTML/WebView content. */
final class InlineEditor extends AppCompatEditText {
  interface Preview { Bitmap load(String id) throws Exception; }
  private final Map<String, JSONObject> files = new HashMap<>();
  private Preview preview;
  private int imageWidth;
  private boolean inlineMode;
  static final class EmbedSpan extends ImageSpan {
    final String id;
    EmbedSpan(BitmapDrawable drawable, String id) { super(drawable, ALIGN_BASELINE); this.id = id; }
  }
  InlineEditor(Context context) { super(context); }
  void load(String content, JSONArray attachments, Preview preview, int width) throws Exception {
    this.preview = preview; imageWidth = width; files.clear();
    for (int i = 0; i < attachments.length(); i++) { JSONObject a = attachments.getJSONObject(i); if (InlineBody.supported(a)) files.put(a.getString("id"), a); }
    inlineMode = content.startsWith(InlineBody.HEADER) || !files.isEmpty();
    setText(render(InlineBody.body(content, attachments)), BufferType.EDITABLE);
  }
  private SpannableStringBuilder render(String text) throws Exception {
    SpannableStringBuilder result = new SpannableStringBuilder(); Matcher matcher = InlineBody.TOKEN.matcher(text); int offset = 0;
    while (matcher.find()) {
      String id = matcher.group(1); if (!files.containsKey(id)) continue;
      result.append(text.substring(offset, matcher.start()));
      Bitmap bitmap = preview.load(id);
      if (bitmap == null) result.append(matcher.group()); // The token survives even if a preview cannot be decoded.
      else {
        BitmapDrawable drawable = new BitmapDrawable(getResources(), bitmap);
        int width = Math.min(imageWidth, bitmap.getWidth());
        drawable.setBounds(0, 0, width, Math.max(1, (int) ((long) bitmap.getHeight() * width / bitmap.getWidth())));
        int start = result.length(); result.append('\ufffc');
        result.setSpan(new EmbedSpan(drawable, id), start, result.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
      }
      offset = matcher.end();
    }
    result.append(text.substring(offset)); return result;
  }
  String portable(int start, int end) {
    Editable text = getText();
    start = Math.max(0, Math.min(start, text.length())); end = Math.max(start, Math.min(end, text.length()));
    EmbedSpan[] spans = text.getSpans(start, end, EmbedSpan.class);
    Arrays.sort(spans, Comparator.comparingInt(text::getSpanStart));
    StringBuilder out = new StringBuilder(); int offset = start;
    for (EmbedSpan span : spans) {
      int a = text.getSpanStart(span), b = text.getSpanEnd(span);
      if (a < offset || a >= end || b <= a) continue;
      out.append(text.subSequence(offset, a)); out.append(InlineBody.token(span.id)); offset = b;
    }
    return out.append(text.subSequence(offset, end)).toString();
  }
  String savedContent() { String text = portable(0, length()); return inlineMode ? InlineBody.encode(text) : text; }
  int insertionOffset() { return portable(0, Math.max(0, getSelectionStart())).length(); }
  void insertAttachment(JSONObject item) throws Exception {
    inlineMode = true;
    files.put(item.getString("id"), item);
    int start = Math.max(0, Math.min(getSelectionStart(), getSelectionEnd())), end = Math.max(start, Math.max(getSelectionStart(), getSelectionEnd()));
    CharSequence inserted = render(InlineBody.token(item.getString("id")));
    getText().replace(start, end, inserted); setSelection(start + inserted.length());
  }
  @Override public boolean onTextContextMenuItem(int action) {
    if (action == android.R.id.copy || action == android.R.id.cut) {
      int start = Math.min(getSelectionStart(), getSelectionEnd()), end = Math.max(getSelectionStart(), getSelectionEnd());
      if (start < 0 || start == end) return super.onTextContextMenuItem(action);
      ClipboardManager clipboard = (ClipboardManager) getContext().getSystemService(Context.CLIPBOARD_SERVICE);
      clipboard.setPrimaryClip(ClipData.newPlainText("TwoNote", portable(start, end)));
      if (action == android.R.id.cut) getText().delete(start, end);
      return true;
    }
    if (action == android.R.id.paste || action == android.R.id.pasteAsPlainText) {
      ClipboardManager clipboard = (ClipboardManager) getContext().getSystemService(Context.CLIPBOARD_SERVICE);
      ClipData clip = clipboard.getPrimaryClip();
      if (clip != null && clip.getItemCount() > 0) {
        try {
          int start = Math.max(0, Math.min(getSelectionStart(), getSelectionEnd())), end = Math.max(start, Math.max(getSelectionStart(), getSelectionEnd()));
          getText().replace(start, end, render(clip.getItemAt(0).coerceToText(getContext()).toString()));
          return true;
        } catch (Exception ignored) { /* Fall back to ordinary text, preserving the reference. */ }
      }
    }
    return super.onTextContextMenuItem(action);
  }
}
