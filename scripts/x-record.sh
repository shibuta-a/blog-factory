#!/usr/bin/env bash
# GitHub Actions の投稿係が data/x.json（と STATUS.md の知らせ）を GitHub に残す（x-post.yml と x-actions.py --loop から呼ぶ）。
# data/x.json を書くのはこの投稿係だけ（同時には動かない）。送れないときは最新に合わせてから x.json を載せ直す
git config user.name "x-post (GitHub Actions)"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
[ -f data/x.json ] || exit 0
git add data/x.json STATUS.md
if git diff --cached --quiet && [ -z "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then exit 0; fi
cp data/x.json "$RUNNER_TEMP/x.json"
for i in 1 2 3 4 5; do
  git fetch -q origin main
  git reset -q --hard origin/main
  cp "$RUNNER_TEMP/x.json" data/x.json
  python3 scripts/x-actions.py --status-only
  git add data/x.json STATUS.md
  git diff --cached --quiet && exit 0
  git commit -q -m "x: 投稿の記録 [CI Skip]"
  git push -q origin HEAD:main && exit 0
  sleep 5
done
exit 1
