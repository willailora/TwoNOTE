package com.twonote.core;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;
import java.util.*;
import javax.net.ssl.*;
import org.json.JSONObject;

/** Every network operation is explicit; all note contents remain ciphertext. */
public final class SyncClient {
  public interface Progress {
    void message(String message);
  }

  public static JSONObject parsePairing(String text) throws Exception {
    URI uri = new URI(text.trim());
    if (!"twonote".equals(uri.getScheme()) || !"pair".equals(uri.getHost()))
      throw new IllegalArgumentException("TwoNoteのペアリングコードではありません");
    Map<String, String> args = new HashMap<>();
    for (String field : uri.getRawQuery().split("&")) {
      String[] pair = field.split("=", 2);
      args.put(URLDecoder.decode(pair[0], "UTF-8"), URLDecoder.decode(pair[1], "UTF-8"));
    }
    int port = Integer.parseInt(args.get("port"));
    if (!"2".equals(args.get("v"))
        || port < 1
        || port > 65535
        || !args.get("pin").matches("[0-9a-f]{64}")
        || !args.get("vault").matches("[0-9a-f]{32}")
        || args.get("secret").length() < 32) throw new IllegalArgumentException("コードが不正です");
    return new JSONObject()
        .put("host", args.get("host"))
        .put("port", port)
        .put("pin", args.get("pin"))
        .put("vault_id", args.get("vault"))
        .put("secret", args.get("secret"));
  }

  public static byte[] request(
      JSONObject peer, String method, String path, byte[] body, String token) throws Exception {
    String pin = peer.getString("pin");
    if (!pin.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("証明書ピンが不正です");
    String host = peer.getString("host");
    for (InetAddress address : InetAddress.getAllByName(host))
      if (!address.isSiteLocalAddress()
          && !address.isLoopbackAddress()
          && !address.isLinkLocalAddress())
        throw new IllegalArgumentException("同じLAN内のPCを指定してください");
    X509TrustManager trust =
        new X509TrustManager() {
          public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
          }

          public void checkClientTrusted(X509Certificate[] chain, String type)
              throws java.security.cert.CertificateException {
            throw new java.security.cert.CertificateException("client");
          }

          public void checkServerTrusted(X509Certificate[] chain, String type)
              throws java.security.cert.CertificateException {
            try {
              if (chain.length == 0
                  || !MessageDigest.isEqual(
                      LocalVault.utf8(LocalVault.hash(chain[0].getEncoded())),
                      LocalVault.utf8(pin)))
                throw new java.security.cert.CertificateException("PCの証明書が変わりました。再ペアリングしてください");
            } catch (java.security.cert.CertificateException e) {
              throw e;
            } catch (Exception e) {
              throw new java.security.cert.CertificateException(e);
            }
          }
        };
    SSLContext ssl = SSLContext.getInstance("TLS");
    ssl.init(null, new TrustManager[] {trust}, null);
    URL url = new URI("https", null, host, peer.getInt("port"), path, null, null).toURL();
    HttpsURLConnection connection = (HttpsURLConnection) url.openConnection();
    connection.setSSLSocketFactory(ssl.getSocketFactory());
    // Exact certificate equality above is the peer identity; CA/hostname trust
    // is deliberately not substituted for that QR-established identity.
    connection.setHostnameVerifier(
        (name, session) -> {
          try {
            return LocalVault.hash(session.getPeerCertificates()[0].getEncoded()).equals(pin);
          } catch (Exception e) {
            return false;
          }
        });
    connection.setInstanceFollowRedirects(false);
    connection.setConnectTimeout(10000);
    connection.setReadTimeout(30000);
    connection.setUseCaches(false);
    connection.setRequestMethod(method);
    connection.setRequestProperty(
        "Authorization", "Bearer " + (token != null ? token : peer.getString("token")));
    try {
      if (body != null) {
        connection.setDoOutput(true);
        connection.setFixedLengthStreamingMode(body.length);
        connection.setRequestProperty("Content-Type", "application/octet-stream");
        try (OutputStream output = connection.getOutputStream()) {
          output.write(body);
        }
      }
      int status = connection.getResponseCode();
      if (status != 200)
        throw new IOException("同期できません (HTTP " + status + ")。PCの同期待受と端末登録を確認してください");
      try (InputStream input = connection.getInputStream();
          ByteArrayOutputStream out = new ByteArrayOutputStream()) {
        byte[] buffer = new byte[65536];
        int count;
        while ((count = input.read(buffer)) != -1) {
          if (out.size() + count > LocalVault.MAX_BLOB + 28) throw new IOException("同期データが大きすぎます");
          out.write(buffer, 0, count);
        }
        return out.toByteArray();
      }
    } finally {
      connection.disconnect();
    }
  }

  public static JSONObject json(
      JSONObject peer, String method, String path, JSONObject body, String token) throws Exception {
    return new JSONObject(
        new String(
            request(
                peer, method, path, body == null ? null : LocalVault.utf8(body.toString()), token),
            StandardCharsets.UTF_8));
  }

  public static JSONObject pair(JSONObject peer, String deviceId) throws Exception {
    return json(
        peer,
        "POST",
        "/v2/pair",
        new JSONObject().put("device_id", deviceId).put("label", "Android"),
        peer.getString("secret"));
  }

  public static JSONObject exchange(
      LocalVault vault, JSONObject peer, boolean finish, Progress progress) throws Exception {
    JSONObject remote = json(peer, "GET", "/v2/index", null, null);
    if (!vault.vaultId().equals(remote.getString("vault_id")))
      throw new IllegalArgumentException("保管庫が違います");
    JSONObject local = vault.inventory();
    int uploaded = 0, downloaded = 0;
    for (String kind : new String[] {"blobs", "revisions"}) {
      List<String> remoteList = LocalVault.strings(remote.getJSONArray(kind));
      if (remoteList.size() > LocalVault.MAX_RECORDS)
        throw new IllegalArgumentException("同期一覧が大きすぎます");
      Set<String> remoteIds = new TreeSet<>(remoteList),
          localIds = new TreeSet<>(LocalVault.strings(local.getJSONArray(kind)));
      for (String id : localIds)
        if (!remoteIds.contains(id)) {
          progress.message("変更を送信中…");
          request(peer, "PUT", "/v2/objects/" + kind + "/" + id, vault.readObject(kind, id), null);
          uploaded++;
        }
      for (String id : remoteIds)
        if (!localIds.contains(id)) {
          progress.message("変更を受信中…");
          vault.objectPath(kind, id);
          byte[] raw = request(peer, "GET", "/v2/objects/" + kind + "/" + id, null, null);
          vault.stageObject(kind, id, raw);
          downloaded++;
        }
    }
    json(
        peer,
        "POST",
        "/v2/commit",
        new JSONObject()
            .put("vault_id", vault.vaultId())
            .put("revisions", local.getJSONArray("revisions")),
        null);
    vault.commit(LocalVault.strings(remote.getJSONArray("revisions")));
    if (finish) json(peer, "POST", "/v2/done", new JSONObject(), null);
    return new JSONObject().put("uploaded", uploaded).put("downloaded", downloaded);
  }
}
