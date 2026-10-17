// 見張り番（Cloudflare Workers・maf-guard）。PC の外にいるので、PC が起動しなかった日でも気づける
//
//   合図（ハートビート）: 各仕組みが終わるたびに POST /beat（合言葉 GUARD_TOKEN 付き）で「終わった・結果」を受け取り、KV に残す
//     boot-start / blog / note / xplan / boot-end … PC② の朝の実行（scripts/boot-run.py → scripts/guard.py）
//     xpost                                       … X の各投稿（Cloudflare の maf-x-poster がサービス連携で送る）
//     pc1-mail                                    … PC① の営業メール（今は待たない。KV の config.expect で切り替え）
//   見張り: 毎朝 7:30（日本時間）にその日の合図がそろっているか確かめ、1通知らせる
//     そろっていれば「【OK】今朝のまとめ」、1つでも足りない・失敗があれば「【要対応】…」（要対応を先頭に、まとめも後ろに付ける）
//     要対応を送った日に、あとから朝の実行が終わったら「【回復】」を送る
//   X の投稿に失敗したら、その場で知らせる（同じ投稿・同じ状態では1回だけ）
//
// 知らせ方: GitHub の非公開リポジトリ shibuta-a/blog-factory-guard に repository_dispatch（guard-notify）を送る
//   → そのリポジトリの Actions が Issue を作り @shibuta-a に割り当てる → GitHub の通知メールが登録メール（Gmail）に届く
//   （自分のアカウントで作った Issue は自分に通知されないので、ボットに作らせる）
// 秘密の設定（scripts/guard.py deploy が入れる）: GUARD_TOKEN（受け口の合言葉）/ GH_TOKEN（dispatch を送る GitHub のトークン）/
//   BACKUP_KEY（.secrets のバックアップを暗号にする鍵。Cloudflare にだけある。PC には置かない）
//
//   POST /beat     {name, date?, ok, lines?, error?}        合図
//   POST /notify   {subject, body}                          すぐに知らせる（PC からの通知・週の成績表）
//   POST /check?date=YYYY-MM-DD&test=1&send=0              朝の見張りを今すぐ（test=1 は件名に【テスト】・send=0 は送らず中身を返す）
//   POST /backup   {files: {名前: base64}}                   .secrets のバックアップ（ここで AES-256-GCM で暗号にして R2、R2 が無ければ KV に。最新＋日付ごとに4つ）
//   GET  /backup/list                                       バックアップの一覧
//   POST /backup/restore?key=backup:latest                  暗号を解いて {files} を返す（復元用）
//   POST /config   {expect: {pc1-mail: true, ...}}          待つ合図の切り替え
//   GET  /status                                            今日の合図・設定・最近の通知（合言葉が要る）
const NOTIFY_REPO = "shibuta-a/blog-factory-guard";

const EXPECT_DEFAULT = { "boot-start": true, blog: true, note: true, xplan: true, "boot-end": true, "pc1-mail": false };
const DAY_TTL = 60 * 60 * 24 * 45;

const LABEL = {
  "boot-start": "朝の実行の開始",
  blog: "ブログの朝の実行",
  note: "note の投稿とコメント返信",
  xplan: "X の予定表の作成",
  "boot-end": "朝の実行の最後（記録・シャットダウン）",
  "pc1-mail": "営業メールの自動送信（PC①）",
};
// 合図が来ていないときの「何が・どうなっていそうか」
const MISSING = {
  "boot-start": "今朝のブログが動いていません。PCが起動していない可能性があります（BIOS の時刻起動・電源・停電・自動ログオン）",
  blog: "ブログの記事づくりが終わっていません。記事の段階で止まっているか、まだ動いている可能性があります（logs/boot-run.log・logs/autopilot.log）",
  note: "note の段階が終わっていません。note の投稿かコメント返信で止まっている可能性があります（logs/note-run.log）",
  xplan: "X の予定表が作られていません。今日の X は投稿されません（logs/x-plan.log）",
  "boot-end": "朝の実行が最後まで終わっていません。途中で止まったか、PC が点いたままの可能性があります",
  "pc1-mail": "営業メールの自動送信（PC①）の合図がありません。PC① が起動していないか、送信が止まっている可能性があります",
};

