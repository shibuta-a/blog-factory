// 自動化いきもの診断（/shindan/）の動き。データは data.js（scripts/shindan.py が作る window.SHINDAN）。
// 回答はブラウザの中だけで計算する。サーバーに送るのは「どの生き物が出たか」の1つだけ（レア度を数えるため）。
// 出会った生き物の記録（図鑑）は、このブラウザの localStorage にだけ残す。
(function () {
  "use strict";
  var D = window.SHINDAN;
  if (!D) return;
  var MET_KEY = "shindan-met";
  var NEW_KEY = "shindan-new";

  function store(kind) {
    try { return window[kind]; } catch (e) { return null; }
  }
  function getMet() {
    var s = store("localStorage");
    try { return JSON.parse((s && s.getItem(MET_KEY)) || "[]"); } catch (e) { return []; }
  }
  function addMet(id) {
    var s = store("localStorage"), met = getMet();
    if (met.indexOf(id) < 0) met.push(id);
    try { s && s.setItem(MET_KEY, JSON.stringify(met)); } catch (e) { /* 記録できなくても表示は続ける */ }
  }

  // 回答（0〜3の数字が12個）→ 型・マス・生き物・面倒くさがり度
  function compute(ans) {
    var t = { A: 0, B: 0, C: 0, W: 0, S: 0, L: 0 }, tags = [];
    for (var i = 0; i < D.questions.length; i++) {
      var o = D.questions[i].opts[ans[i]];
      if (!o) return null;
      ["A", "B", "C", "W", "S"].forEach(function (k) { t[k] += o[k] || 0; });
      t.L += o.lz || 0;
      if (o.m && tags.indexOf(o.m) < 0) tags.push(o.m);
    }
    var type = (t.A > 0 ? "S" : "M") + (t.B > 0 ? "P" : "I") + (t.C > 0 ? "C" : "N");
    var lv = t.L <= D.lazy.low ? "low" : t.L <= D.lazy.mid ? "mid" : "high";
    var scene = t.W > 0 ? "work" : "life";
    var key = lv + "-" + scene + "-" + (t.S > 0 ? "solo" : "team");
    var cells = D.map[type] || {};
    var id = t.L >= D.lazy.rare && cells.rare ? cells.rare : cells[key];
    return { type: type, key: key, id: id, scene: scene, tags: tags,
             pct: Math.round(D.lazy.base + t.L * D.lazy.step) };
  }

  D.compute = compute;  // scripts/shindan.py sim が同じ計算でランダムに試す

  function parseAns(str) {
    if (!/^[0-3]+$/.test(str || "") || str.length !== D.questions.length) return null;
    return str.split("").map(Number);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ---------------- 質問 ----------------
  function quiz(root) {
    var ans = [], i = 0;
    var start = document.getElementById("sd-start");
    function show() {
      var q = D.questions[i];
      root.innerHTML = "";
      var head = el("div", "sd-qhead");
      head.appendChild(el("span", "sd-qnum", "Q" + (i + 1) + " / " + D.questions.length));
      var bar = el("div", "sd-bar"), fill = el("span");
      fill.style.width = Math.round((i / D.questions.length) * 100) + "%";
      bar.appendChild(fill);
      head.appendChild(bar);
      root.appendChild(head);
      root.appendChild(el("p", "sd-q", q.q));
      var list = el("div", "sd-opts");
      q.opts.forEach(function (o, j) {
        var b = el("button", "sd-opt" + (ans[i] === j ? " is-on" : ""), o.t);
        b.type = "button";
        b.addEventListener("click", function () {
          ans[i] = j;
          if (i < D.questions.length - 1) { i++; show(); window.scrollTo(0, root.offsetTop - 12); }
          else finish();
        });
        list.appendChild(b);
      });
      root.appendChild(list);
      if (i > 0) {
        var back = el("button", "sd-back", "← ひとつ前にもどる");
        back.type = "button";
        back.addEventListener("click", function () { i--; show(); });
        root.appendChild(back);
      }
    }
    function finish() {
      var r = compute(ans);
      if (!r || !r.id) return;
      var s = store("sessionStorage");
      try { s && s.setItem(NEW_KEY, r.id); } catch (e) { /* 数えられなくても結果は出す */ }
      location.href = "r/" + r.id + ".html?a=" + ans.join("");
    }
    if (start) {
      start.addEventListener("click", function () {
        document.getElementById("sd-intro").hidden = true;
        root.hidden = false;
        show();
        window.scrollTo(0, root.offsetTop - 12);
      });
    }
  }

  // ---------------- 結果 ----------------
  function tier(p) {
    return p < D.tiers.gekirare ? ["激レア", "is-gekirare"] : p < D.tiers.rare ? ["レア", "is-rare"] : ["コモン", "is-common"];
  }

  function rarity(id, box) {
    if (!box) return;
    var expected = parseFloat(box.getAttribute("data-expected") || "0");
    function draw(total, n) {
      var useReal = total >= D.minTotal;
      var p = useReal ? (n / total) * 100 : expected;
      var tr = tier(p);
      box.querySelector(".sd-tier").textContent = tr[0];
      box.className = "sd-rarity " + tr[1];
      box.querySelector(".sd-share").textContent = (p < 0.1 ? p.toFixed(2) : p.toFixed(1)) + "%";
      box.querySelector(".sd-rnote").textContent = useReal
        ? "これまでに診断した " + total.toLocaleString() + " 人のうち " + n.toLocaleString() + " 人に出ました。"
        : "まだ診断した人が少ないため、回答をランダムに1万回試したときの割合です（実際の結果は " + total.toLocaleString() + " 人分）。";
    }
    fetch(D.api + "/stats").then(function (r) { return r.json(); })
      .then(function (s) { draw(s.total || 0, (s.counts || {})[id] || 0); })
      .catch(function () { draw(0, 0); });
  }

  function result(page) {
    var id = page.getAttribute("data-id");
    var ans = parseAns(new URLSearchParams(location.search).get("a"));
    var r = ans && compute(ans);
    var mine = r && r.id === id;
    var hit = Promise.resolve();
    if (mine) {
      addMet(id);
      var s = store("sessionStorage"), flag = null;
      try { flag = s && s.getItem(NEW_KEY); } catch (e) { flag = null; }
      if (flag === id) {
        try { s.removeItem(NEW_KEY); } catch (e) { /* なにもしない */ }
        hit = fetch(D.api + "/hit", { method: "POST", headers: { "content-type": "application/json" },
                                      body: JSON.stringify({ id: id }) }).catch(function () {});
      }
      document.querySelectorAll(".sd-mine").forEach(function (e) { e.hidden = false; });
      document.querySelectorAll(".sd-notmine").forEach(function (e) { e.hidden = true; });
      var pct = document.getElementById("sd-pct");
      if (pct) {
        pct.textContent = r.pct + "%";
        document.getElementById("sd-pctbar").style.width = r.pct + "%";
        document.getElementById("sd-pctnote").textContent =
          r.pct >= 75 ? "かなりの面倒くさがり。だからこそ、自動化の伸びしろが大きいタイプです。"
          : r.pct >= 45 ? "ほどよく面倒くさがり。ここぞという所だけ自動化すると気持ちよく回ります。"
          : "面倒をあまり苦にしない、まじめな手さばき。仕組みにすると、さらに速くなります。";
      }
      var list = document.getElementById("sd-mendou");
      if (list) {
        var tags = r.tags.slice();
        (D.fallback[r.scene] || []).forEach(function (m) { if (tags.indexOf(m) < 0) tags.push(m); });
        tags = tags.filter(function (m) { return D.tags[m]; })
          .sort(function (a, b) { return D.tags[a].rank - D.tags[b].rank; }).slice(0, 3);
        list.innerHTML = "";
        tags.forEach(function (m) {
          var t = D.tags[m], li = el("li");
          li.appendChild(el("span", "sd-mname", t.name));
          li.appendChild(el("span", "sd-mrank", "自動化ランキング " + t.rank + "位"));
          if (t.url) {
            var a = el("a", "sd-mlink", "やり方を読む");
            a.href = t.url;
            li.appendChild(a);
          }
          list.appendChild(li);
        });
      }
      var x = document.getElementById("sd-x");
      if (x) {
        var u = new URL(x.href);
        u.searchParams.set("text", u.searchParams.get("text").replace("が出ました。", "が出ました。面倒くさがり度 " + r.pct + "%。"));
        x.href = u.toString();
      }
    }
    hit.then(function () { rarity(id, document.getElementById("sd-rarity")); });
  }

  // ---------------- 図鑑 ----------------
  function zukan(root) {
    var met = getMet(), n = 0;
    root.querySelectorAll("[data-id]").forEach(function (c) {
      if (met.indexOf(c.getAttribute("data-id")) >= 0) { c.classList.add("is-met"); n++; }
    });
    var cnt = document.getElementById("sd-metcount");
    if (cnt) cnt.textContent = n;
  }

  if (typeof document === "undefined") return;
  var q = document.getElementById("sd-quiz");
  if (q) quiz(q);
  var res = document.querySelector("[data-sd-result]");
  if (res) result(res);
  var z = document.getElementById("sd-zukan");
  if (z) zukan(z);
})();
