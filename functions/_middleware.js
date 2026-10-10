// 予約公開の門番（Cloudflare Pages Functions）
//
// 予約記事も含めて全記事をビルドしておき、ここでアクセスのたびに「公開予定時刻（日本時間）が来たか」を判定する。
// 時刻前の記事は 404、トップページの一覧とサイトマップからも外す。時刻が来た瞬間から表示される。
// 何かを定時に動かす必要がない（GitHub Actions の定時実行のように「動かなかった」が起きない）。
//
// 予約の一覧は scripts/build.py が public/_schedule.json に書く（{ "slug": "YYYY-MM-DDTHH:MM", ... }）。
// この関数が動くURLは public/_routes.json で、ページ・サイトマップだけに絞っている（画像・CSSでは動かない）。

function nowJst() {
  return new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM"
}

async function loadSchedule(context) {
  const url = new URL("/_schedule.json", context.request.url);
  const res = await context.env.ASSETS.fetch(url);
  if (!res.ok) return {};
  try { return await res.json(); } catch { return {}; }
}

function notFound() {
  return new Response(
    '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="robots" content="noindex">' +
    '<title>ページが見つかりません</title><p>ページが見つかりません。<a href="/">トップページへ</a></p></html>',
    { status: 404, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

export async function onRequest(context) {
  const url = new URL(context.request.url);
  const path = url.pathname;

  // 予約の一覧そのものは見せない
  if (path === "/_schedule.json") return notFound();

  const schedule = await loadSchedule(context);
  const now = nowJst();
  const future = new Set(Object.keys(schedule).filter((s) => schedule[s] > now));
  if (future.size === 0) return context.next();

  // 記事ページ：/slug /slug.html /en/slug など
  const last = path.split("/").pop().replace(/\.html$/, "");
  if (last && future.has(last)) return notFound();

  const res = await context.next();
  const type = res.headers.get("content-type") || "";

  // サイトマップ：時刻前の記事の <url> を外す
  if (path.endsWith("/sitemap.xml")) {
    let xml = await res.text();
    xml = xml.replace(/\s*<url><loc>[^<]*\/([\w-]+)<\/loc>.*?<\/url>/g, (m, slug) => (future.has(slug) ? "" : m));
    const headers = new Headers(res.headers);
    headers.set("cache-control", "no-store");
    return new Response(xml, { status: res.status, headers });
  }

  // トップ・一覧ページ：時刻前の記事のカードを外す
  if (type.includes("text/html")) {
    const out = new HTMLRewriter()
      .on("li[data-slug]", {
        element(el) {
          if (future.has(el.getAttribute("data-slug"))) el.remove();
        },
      })
      .transform(res);
    const headers = new Headers(out.headers);
    headers.set("cache-control", "no-store");
    return new Response(out.body, { status: out.status, headers });
  }
  return res;
}
