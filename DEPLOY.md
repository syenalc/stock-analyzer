# Streamlit Cloud デプロイ手順

## 1. GitHub にリポジトリを作成

### 1-1. GitHub Web でリポジトリ作成

1. https://github.com/new を開く
2. リポジトリ名: `stock-analyzer`
3. **Private** にチェック（公開すると .env履歴漏洩リスクあり）
4. README/License 等は **何もチェックしない**
5. 「Create repository」

### 1-2. ローカルから push（PowerShell）

```powershell
cd C:\Users\shunp\stock-analyzer

# git 初期化（初回のみ）
git init
git branch -M main

# 念のため .env が無視されているか確認
git status
# → .env が表示されないこと（Untracked にも出ないこと）を確認

git add .
git commit -m "initial commit"
git remote add origin https://github.com/<your-username>/stock-analyzer.git
git push -u origin main
```

> ⚠️ `.env` や `.streamlit/secrets.toml` が push されていないか必ず確認

---

## 2. Streamlit Cloud にデプロイ

1. https://share.streamlit.io にアクセス
2. 「Continue with GitHub」でログイン
3. 「Create app」→「Deploy a public app from GitHub」
4. リポジトリ選択: `<your-username>/stock-analyzer`
5. Branch: `main`
6. Main file path: `dashboard/app.py`
7. App URL（任意のサブドメイン）を設定
8. 「Advanced settings」→ Python version: `3.11` 推奨
9. **Secrets** に `.streamlit/secrets.toml.example` の内容を実値で貼り付け
10. 「Deploy」

数分待つと `https://<your-app>.streamlit.app` が発行される。

---

## 3. スマホで「アプリ化」する

### iPhone (Safari)
1. 発行URLをSafariで開く
2. パスワードでログイン
3. 共有ボタン → 「ホーム画面に追加」
4. ホーム画面のアイコンタップで起動 ← アプリ風

### Android (Chrome)
1. 発行URLをChromeで開く
2. 右上メニュー → 「ホーム画面に追加」
3. ホーム画面のアイコンタップで起動

---

## 4. 後でコードを変更したとき

```powershell
git add .
git commit -m "update"
git push
```

push すると Streamlit Cloud が自動で再ビルド・再デプロイ（1〜2分）。

---

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `ModuleNotFoundError` | `requirements.txt` に漏れがないか確認 |
| Supabase 接続エラー | Streamlit Cloud の Secrets に `SUPABASE_URL` `SUPABASE_KEY` が入っているか |
| `.env` を push してしまった | リポジトリを削除→新規作成 + APIキー全部再発行 |
| スリープから起きない | 無料枠は7日未アクセスでスリープ。次アクセスで30秒程度待つ |
| パスワード忘れ | Streamlit Cloud の Secrets で `APP_PASSWORD` を書き換え |
