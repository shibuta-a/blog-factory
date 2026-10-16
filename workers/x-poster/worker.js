// X（@maf_media_jp）の投稿係（Cloudflare Workers・5分おきの定期実行。PC が切れていても動く）
//
//   1. GitHub の data/x-queue/<今日（日本時間）>.json（朝の scripts/x-plan.py が作る予定表）を読む
//   2. 時刻が来ていて、まだ出していない投稿を1件だけ選ぶ（前の投稿から60分はあける）
//   3. リンク付きは記事のページが表示される（200）ことを確かめる
//   4. 二重投稿を防ぐため、X に送る前に KV（POSTED）に「送信中」の印を付ける。印がある投稿は二度と送らない
//   5. 画像があれば /2/media/upload（multipart）→ /2/tweets → 結果を KV に記録
//
// キーは Cloudflare の秘密の設定（X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET / ADMIN_TOKEN）。GitHub には置かない。
//   GET  /status                        … 最後の実行・今月の件数と概算費用・クレジット切れ・最近の記録（だれでも見られる。キーは出ない）
//   GET  /log                           … 投稿の記録（朝の x-plan.py が data/x.json に写す）
//   POST /dry-run?queue=<path>&now=<YYYY-MM-DDTHH:MM>   （ヘッダー Authorization: Bearer <ADMIN_TOKEN>）
//        … 通し確認。本番と同じ判定をして、X API に送る直前で止める（認証の確認 GET /2/users/me だけ行う）

const REPO_RAW = "https://raw.githubusercontent.com/shibuta-a/blog-factory/main/";
const MIN_GAP_MIN = 60;
const RETRY_CODES = [0, 429, 500, 502, 503, 504];

