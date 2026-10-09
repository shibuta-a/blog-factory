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
| `scripts/auto-generate.py` | Claude APIで記事を自動生成→広告挿入→公開（「自動投稿オン」まで動かさない） |
| `scripts/affiliate.py` | 記事の内容に合う広告を台帳から選んで差し込む（build.py が使う） |
| `scripts/fetch-affiliates.py` | A8.net の提携申請・広告リンク取得をブラウザ自動操作で行い、台帳を更新 |
| `scripts/a8-login.py` | A8.net にログインした Chrome を用意する |
| `data/affiliates.json` | A8.net 提携中の案件と広告リンクの台帳 |
| `templates/kata/` | 記事の型（クリーニング比較記事） |
| `.secrets/` | ログイン情報・ブラウザのログイン状態・APIキー（**GitHubに上げない**。.gitignore 済み） |
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

## A8.net の広告（全自動）

### 全体の流れ
```
A8.net（提携・広告リンク取得）  ──scripts/fetch-affiliates.py──>  data/affiliates.json（台帳）
                                                                      ↓ ビルド時に自動
記事 content/ja/*.md  ──scripts/build.py（scripts/affiliate.py）──>  記事の内容に合う広告が入ったページ
```

### 台帳 `data/affiliates.json`
- A8.net で**提携中**の案件と広告リンクの一覧（1案件＝1項目）。今は**宅配・布団クリーニング 9件**
- `url`・`report_tag`（成果計測用の1px画像）・`source_code` などはスクリプトが自動で更新する
- `label`（表示名）・`catch`（ひとこと説明）・`keywords`（記事とのマッチ用）・`priority`（同点時の優先度）・
  `cta`（ボタン文言。省略時「◯◯の公式サイトを見る」）・`enabled: false`（出さない）は手で調整してよい。更新しても上書きされない
- 広告リンク内の `a8mat=…` はアフィリエイトID（公開されることが前提のもの）。ログイン情報は含まれない

### 提携・リンク取得の更新（Claude Code が実行する。渋田さんの作業はA8.netへのログインだけ）
```
python scripts/a8-login.py                     # ログイン済みChromeを用意（開いていれば何もしない）
python scripts/fetch-affiliates.py search      # 候補を検索 → data/a8-candidates.json（Git管理外）
python scripts/fetch-affiliates.py apply -n 20 # 未提携の候補に提携申請（即時提携を優先）
python scripts/fetch-affiliates.py apply --ids s00000013507003   # 指定したプログラムだけ申請
python scripts/fetch-affiliates.py links       # 提携中の広告リンクを取得して台帳を更新
python scripts/publish.py                      # 台帳の変更をサイトに反映
```
- A8.net は**ブラウザを閉じるとログインが消える**。`a8-login.py` が開いた Chrome でログインしたら、作業が終わるまで閉じない
- 審査ありの案件は、承認された後に `links` を実行すれば台帳に入る（2026-10-09 時点で11件が審査待ち）
- 別ジャンル（宅配食・通信講座など）に広げるときは、`fetch-affiliates.py` 冒頭の `KEYWORDS` / `RELEVANT` を変える
- 広告を載せた記事を公開したら、A8.net の「広告掲載URL管理」に記事URLを提出する（A8.net のルール）

### 記事への自動挿入（`scripts/affiliate.py`）
- ビルドのたびに、記事のタイトル・本文の言葉と台帳の `keywords` を突き合わせ、合う案件を**最大3件**選ぶ（タイトルは3倍の重み）
- `genre_keywords`（今は「クリーニング」）が記事に1回も出てこなければ入れない → コーヒー記事などには出ない
- 入る場所:
  - 本文に `<!-- AFFILIATE_COMPARE -->` の行 → **比較カード**（選ばれた2〜3件）
  - 本文に `<!-- AFFILIATE_CTA -->` の行 → 一番合う1件の**申込みボタン**
  - どちらも無い記事 → 記事末尾に自動で入る（タイトルに「比較」「おすすめ」等があれば比較カード、なければボタン）
- 広告が入った記事には、タイトル下に「※本記事にはプロモーション（広告）が含まれています。」を**自動表示**
  （景品表示法のステマ規制と A8.net のルールで、広告であることの表示が必要なため）
- 記事ごとの指定（フロントマター）: `affiliates: none`（入れない）／`affiliates: しももとクリーニング, フレスコ`（案件を指定）
- 見た目は `templates/style.css` の `.aff-*`

## 記事量産の型（クリーニング比較記事）

`templates/kata/cleaning-compare.md` が型。構成は
**導入（悩みへの共感）→ 選び方4つのポイント（料金・保管・日数・対応品目）→ おすすめ比較 → よくある質問 → まとめ**。

- MAF社長が書いた記事Markdownも、この型の見出し構成で書いて `<!-- AFFILIATE_COMPARE -->` と `<!-- AFFILIATE_CTA -->` の2行を入れておけば、
  「この記事を公開して」で広告入りのページになる（2行が無くても末尾に自動で入る）
- サービスごとの料金・日数などの数字は、公式サイトで確認できたものだけ書く

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

前提：`.secrets/anthropic.env`（または従来の `.env`）に `ANTHROPIC_API_KEY=…` を書き、`topics.txt` にネタを並べておく（`scripts/auto-generate.py` 冒頭参照）。

- ネタにクリーニング・布団・家事代行などの言葉があれば、**クリーニング比較の型**で書かせ、広告の差し込み口を必ず入れる。
  生成 → 保存 → ビルド（広告を自動挿入）→ 公開 まで止まらずに回る
- 生成だけ試す（公開しない）：`python scripts/auto-generate.py --dry-run`
- APIを呼ばずにプロンプトだけ確認：`python scripts/auto-generate.py --show-prompt "布団クリーニング おすすめ 比較"`
- 渋田さんが「自動投稿オンにして」と言ったら、Claude Code が APIキーの用意を確認し、下のコマンドで毎日の実行を登録する

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

## ログイン情報・キーの保存場所（GitHubには上げない）

| もの | 場所 |
|---|---|
| A8.net のログイン状態（ブラウザのデータ） | `.secrets/a8-browser-profile/` ※ID・パスワードそのものは保存していない |
| Claude API キー（自動投稿用） | `.secrets/anthropic.env`（従来の `.env` も可） |
| 作業時のスクリーンショット | `.secrets/shots/` |

`.secrets/`・`.env`・`data/a8-candidates.json` は `.gitignore` で除外済み。公開リポジトリに出るのは台帳 `data/affiliates.json`（公開前提のアフィリエイトリンクのみ）。
