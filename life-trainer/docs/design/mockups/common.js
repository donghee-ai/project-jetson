/* 시안 3종이 공유하는 계산 — 디자인이 아니라 데이터 가공만 한다.
 * 여기서 하는 일: (1) 팔레트 CSS 변수 주입 (2) 연속 슬롯 병합 (3) 시각 포맷.
 * 색 hex 는 한 글자도 없다 — data.js 가 config/palette.yaml 에서 뽑아온 것만 쓴다. */
(function () {
  var D = window.LT_DAY;

  // ── 팔레트 주입 ────────────────────────────────────────────────
  var st = document.createElement("style");
  st.textContent =
    window.LT_CSS_LIGHT +
    '\n@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){' +
    window.LT_CSS_DARK.replace(/^:root\s*\{|\}\s*$/g, "") + "}}\n" +
    ':root[data-theme="dark"]{' + window.LT_CSS_DARK.replace(/^:root\s*\{|\}\s*$/g, "") + "}";
  document.head.insertBefore(st, document.head.firstChild);

  // ── 연속 슬롯 병합 ─────────────────────────────────────────────
  // 지금 UI 의 가장 큰 문제: 10분 칸 144개가 전부 따로 그려져 눈이 덩어리를
  // 못 잡는다. 같은 카테고리가 이어지면 하나의 블록으로 본다.
  function runs(slots) {
    var out = [], cur = null;
    slots.forEach(function (s) {
      if (cur && cur.c === s.c && cur.end === s.slot) { cur.end = s.slot + 1; cur.len++; return; }
      cur = { c: s.c, start: s.slot, end: s.slot + 1, len: 1, app: s.app, t: s.t };
      out.push(cur);
    });
    return out;
  }

  function slotToMin(slot) { return (D.grid_start_hour * 60 + slot * D.slot_minutes) % 1440; }
  function hhmm(min) {
    min = ((min % 1440) + 1440) % 1440;
    return String(Math.floor(min / 60)).padStart(2, "0") + ":" + String(min % 60).padStart(2, "0");
  }
  function dur(sec) {
    var m = Math.floor(sec / 60), h = Math.floor(m / 60);
    return h ? h + "시간 " + (m % 60) + "분" : m + "분";
  }
  function durShort(min) {
    var h = Math.floor(min / 60);
    return h ? h + "h" + (min % 60 ? " " + (min % 60) + "m" : "") : min + "m";
  }
  function labelOf(c) { return D.labels[c] || D.structural_labels[c] || c; }
  function isActivity(c) { return !!D.labels[c]; }
  function colorVar(c) { return isActivity(c) ? "var(--cat-" + c + ")" : "var(--structural-" + c + ")"; }

  // 현재 시각 — 시안 스크린샷을 매번 같은 그림으로 찍기 위해 ?now=HH:MM 으로 고정할 수 있다.
  function nowMin() {
    var q = new URLSearchParams(location.search).get("now");
    if (q && /^\d{1,2}:\d{2}$/.test(q)) { var p = q.split(":"); return (+p[0]) * 60 + (+p[1]); }
    var d = new Date();
    return d.getHours() * 60 + d.getMinutes();
  }
  function nowSec() {
    var q = new URLSearchParams(location.search).get("now");
    if (q) return 0;
    return new Date().getSeconds();
  }
  // 논리적 하루(06:00 시작) 기준 위치. 0~144 사이 실수.
  function nowPos() {
    var m = nowMin() - D.grid_start_hour * 60;
    if (m < 0) m += 1440;
    return m / D.slot_minutes;
  }

  var WD = ["일", "월", "화", "수", "목", "금", "토"];
  function weekdayKr(iso) { var d = new Date(iso + "T00:00:00"); return WD[d.getDay()]; }

  // 현재 걸쳐 있는 계획(있으면). 지어내지 않는다 — 없으면 null.
  function currentPlan() {
    var m = nowMin();
    return D.plans.filter(function (p) { return p.start_min <= m && m < p.end_min; })[0] || null;
  }

  window.LT = { runs: runs, slotToMin: slotToMin, hhmm: hhmm, dur: dur, durShort: durShort,
                labelOf: labelOf, isActivity: isActivity, colorVar: colorVar,
                nowMin: nowMin, nowSec: nowSec, nowPos: nowPos, weekdayKr: weekdayKr,
                currentPlan: currentPlan, D: D };
})();
