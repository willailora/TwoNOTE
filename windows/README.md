# Windows版（Qt）

UIはPySide6 6.10.2のQt Widgetsです。Webサーバーやブラウザは起動せず、暗号化されたローカルファイルを直接読み書きします。

## 実行

```powershell
.\dist\TwoNote.exe --data-dir ".\data"
```

EXEはPython不要です。既存のショートカット向けの `TwoNoteServer.exe` も同じネイティブアプリです。旧引数 `--host`, `--port`, `--https` は互換のため受け取りますが、サーバーを常時公開しません。ブラウザURLはありません。

起動直後に旧保管庫があれば「移行して開く」が表示されます。旧アプリを閉じてからパスワードを入力してください。元ファイルを破壊して暗号化する方式ではなく、別保管庫を検証してから移行します。

通常の編集はオフラインです。「スマホと同期」を押した間だけLANにTLS待受を開始し、完了後に閉じます。`--sync-port 8001` でポートを指定できます。

## ビルド

```powershell
.\windows\build_windows.ps1 -InstallDependencies
```

依存をプロジェクト内 `.tools/python-packages` に取得し、アイコンとライセンスを含めてパッケージします。出力は `dist/TwoNote.exe` と `dist/TwoNote-Windows.zip` です。Windowsのコード署名証明書は設定していないため、EXEは未署名です。

## 単体の自己診断

```powershell
.\dist\TwoNote.exe --self-test-report build\qa\packaged-smoke.json
```

このモードは専用の一時保管庫・合成ノートのみで、日本語編集、画像保存、暗号化バックアップの往復、再起動後の読み込みを確認して終了します。実データの保管庫指定は使用せず、デスクトップにウィンドウを表示しません。
