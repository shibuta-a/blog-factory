// X への自動ポスト係（Cloudflare Workers・10分ごとの定時実行）
//
// PC が止まっていても、記事の公開予定時刻が来たら X にポストするためにクラウド側で動かす。
//   1. GitHub の public/_x_queue.json（scripts/build.py が出力。記事ごとのポスト文と公開時刻）を読む
//   2. 公開時刻を過ぎていて、まだポストしていない記事を1件だけ選ぶ（1回の実行で1件・1日 max_per_day 件まで）
//   3. 記事のURLが本当に表示される（200）ことを確かめてから X API でポスト
//   4. ポスト済みの印を KV（POSTED）に残す。2回ポストしない
//
// X のキーは Cloudflare の秘密の設定（wrangler secret）にだけ置く。GitHub には上げない。
// 状態の確認: https://<この Worker の URL>/status

const QUEUE_URL = "https://raw.githubusercontent.com/shibuta-a/blog-factory/main/public/_x_queue.json";
const TWEET_URL = "https://api.x.com/2/tweets";
const STALE_HOURS = 48; // 公開から48時間以上たった記事はポストしない（止まっていた後に古い記事をまとめて出さない）

function jst(d = new Date()) {
  return new Date(d.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM"
}

function q(s) {
  return encodeURIComponent(s).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}

async function oauthHeader(method, url, env) {
  const oauth = {
    oauth_consumer_key: env.X_API_KEY,
    oauth_nonce: crypto.randomUUID().replace(/-/g, ""),
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp: String(Math.floor(Date.now() / 1000)),
    oauth_token: env.X_ACCESS_TOKEN,
    oauth_version: "1.0",
  };
  const params = Object.keys(oauth).sort().map((k) => `${q(k)}=${q(oauth[k])}`).join("&");
  const base = [method, q(url), q(params)].join("&");
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(`${q(env.X_API_SECRET)}&${q(env.X_ACCESS_SECRET)}`),
    { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(base));
  oauth.oauth_signature = btoa(String.fromCharCode(...new Uint8Array(sig)));
  return "OAuth " + Object.keys(oauth).sort().map((k) => `${q(k)}="${q(oauth[k])}"`).join(", ");
}

async function getPosted(env) {
  return (await env.POSTED.get("posted", "json")) || {};
}

async function note(env, msg) {
  const log = (await env.POSTED.get("log", "json")) || [];
  log.push(`${jst()} ${msg}`);
  await env.POSTED.put("log", JSON.stringify(log.slice(-50)));
}

async function run(env) {
  const res = await fetch(`${QUEUE_URL}?t=${Date.now()}`, { cf: { cacheTtl: 0 } });
  if (!res.ok) return `予定表を読めません（${res.status}）`;
  const queue = await res.json();
  if (!queue.enabled) return "X への自動ポストはオフです";
  if (!env.X_API_KEY || !env.X_ACCESS_TOKEN) return "X のキーが未設定です";

  const now = jst();
  const stale = jst(new Date(Date.now() - STALE_HOURS * 3600 * 1000));
  const posted = await getPosted(env);
  const today = now.slice(0, 10);
  const doneToday = Object.values(posted).filter((p) => (p.at || "").slice(0, 10) === today).length;
  if (doneToday >= (queue.max_per_day || 3)) return `今日は上限（${queue.max_per_day}件）までポスト済み`;

  const due = (queue.items || [])
    .filter((it) => it.at <= now && it.at >= stale && it.at >= (queue.since || "") && !posted[it.slug])
    .sort((a, b) => (a.at < b.at ? -1 : 1));
  if (!due.length) return "ポストする記事はありません";
  const it = due[0];

  // 記事が本当に表示されているか（予約公開の門番が 404 を返す間はポストしない）
  const page = await fetch(it.url, { redirect: "follow" });
  if (page.status !== 200) return `${it.slug}: 記事がまだ表示されていない（${page.status}）ので次回に`;

  const r = await fetch(TWEET_URL, {
    method: "POST",
    headers: { Authorization: await oauthHeader("POST", TWEET_URL, env), "Content-Type": "application/json" },
    body: JSON.stringify({ text: it.text }),
  });
  const body = await r.text();
  if (r.status === 201) {
    const id = JSON.parse(body).data?.id || "";
    posted[it.slug] = { at: now, id };
    await env.POSTED.put("posted", JSON.stringify(posted));
    await note(env, `ポスト ${it.slug} → https://x.com/i/web/status/${id}`);
    return `ポストしました: ${it.slug}`;
  }
  // 重複（同じ文面）と判定されたものは、二度と試さない
  if (r.status === 403 && body.includes("duplicate")) {
    posted[it.slug] = { at: now, id: "", error: "duplicate" };
    await env.POSTED.put("posted", JSON.stringify(posted));
  }
  await note(env, `失敗 ${it.slug}: ${r.status} ${body.slice(0, 300)}`);
  return `失敗: ${r.status}`;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env).then((m) => env.POSTED.put("last", `${jst()} ${m}`)));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/status") {
      const [posted, log, last] = await Promise.all([getPosted(env), env.POSTED.get("log", "json"), env.POSTED.get("last")]);
      return Response.json({ last, posted, log: log || [] });
    }
    return new Response("not found", { status: 404 });
  },
};