function jst(d = new Date()) {
  return new Date(d.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 16).replace("T", " ");
}
const today = () => jst().slice(0, 10);
function addDays(day, n) {
  return new Date(Date.parse(day + "T00:00:00Z") + n * 86400000).toISOString().slice(0, 10);
}
function md(day) {
  return `${Number(day.slice(5, 7))}/${Number(day.slice(8, 10))}`;
}

async function kvJson(env, key, dflt) {
  return (await env.GUARD.get(key, "json")) ?? dflt;
}
async function addLog(env, msg) {
  const log = await kvJson(env, "log", []);
  log.push(`${jst()} ${msg}`);
  await env.GUARD.put("log", JSON.stringify(log.slice(-100)));
}
function authed(request, env) {
  return env.GUARD_TOKEN && request.headers.get("Authorization") === `Bearer ${env.GUARD_TOKEN}`;
}

// ---------------------------------------------------------------------------
// 知らせ（GitHub の Issue → 通知メール）
// ---------------------------------------------------------------------------
function labelOf(subject) {
  if (subject.includes("【テスト】")) return "テスト";
  const m = subject.match(/【(OK|要対応|回復|週報)】/);
  return m ? m[1].replace("OK", "ok") : "info";
}

async function sendIssue(env, subject, body) {
  if (!env.GH_TOKEN) throw new Error("GH_TOKEN が未設定です");
  const r = await fetch(`https://api.github.com/repos/${NOTIFY_REPO}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "User-Agent": "maf-guard",
               "Content-Type": "application/json" },
    body: JSON.stringify({ event_type: "guard-notify", client_payload: { title: subject, body: body.slice(0, 60000), label: labelOf(subject) } }),
  });
  if (r.status !== 204) throw new Error(`GitHub ${r.status}: ${(await r.text()).slice(0, 200)}`);
}

async function notify(env, subject, body) {
  try {
    await sendIssue(env, subject, body);
    await addLog(env, `送信 ${subject}`);
    await env.GUARD.put("mail_last", JSON.stringify({ ok: true, at: jst(), subject }));
    return { ok: true };
  } catch (e) {
    const err = String(e).slice(0, 300);
    await addLog(env, `送信失敗 ${subject}: ${err}`);
    await env.GUARD.put("mail_last", JSON.stringify({ ok: false, at: jst(), subject, error: err }));
    return { ok: false, error: err };
  }
}

// ---------------------------------------------------------------------------
// バックアップ（鍵 BACKUP_KEY は Cloudflare の秘密の設定にだけある）
// ---------------------------------------------------------------------------
function b64ToBytes(b64) {
  const bin = atob(b64);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}
function bytesToB64(u8) {
  let bin = "";
  for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode(...u8.subarray(i, i + 0x8000));
  return btoa(bin);
}
async function backupKey(env) {
  if (!env.BACKUP_KEY) throw new Error("BACKUP_KEY が未設定です");
  return crypto.subtle.importKey("raw", b64ToBytes(env.BACKUP_KEY), "AES-GCM", false, ["encrypt", "decrypt"]);
}
async function storePut(env, key, text) {
  if (env.BACKUP_R2) await env.BACKUP_R2.put(key, text);
  else await env.GUARD.put(key, text);
}
async function storeGet(env, key) {
  if (env.BACKUP_R2) { const o = await env.BACKUP_R2.get(key); return o ? o.text() : null; }
  return env.GUARD.get(key);
}
async function storeList(env) {
  if (env.BACKUP_R2) return (await env.BACKUP_R2.list({ prefix: "backup:" })).objects.map((o) => o.key);
  return (await env.GUARD.list({ prefix: "backup:" })).keys.map((k) => k.name);
}
async function storeDelete(env, key) {
  if (env.BACKUP_R2) await env.BACKUP_R2.delete(key);
  else await env.GUARD.delete(key);
}
async function backupSave(env, payload) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const plain = new TextEncoder().encode(JSON.stringify(payload));
  const enc = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await backupKey(env), plain));
  const n = Object.keys(payload.files || {}).length;
  const text = JSON.stringify({ v: 2, created: jst(), files: Object.keys(payload.files || {}), iv: bytesToB64(iv), data: bytesToB64(enc) });
  await storePut(env, "backup:latest", text);
  await storePut(env, `backup:${today()}`, text);
  const dated = (await storeList(env)).filter((k) => /^backup:\d/.test(k)).sort();
  for (const k of dated.slice(0, Math.max(0, dated.length - 4))) await storeDelete(env, k);
  await addLog(env, `バックアップを保存（${n} ファイル・${env.BACKUP_R2 ? "R2" : "KV"}）`);
  return { ok: true, store: env.BACKUP_R2 ? "R2" : "KV", files: n, size: text.length };
}
async function backupOpen(env, key) {
  const text = await storeGet(env, key);
  if (!text) return null;
  const d = JSON.parse(text);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64ToBytes(d.iv) }, await backupKey(env), b64ToBytes(d.data));
  return { created: d.created, ...JSON.parse(new TextDecoder().decode(plain)) };
}

// ---------------------------------------------------------------------------
// 合図と朝の見張り
// ---------------------------------------------------------------------------
async function config(env) {
  const c = await kvJson(env, "config", {});
  return { expect: { ...EXPECT_DEFAULT, ...(c.expect || {}) } };
}

async function beats(env, day) {
  const list = await env.GUARD.list({ prefix: `beat:${day}:` });
  const out = {};
  for (const k of list.keys) out[k.name.split(":").slice(2).join(":")] = await kvJson(env, k.name, null);
  return out;
}

async function xposts(env, day) {
  const list = await env.GUARD.list({ prefix: `xpost:${day}:` });
  return Promise.all(list.keys.map((k) => kvJson(env, k.name, null)));
}

// その日の合図から、まとめ（要対応＋各仕組みの数行）を作る
async function compose(env, day, test) {
  const { expect } = await config(env);
  const b = await beats(env, day);
  const issues = [];
  const pcDown = expect["boot-start"] && !b["boot-start"];   // PC が起動していない日は、ほかの合図が無いのは当然なので1件にまとめる
  for (const [name, on] of Object.entries(expect)) {
    if (!on || (pcDown && name !== "boot-start" && name !== "pc1-mail")) continue;
    if (!b[name]) issues.push(MISSING[name] || `${LABEL[name] || name} の合図がありません`);
    else if (b[name].ok === false) issues.push(`${LABEL[name] || name}で失敗: ${b[name].error || "（理由は PC のログ）"}`);
  }
  // 各仕組みが自分で書いた「要確認」（Claude の使用量の上限・note の要確認コメント・週の点検など）
  const notes = [];
  for (const v of Object.values(b)) for (const w of v?.warn || []) notes.push(w);
  // 昨日の X の投稿結果（X は日中に出るので、朝のまとめでは前の日の分を見る）
  const yday = addDays(day, -1);
  const xp = (await xposts(env, yday)).filter(Boolean);
  const xfail = xp.filter((p) => !p.ok);
  const lines = [];
  const sec = (title, name) => {
    const v = b[name];
    lines.push(`■ ${title}`);
    if (!v) lines.push("  （合図なし）");
    else for (const l of v.lines || [v.ok ? "終わりました" : "失敗"]) lines.push("  " + l);
  };
  sec("ブログ", "blog");
  sec("note", "note");
  sec("X（今日の予定）", "xplan");
  lines.push(`■ X（昨日 ${md(yday)} の投稿）`);
  if (!xp.length) lines.push("  記録なし");
  for (const p of xp) lines.push(`  ${p.at?.slice(11) || ""} ${p.ok ? "投稿 " + (p.url || "") : "失敗 " + (p.error || "")}`);
  if (expect["pc1-mail"]) sec("営業メール（PC①）", "pc1-mail");
  if (b["boot-end"]?.lines?.length) {
    lines.push("■ その他");
    for (const l of b["boot-end"].lines) lines.push("  " + l);
  }
  const all = [...issues, ...xfail.map((p) => `昨日の X の投稿が失敗しました（${p.at?.slice(11) || ""}・${p.error || ""}）`)];
  const t = test ? "【テスト】" : "";
  let subject;
  if (all.length === 1) subject = `${t}【要対応】${issues.length ? shortTitle(issues[0]) : "昨日の X の投稿が失敗しました"}`;
  else if (all.length) subject = `${t}【要対応】今朝の自動運転で ${all.length} 件`;
  else subject = `${t}【OK】今朝のまとめ（${md(day)}）`;
  const body = [
    test ? "これはテストです（見張り番の動作確認のために送っています）。\n" : "",
    all.length ? "◆ 要対応\n" + all.map((x) => "・" + x).join("\n") + "\n" : "",
    notes.length ? "◆ 要確認\n" + notes.map((x) => "・" + x).join("\n") + "\n" : "",
    `◆ 今朝のまとめ（${day}）`,
    ...lines,
    "",
    "（ブログ工場の見張り番・Cloudflare。毎朝 7:30 に確かめています。今の状態: https://github.com/shibuta-a/blog-factory/blob/main/STATUS.md）",
  ].filter((x) => x !== "").join("\n");
  return { subject, body, alert: all.length > 0, issues: all };
}

function shortTitle(issue) {
  if (issue.startsWith("今朝のブログが動いていません")) return "今朝のブログが動いていません";
  return issue.split("。")[0].slice(0, 40);
}

async function morningCheck(env, day, { test = false, send = true } = {}) {
  const r = await compose(env, day, test);
  if (!send) return r;
  const m = await notify(env, r.subject, r.body);
  if (!test) await env.GUARD.put(`report:${day}`, JSON.stringify({ at: jst(), alert: r.alert, subject: r.subject, mail: m }), { expirationTtl: DAY_TTL });
  return { ...r, mail: m };
}

async function onBeat(env, beat) {
  const day = beat.date || today();
  const v = { at: jst(), ok: beat.ok !== false, lines: beat.lines || [], warn: beat.warn || [], error: beat.error || "" };
  await env.GUARD.put(`beat:${day}:${beat.name}`, JSON.stringify(v), { expirationTtl: DAY_TTL });
  await addLog(env, `合図 ${day} ${beat.name} ${v.ok ? "OK" : "NG " + v.error.slice(0, 80)}`);
  // 要対応を送った日に、あとから朝の実行が終わった → 回復の知らせ
  if (beat.name === "boot-end" && v.ok) {
    const rep = await kvJson(env, `report:${day}`, null);
    if (rep?.alert && !rep.recovered) {
      const r = await compose(env, day, false);
      if (!r.alert) {
        await notify(env, `【回復】今朝の自動運転は遅れて終わりました（${md(day)}）`, r.body);
        await env.GUARD.put(`report:${day}`, JSON.stringify({ ...rep, recovered: jst() }), { expirationTtl: DAY_TTL });
      }
    }
  }
}

// X の投稿の結果（maf-x-poster から）。失敗はその場で通知（同じ投稿・同じ状態で1回だけ）
async function onXPost(env, p) {
  const day = (p.at || jst()).slice(0, 10);
  await env.GUARD.put(`xpost:${day}:${p.id}`, JSON.stringify(p), { expirationTtl: DAY_TTL });
  if (p.ok) return;
  const key = `xnotified:${p.id}:${p.state}`;
  if (await env.GUARD.get(key)) return;
  await env.GUARD.put(key, "1", { expirationTtl: DAY_TTL });
  const retry = p.state === "retry";
  await notify(env, `【要対応】X の投稿に失敗しました（${(p.at || "").slice(5, 10).replace("-", "/")} ${(p.at || "").slice(11, 16)}）`, [
    `X（@maf_media_jp）の投稿に失敗しました。${retry ? "混雑・残高切れなどの一時的な失敗なので、5分おきに再試行します（成功しても知らせません。最終的にだめなら、もう一度知らせます）。" : "この投稿はあきらめました。"}`,
    "",
    `投稿: ${p.id}（${p.kind || ""}・予定 ${p.planned || ""}）`,
    `場所: ${p.where || ""}`,
    `エラー: ${p.code || ""} ${p.error || ""}`,
    p.credit ? "\nクレジット（残高）切れの可能性があります。X の Developer Console の Billing で残高を確かめてください。" : "",
    "",
    `本文:\n${p.text || ""}`,
  ].join("\n"));
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const day = today();
      if (await env.GUARD.get(`report:${day}`)) return;   // 同じ日に2回は送らない
      await morningCheck(env, day);
    })().catch((e) => addLog(env, `見張りのエラー: ${String(e).slice(0, 200)}`)));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!authed(request, env)) return new Response("forbidden", { status: 403 });
    try {
      if (url.pathname === "/beat" && request.method === "POST") {
        const b = await request.json();
        if (b.name === "xpost") await onXPost(env, b);
        else await onBeat(env, b);
        return Response.json({ ok: true });
      }
      if (url.pathname === "/notify" && request.method === "POST") {
        const b = await request.json();
        return Response.json(await notify(env, b.subject, b.body));
      }
      if (url.pathname === "/check" && request.method === "POST") {
        const r = await morningCheck(env, url.searchParams.get("date") || today(),
          { test: url.searchParams.get("test") === "1", send: url.searchParams.get("send") !== "0" });
        return Response.json(r);
      }
      if (url.pathname === "/backup" && request.method === "POST") {
        return Response.json(await backupSave(env, await request.json()));
      }
      if (url.pathname === "/backup/list") {
        return Response.json({ store: env.BACKUP_R2 ? "R2" : "KV", keys: await storeList(env) });
      }
      if (url.pathname === "/backup/restore" && request.method === "POST") {
        const d = await backupOpen(env, url.searchParams.get("key") || "backup:latest");
        return d ? Response.json(d) : new Response("not found", { status: 404 });
      }
      if (url.pathname === "/config" && request.method === "POST") {
        const b = await request.json();
        const c = await kvJson(env, "config", {});
        c.expect = { ...(c.expect || {}), ...(b.expect || {}) };
        await env.GUARD.put("config", JSON.stringify(c));
        return Response.json(await config(env));
      }
      if (url.pathname === "/utm") {   // ブログの門番（functions/_middleware.js）が数えた ?utm_source の訪問数
        const from = url.searchParams.get("from") || "0000", to = url.searchParams.get("to") || "9999";
        const out = {};
        let cursor;
        do {
          const l = await env.GUARD.list({ prefix: "utm:", cursor });
          for (const k of l.keys) {
            const day = k.name.split(":")[1];
            if (day >= from && day <= to) out[k.name] = Number((await env.GUARD.get(k.name)) || 0);
          }
          cursor = l.list_complete ? null : l.cursor;
        } while (cursor);
        return Response.json(out);
      }
      if (url.pathname === "/beats") {
        const day = url.searchParams.get("date") || today();
        return Response.json({ day, beats: await beats(env, day), xposts: await xposts(env, day) });
      }
      if (url.pathname === "/status") {
        const day = today();
        const [cfg, b, log, mail, rep] = await Promise.all([config(env), beats(env, day), kvJson(env, "log", []),
          kvJson(env, "mail_last", null), kvJson(env, `report:${day}`, null)]);
        const bk = await storeList(env);
        return Response.json({ now: jst(), config: cfg, beats: b, report: rep, mail_last: mail, backups: bk, log: log.slice(-30) });
      }
    } catch (e) {
      return new Response(`エラー: ${e}\n`, { status: 500 });
    }
    return new Response("not found", { status: 404 });
  },
};
