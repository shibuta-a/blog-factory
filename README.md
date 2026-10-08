# 全自動ブログ工場（blog-factory）

## これは何か

Markdown（記事の元ネタ）を置くだけで、HTMLの記事ページに変換して世界に公開する仕組み。

```
content/ja/記事.md  →  scripts/build.py  →  public/*.html
                                              ↓ git push（publish が自動でやる）
                                           GitHub（blog-factory リポジトリ）
                                              ↓ 届いた瞬間に自動
                                           Cloudflare Pages が公開 → https://blog-factory-cf7.pages.dev
```

- サーバー管理なし・維持費0円・脆弱性対応なし（静的サイト）
- 必要なのは Python と git だけ（外部ライブラリ不要）

## フォルダ構成

| 場所 | 中身 |
|---|---|
| `content/ja/` | 日本語記事の元ネタ（Markdown 1ファイル＝記事1本） |
| `content/en/` `content/zh/` | 英語版・中国語版（作れば自動で出力される） |
| `templates/` | デザインの雛形（`article.html` 記事 / `index.html` 一覧 / `style.css`） |
| `slots/` | **収益タグの差し込み口**（広告・アフィリのコードを貼る場所） |
| `static/` | 画像など、そのまま公開したいファイル |
| `public/` | 自動生成された公開用HTML（手で触らない） |
| `scripts/build.py` | ビルド（Markdown → HTML） |
| `scripts/publish.py` | 公開（保存 → ビルド → git push） |
| `scripts/auto-generate.py` | 将来用：Claude APIで記事を自動生成して公開 |
| `site.json` | サイト名・言語ごとの表示文言 |

## 記事を1本 publish する手順

### 渋田さんの場合
Claude Code に「この記事を公開して」と Markdown を貼るだけ。保存〜公開は Claude Code がやる。

### Claude Code がやること
1. 渡された Markdown を `content/ja/<英語のslug>.md` に保存
2. `python scripts/publish.py`（または `publish.bat` をダブルクリック）を実行
3. 1〜2分後、公開URLに記事が出る

### 記事Markdownの書き方
```markdown
---
title: 記事タイトル
date: 2026-10-08
description: 検索結果に出る要約（120字以内）
slug: url-no-namae
---

導入文。

## 見出し1（目次に自動で載る）
本文…

### 小見出し（目次に入れ子で載る）
本文…
```

- `slug`：URLの名前（英小文字・数字・ハイフン）。`https://blog-factory-cf7.pages.dev/slug` になる
- `date`：**未来の日付にすると予約投稿**（その日以降のビルドで初めて公開される）
- `draft: true` を付けると公開しない（下書き）
- 見出し・箇条書き・番号リスト・表・引用・リンク・画像・コード・太字が使える
- 記事中にHTML（`<div>…</div>` やアフィリのバナータグ）を直接書いてもそのまま出る
- 画像は `static/images/` に置いて `![説明](images/xxx.jpg)` で参照

## 収益タグ（広告・アフィリ）を入れる場所

`slots/` のファイルにコードを貼って publish すれば、**全記事に一括で反映**される。

| ファイル | 出る場所 | テンプレ上の目印 |
|---|---|---|
| `slots/head.html` | 全ページの `<head>` 内（AdSense自動広告・アクセス解析タグ用） | `<!-- HEAD_SLOT -->` |
| `slots/ad_top.html` | 記事タイトルの直下（一覧ページ上部にも） | `<!-- AD_SLOT_TOP -->` |
| `slots/ad_middle.html` | 本文の真ん中（中間の見出しの直前に自動挿入） | `<!-- AD_SLOT_MIDDLE -->` |
| `slots/affiliate.html` | 記事の末尾 | `<!-- AFFILIATE_SLOT -->` |

ファイル内の `<!-- 説明 -->` コメントの下に貼るだけ。空のままなら何も表示されない。

## 多言語版を足す方法

1. `content/en/`（英語）や `content/zh/`（中国語）フォルダに記事 Markdown を置く
   - 公開コマンド：`python scripts/publish.py article.md --lang en`
2. ビルドすると自動で `https://blog-factory-cf7.pages.dev/en/` に英語版トップと記事が出る。
   `<html lang="en">` も自動で切り替わり、トップに言語切替リンクが出る
3. 表示文言（「目次」「公開日」など）は `site.json` の `languages` に言語ごとに定義済み（ja / en / zh）
4. 新しい言語（例：韓国語 ko）を足すときは `site.json` に `"ko": {…}` を追加して `content/ko/` を作るだけ

同じ記事を翻訳するときは、翻訳版も **同じ slug** にしておくと URL が `/slug` と `/en/slug` で揃う。

## 毎日自動投稿をオンにする方法（Windows タスクスケジューラ）

※ 現在は**オフ**。渋田さんが「自動投稿オンにして」と言ったら Claude Code が設定する。

前提：`.env` に `ANTHROPIC_API_KEY=…` を書き、`topics.txt` にネタを並べておく（`scripts/auto-generate.py` 冒頭参照）。

### Claude Code が実行するコマンド（毎日朝7時に1本）
```powershell
$action  = New-ScheduledTaskAction -Execute "C:\blog-factory\auto-generate.bat" -Argument "-n 1" -WorkingDirectory "C:\blog-factory"
$trigger = New-ScheduledTaskTrigger -Daily -At 7:00
$setting = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
Register-ScheduledTask -TaskName "BlogFactory-AutoPost" -Action $action -Trigger $trigger -Settings $setting -Description "ブログ工場 毎日自動投稿"
```

- 確認：`Get-ScheduledTask -TaskName BlogFactory-AutoPost`
- 今すぐ1回試す：`Start-ScheduledTask -TaskName BlogFactory-AutoPost`
- オフにする：`Unregister-ScheduledTask -TaskName BlogFactory-AutoPost -Confirm:$false`
- ログ：`logs/auto-generate.log`
- PCの電源が切れている時刻は実行されない（`StartWhenAvailable` で次回起動時に実行）

### 手で設定する場合（参考）
タスクスケジューラ → 基本タスクの作成 → 名前「BlogFactory-AutoPost」→ 毎日 → 7:00 → プログラムの開始 →
プログラム `C:\blog-factory\auto-generate.bat`、引数 `-n 1`、開始 `C:\blog-factory` → 完了。

## Cloudflare Pages の設定値（記録）

| 項目 | 値 |
|---|---|
| リポジトリ | `blog-factory` |
| 本番ブランチ | `main` |
| フレームワーク プリセット | なし（None） |
| ビルドコマンド | `python3 scripts/build.py` |
| ビルド出力ディレクトリ | `public` |
| 公開URL | https://blog-factory-cf7.pages.dev |

`public/` もリポジトリに入れてあるので、万一 Cloudflare 側のビルドが失敗したらビルドコマンドを空欄にしても公開できる。
