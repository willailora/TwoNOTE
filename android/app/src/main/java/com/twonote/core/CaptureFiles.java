package com.twonote.core;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Camera output only: never accepts arbitrary paths or traverses other app files. */
public final class CaptureFiles {
  private CaptureFiles() {}

  public static boolean validName(String name) {
    return name != null && name.matches("capture-[0-9a-f]{32}\\.jpg");
  }

  public static File resolve(File directory, String name) throws IOException {
    if (!validName(name)) throw new IOException("撮影ファイルの識別情報が不正です");
    File file = new File(directory, name);
    if (!file.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile())
        || Files.isSymbolicLink(file.toPath())) throw new IOException("撮影ファイルの場所が不正です");
    return file;
  }

  public static File create(File directory) throws IOException {
    if (!directory.isDirectory() && !directory.mkdirs()) throw new IOException("撮影用の空き領域を作成できません");
    File file = resolve(directory, "capture-" + UUID.randomUUID().toString().replace("-", "") + ".jpg");
    if (!file.createNewFile()) throw new IOException("撮影用ファイルを作成できません");
    return file;
  }

  public static void validatePhoto(File directory, String name) throws IOException {
    File file = resolve(directory, name);
    if (!file.isFile() || file.length() == 0) throw new IOException("撮影結果がありません。もう一度撮影してください");
    if (file.length() > LocalVault.MAX_BLOB) throw new IOException("写真は64MBまでです");
  }

  public static List<String> abandoned(File directory, String keep) throws IOException {
    List<String> names = new ArrayList<>();
    if (!directory.exists()) return names;
    File[] files = directory.listFiles();
    if (files == null) throw new IOException("撮影用の一時領域を確認できません");
    for (File file : files) {
      String name = file.getName();
      if (validName(name) && !name.equals(keep) && resolve(directory, name).isFile()) names.add(name);
    }
    return names;
  }

  public static void delete(File directory, String name) throws IOException {
    // Only files issued by create(), never a directory tree or a gallery image.
    File file = resolve(directory, name);
    if (file.exists() && (!file.isFile() || !file.delete())) throw new IOException("撮影用の一時ファイルを削除できません");
  }
}
