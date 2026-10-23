package com.twonote.android;

import android.graphics.*;
import com.twonote.core.NoteBlock;
import org.json.*;
import java.util.Locale;

/** Small native preview of immutable editable table/chart data. */
final class BlockPreview {
  static Bitmap render(JSONObject value) throws Exception {
    NoteBlock.validate(value);
    JSONArray rows = value.getJSONArray("rows");
    boolean table = value.getString("kind").equals("table");
    int width = table ? Math.max(360, Math.min(1200, rows.getJSONArray(0).length() * 190)) : 900;
    int height = table ? 56 + rows.length() * 42 : 490;
    Bitmap image = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
    Canvas canvas = new Canvas(image); canvas.drawColor(0xff122136);
    Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG); paint.setTextSize(22); paint.setColor(0xffe5edf7);
    canvas.drawText(clip(paint, value.getString("title"), width - 24), 12, 33, paint);
    if (table) {
      float cell = (float) width / rows.getJSONArray(0).length();
      for (int r = 0; r < rows.length(); r++) {
        JSONArray row = rows.getJSONArray(r);
        for (int c = 0; c < row.length(); c++) {
          float x = c * cell, y = 52 + r * 42;
          paint.setStyle(Paint.Style.STROKE); paint.setColor(0xff53738f);
          canvas.drawRect(x, y, x + cell, y + 42, paint);
          paint.setStyle(Paint.Style.FILL); paint.setColor(0xffe5edf7);
          canvas.drawText(clip(paint, row.getString(c).replace('\n', ' '), cell - 16), x + 8, y + 29, paint);
        }
      }
    } else {
      double low = 0, high = 0; double[] numbers = new double[rows.length()];
      for (int i = 0; i < numbers.length; i++) { numbers[i] = Double.parseDouble(rows.getJSONArray(i).getString(1)); low = Math.min(low, numbers[i]); high = Math.max(high, numbers[i]); }
      if (high == low) high = low + 1;
      for (int i = 0; i < 5; i++) {
        double n = low + (high - low) * i / 4;
        float y = y(n, low, high);
        paint.setColor(0xff365069); canvas.drawLine(110, y, 875, y, paint);
        paint.setColor(0xffd5e1ef); canvas.drawText(String.format(Locale.ROOT, "%.3g", n), 4, y + 8, paint);
      }
      float slot = 765f / numbers.length, previousX = 0, previousY = 0;
      for (int i = 0; i < numbers.length; i++) {
        float x = 110 + slot * (i + .5f), y = y(numbers[i], low, high), zero = y(0, low, high);
        paint.setColor(0xff6dd3cf); paint.setStrokeWidth(3);
        if (value.getString("kind").equals("bar")) canvas.drawRect(x - slot * .3f, Math.min(y, zero), x + slot * .3f, Math.max(Math.min(y, zero) + 1, Math.max(y, zero)), paint);
        else { if (i > 0) canvas.drawLine(previousX, previousY, x, y, paint); canvas.drawCircle(x, y, 4, paint); }
        previousX = x; previousY = y;
        paint.setColor(0xffe5edf7);
        canvas.drawText(clip(paint, rows.getJSONArray(i).getString(0), slot - 2), x - slot / 2, 414, paint);
      }
    }
    return image;
  }
  private static float y(double n, double low, double high) { return (float) (380 - (n - low) / (high - low) * 315); }
  private static String clip(Paint paint, String value, float width) {
    if (paint.measureText(value) <= width) return value;
    int length = paint.breakText(value, true, Math.max(0, width - paint.measureText("…")), null);
    if (length > 0 && length < value.length() && Character.isHighSurrogate(value.charAt(length - 1))) length--;
    return value.substring(0, length) + "…";
  }
}