function jst(d = new Date()) {
  return new Date(d.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM"
}
function minutesBetween(a, b) {
  return (Date.parse(b + ":00Z") - Date.parse(a + ":00Z")) / 60000;
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

async function xFetch(env, method, url, body, contentType) {
  const headers = { Authorization: await oauthHeader(method, url, env) };
  if (contentType) headers["Content-Type"] = contentType;
  try {
    const r = await fetch(url, { method, headers, body });
    const text = await r.text();
    let data = {};
    try { data = JSON.parse(text || "{}"); } catch { data = { raw: text.slice(0, 300) }; }
    return { code: r.status, data };
  } catch (e) {
    return { code: 0, data: { error: String(e).slice(0, 200) } };
  }
}

function isCreditError(code, data) {
  const s = JSON.stringify(data).toLowerCase();
  return code === 402 || s.includes("credit") || s.includes("insufficient") || s.includes("payment");
}

async function kvJson(env, key, dflt) {
  return (await env.POSTED.get(key, "json")) ?? dflt;
}

async function addLog(env, msg) {
  const log = await kvJson(env, "log", []);
  log.push(`${jst()} ${msg}`);
  await env.POSTED.put("log", JSON.stringify(log.slice(-80)));
}

// 予定表を読む（GitHub の raw。キャッシュを避けるため毎回クエリを変える）
async function loadQueue(path) {
  const r = await fetch(`${REPO_RAW}${path}?t=${Date.now()}`, { cf: { cacheTtl: 0 } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`予定表を読めません（${r.status}）`);
  return await r.json();
}

// 本番と通し確認で共通の判定。投稿する1件（なければ null）と説明を返す
async function pick(env, queue, now) {
  const states = await kvJson(env, "items", {});      // id → {state: posting|posted|failed|retry, ...}
  const lines = [];
  for (const it of queue.items || []) {
    const st = states[it.id]?.state;
    lines.push(`${it.at.slice(11)} ${it.kind} ${st || (it.at <= now ? "時刻が来た" : "待ち")}`);
  }
  const due = (queue.items || []).filter((it) => it.at <= now && (!states[it.id] || states[it.id].state === "retry"))
    .sort((a, b) => (a.at < b.at ? -1 : 1));
  if (!due.length) return { it: null, msg: "時刻が来た未投稿の投稿はありません", lines };
  const last = await env.POSTED.get("last_posted_at");
  if (last && minutesBetween(last, now) < MIN_GAP_MIN) return { it: null, msg: `前の投稿（${last}）から${MIN_GAP_MIN}分たっていないので次の回に`, lines };
  const it = due[0];
  if (it.check_url) {
    const page = await fetch(it.check_url, { redirect: "follow", headers: { "User-Agent": "maf-x-poster" } });
    if (page.status !== 200) return { it: null, msg: `${it.id}: 記事がまだ表示されていない（${page.status}）ので次の回に`, lines };
  }
  return { it, msg: "", lines, states };
}

async function loadImage(it) {
  if (!it.image) return null;
  const r = await fetch(`${REPO_RAW}${it.image}?t=${Date.now()}`);
  if (!r.ok) throw new Error(`画像を読めません（${r.status} ${it.image}）`);
  return await r.arrayBuffer();
}

async function run(env, nowOverride) {
  if (!env.X_API_KEY || !env.X_ACCESS_TOKEN) return "X のキーが未設定です";
  const now = nowOverride || jst();
  const queue = await loadQueue(`data/x-queue/${now.slice(0, 10)}.json`);
  if (!queue) return "今日の予定表はありません";
  const { it, msg, states } = await pick(env, queue, now);
  if (!it) return msg;

  // 二重投稿の防止: 送る前に「送信中」を書く。送信中のまま残った投稿（途中で落ちた等）は、出たかどうか分からないので二度と送らない
  const prev = states[it.id];
  states[it.id] = { state: "posting", at: now, tries: (prev?.tries || 0) + 1 };
  await env.POSTED.put("items", JSON.stringify(states));

  const fail = async (where, r) => {
    const retry = RETRY_CODES.includes(r.code) || isCreditError(r.code, r.data);
    states[it.id] = { state: retry && states[it.id].tries < 6 ? "retry" : "failed", at: now, tries: states[it.id].tries,
                      where, code: r.code, error: JSON.stringify(r.data).slice(0, 300) };
    await env.POSTED.put("items", JSON.stringify(states));
    if (isCreditError(r.code, r.data)) await env.POSTED.put("credit", JSON.stringify({ depleted: true, at: now }));
    await addLog(env, `失敗 ${it.id}（${where}）: ${r.code} ${JSON.stringify(r.data).slice(0, 200)}`);
    return `失敗: ${it.id} ${where} ${r.code}`;
  };

  let mediaIds;
  const img = await loadImage(it);
  if (img) {
    const url = "https://api.x.com/2/media/upload";
    const form = new FormData();
    form.append("media", new Blob([img], { type: "image/png" }), "card.png");
    form.append("media_category", "tweet_image");
    const r = await xFetch(env, "POST", url, form);        // multipart の中身は OAuth 1.0a の署名に含めない
    const id = r.data?.data?.id;
    if (!id) return await fail("画像のアップロード", r);
    mediaIds = [id];
  }
  const body = { text: it.text };
  if (mediaIds) body.media = { media_ids: mediaIds };
  const r = await xFetch(env, "POST", "https://api.x.com/2/tweets", JSON.stringify(body), "application/json");
  if (r.code !== 200 && r.code !== 201) return await fail("投稿", r);
  const tid = r.data?.data?.id || "";
  states[it.id] = { state: "posted", at: now, tries: states[it.id].tries, tweet_id: tid };
  await env.POSTED.put("items", JSON.stringify(states));
  await env.POSTED.put("last_posted_at", now);
  const posts = await kvJson(env, "posts", []);
  posts.push({ id: it.id, date: queue.date, kind: it.kind, at: it.at, posted_at: now, tweet_id: tid,
               url: it.url || null, text: it.text, cost: Number(it.est_cost || 0) });
  await env.POSTED.put("posts", JSON.stringify(posts.slice(-400)));
  if ((await kvJson(env, "credit", {})).depleted) await env.POSTED.put("credit", JSON.stringify({ depleted: false, recovered_at: now }));
  await addLog(env, `投稿 ${it.id} → https://x.com/i/web/status/${tid}`);
  return `投稿しました: ${it.id}`;
}

async function dryRun(env, url) {
  const now = url.searchParams.get("now") || jst();
  const path = url.searchParams.get("queue") || `data/x-queue/${now.slice(0, 10)}.json`;
  const out = [`いま（日本時間）: ${now}・予定表: ${path}（通し確認: 投稿はしない）`];
  if (!env.X_API_KEY || !env.X_ACCESS_TOKEN) return [...out, "X のキーが未設定です"];
  const queue = await loadQueue(path);
  if (!queue) return [...out, "予定表がありません"];
  const { it, msg, lines } = await pick(env, queue, now);
  out.push(...lines.map((l) => "  " + l));
  if (!it) return [...out, msg];
  out.push(`次に出す投稿: ${it.id}（${it.kind}・予定 ${it.at.slice(11)}）`, it.text);
  if (it.check_url) out.push(`記事のページの表示を確認しました（${it.check_url}・200）`);
  const img = await loadImage(it);
  if (img) out.push(`画像: ${it.image}（${Math.round(img.byteLength / 1024)} KB）を GitHub から読み込みました`);
  const me = await xFetch(env, "GET", "https://api.x.com/2/users/me");
  out.push(`認証の確認: ${me.code === 200 ? "OK @" + me.data.data.username : "NG " + me.code + " " + JSON.stringify(me.data).slice(0, 200)}`);
  out.push(`通し確認のため、ここで止めます（本番はこの次に KV に「送信中」を書き、${img ? "POST /2/media/upload → " : ""}POST /2/tweets を送ります）`);
  return out;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env).catch((e) => `エラー: ${String(e).slice(0, 200)}`)
      .then((m) => env.POSTED.put("last", `${jst()} ${m}`)));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/status") {
      const [last, credit, posts, log, items] = await Promise.all([env.POSTED.get("last"), kvJson(env, "credit", {}),
        kvJson(env, "posts", []), kvJson(env, "log", []), kvJson(env, "items", {})]);
      const month = jst().slice(0, 7);
      const mp = posts.filter((p) => p.posted_at.startsWith(month));
      return Response.json({ last, credit, month, month_posts: mp.length,
        month_cost: Math.round(mp.reduce((s, p) => s + p.cost, 0) * 1000) / 1000, items, log: log.slice(-20) });
    }
    if (url.pathname === "/log") {
      const [posts, credit, items] = await Promise.all([kvJson(env, "posts", []), kvJson(env, "credit", {}), kvJson(env, "items", {})]);
      return Response.json({ posts, credit, failed: Object.fromEntries(Object.entries(items).filter(([, v]) => v.state !== "posted")) });
    }
    if (url.pathname === "/dry-run" && request.method === "POST") {
      if (!env.ADMIN_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.ADMIN_TOKEN}`)
        return new Response("forbidden", { status: 403 });
      try {
        return new Response((await dryRun(env, url)).join("\n") + "\n", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
      } catch (e) {
        return new Response(`エラー: ${e}\n`, { status: 500 });
      }
    }
    return new Response("not found", { status: 404 });
  },
};
