// 自動化いきもの診断（ブログの /shindan/）の「出た数」を数える係。診断専用の Worker と D1（maf-shindan）。
//
//   POST /hit   {"id": "shoebill"}  … 診断を終えた人に出た生き物を1つ数える（名前・メールなど個人の情報は受け取らない・残さない）
//   GET  /stats                      … {"total": 全体の数, "counts": {id: 数}}（結果ページのレア度に使う）
//
// 数える生き物は表 counts にある id だけ（scripts/shindan.py deploy が全種の行を入れておく）。知らない id は数えない。

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "content-type",
  "access-control-max-age": "86400",
};

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json; charset=utf-8", ...CORS, ...extra } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    if (url.pathname === "/hit" && request.method === "POST") {
      let id = "";
      try { id = String((await request.json()).id || ""); } catch { /* 空のまま */ }
      if (!/^[a-z0-9-]{2,40}$/.test(id)) return json({ ok: false }, 400);
      const r = await env.DB.prepare("UPDATE counts SET n = n + 1 WHERE id = ?").bind(id).run();
      return json({ ok: r.meta.changes === 1 });
    }

    if (url.pathname === "/stats" && request.method === "GET") {
      const { results } = await env.DB.prepare("SELECT id, n FROM counts").all();
      const counts = {};
      let total = 0;
      for (const row of results) { counts[row.id] = row.n; total += row.n; }
      return json({ total, counts }, 200, { "cache-control": "public, max-age=60" });
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
