# ブログ工場 毎日の自動運転（Claude Code がこの手順で動く）

あなたはブログ工場（C:\blog-factory）の自動運転担当です。渋田さんへの確認は不要です。下の手順を上から順に、すべて自分で実行してください。
今回書く記事の本数: **{N} 本**（ただし `autopilot.py next` が「上限」と言ったら、そこで書くのをやめる）。

## 1. 準備

1. `python scripts/autopilot.py sync` … 審査待ちだった広告が承認されていれば台帳に取り込まれる（A8 のログインが切れていれば自動でスキップされる）
2. `python scripts/autopilot.py review` … 「整えてください」と出た案件があれば、`data/affiliates.json` のその案件を編集する
   - `label`（短いサービス名）、`catch`（30字前後の特徴。ad_copies に書いてあることだけ）、`keywords`（記事に出てきそうな語を5個前後）、
     `table`（`price` / `storage` / `days` / `feature`。各項目がそのジャンルで何を表すかは review の表示どおり。ad_copies に書いてある事実だけ。分からなければ空文字）を書き、`needs_review` を消す
   - 誇張（No.1、最安など）は ad_copies にあっても書かない

## 2. 記事を書く（{N} 回くり返す）

1. `python scripts/autopilot.py next` を実行し、ネタ・ジャンル・型・公開済み記事の一覧を読む
2. 「広告が足りません」と出たら、`python scripts/autopilot.py ads <genre>` を実行する（A8 で検索→提携申請→即時提携分を台帳へ。数分かかる）
3. 表示された「型」のファイルを読み、その見出し構成に沿って記事を書く
4. `content/ja/<slug>.md` に保存する（slug は内容に合う英小文字とハイフンの短い名前。既存と重ならないこと）
5. `python scripts/autopilot.py done "<ネタ>" <slug>` でネタに使用済みの印を付ける

### 記事のルール（必ず守る）

- フロントマターは次の形（`generated_by: autopilot` と `genre` は必須）:
  ```
  ---
  title: （32字前後。ネタのキーワードを自然に含める）
  date: （今日の日付 YYYY-MM-DD。公開予定時刻は publish.py が自動で付けて date も直すので、publish_at は書かない）
  description: （120字以内の要約）
  slug: （英小文字とハイフン）
  genre: （next で表示されたジャンルの id）
  generated_by: autopilot
  image_query: （写真検索用の英語2語。例: bedroom bed / healthy meal / cardboard box）
  image_queries: （本文中の写真用の英語。2つを | で区切る。例: laundry clothes | closet）
  ---
  ```
- トーンは丁寧な「です・ます」調。煽らない、急かさない、イケイケな調子にしない。読者の不安や手間に寄り添う
- **中身の薄い記事は書かない**。具体的な選び方・比較の観点・注意点・Q&A（3問以上）を必ず入れる。本文 3000〜4500 字
- **公開済み記事と同じ切り口・似たタイトルにしない**。ネタのキーワードに合わせて、読者・場面・悩みを絞った独自の切り口にする
- 各社の料金・日数・割引・ランキングなどの**具体的な数字や順位を本文に書かない**（比較表は台帳から自動で入る）。
  一般的な相場は「〜が目安です」「詳しくは公式サイトでご確認ください」と書く
- 「絶対」「必ず儲かる」「No.1」「最安」「業界初」などの断定・誇大表現は使わない
- 比較表・申込みボックスの差し込み口 `<!-- AFFILIATE_COMPARE -->`（比較の見出しの中）と `<!-- AFFILIATE_CTA -->`（まとめの最後）を、型のとおり1回ずつ書く
- 見た目の書き方: 各見出しで一番大事な一文を 0〜1 か所 `==マーカー==`、箇条書きは「- ラベル：説明」、
  コツは `> [!POINT]`、注意は `> [!WARN]`（記事全体で1〜2回まで）、よくある質問は「Q. 質問」「A. 答え」の2行
- 1段落は3文程度まで。PR表記・写真・申込みボタン・比較表は自動で入るので本文に書かない

## 3. 公開

1. `python scripts/publish.py -m "auto: <書いた記事のslugをカンマ区切り>"` … 公開予定時刻の割り振り（24時間以内のランダム）・写真の取得・ビルド・
   GitHub への送信・STATUS.md 更新まで自動。記事は予定時刻になると GitHub Actions がサイトに出す（今すぐは出ない）
2. 出力に「写真の用意をスキップ」「使える写真が見つからず」と出たら、その記事の `image_query` を別の一般的な英単語（1〜2語）に変えて
   `python scripts/images.py <slug>` → もう一度 `python scripts/publish.py -m "auto: 写真の補完"` を実行する

## 4. ネタの補充と記録

1. `python scripts/autopilot.py topics-check` … 「補充が必要」と出たジャンルがあれば、topics.txt のそのジャンルの見出しの下に
   `- [ ] キーワード` を10行追記する（既存のネタ・使用済みのネタと同じ・似た切り口は避ける。実際に検索されそうな2〜4語の組み合わせ）
2. `python scripts/autopilot.py status`
3. topics.txt や台帳を変更した場合は `python scripts/publish.py -m "auto: ネタ・台帳の更新"` で保存する
4. 最後に、今回やったこと（公開した記事のタイトル、A8 の作業、問題があればその内容）を3〜6行で報告して終わる

## やってはいけないこと

- 1日に `next` が許す本数を超えて記事を書くこと
- `.secrets/` の中身を表示・コミットすること、ログイン情報を扱うこと
- 既存の仕組み（scripts/ や templates/）を書き換えること（記事・topics.txt・台帳の編集だけを行う）
- A8 で、ジャンルと関係ない案件や求人・代理店募集の案件に申請すること
