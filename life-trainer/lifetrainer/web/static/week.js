// Life Trainer 주간 뷰 — 바닐라 JS, 외부 요청 없음(CDN 금지).
//
// 전용 주간 API(`/api/week/<end_day>`)는 아직 없다(계약서 §6, 라우트는 app.py
// 담당이 추가해야 한다). 그래서 이 화면은 기존 `/api/day/<day>` 를 7일치
// 순서대로 불러 클라이언트에서 직접 합산한다 — "없으면 기존 /api/day 만으로
// 할 수 있는 범위까지만 하라"는 지시를 그대로 따른 것이다. 지어낸 통계는
// 없다: 전부 매일의 실제 응답을 더한 값이다.
// ★ 앱이 `/planner` 아래에 mount 될 수 있다 — 절대 경로로 부르면 접두사 밖으로 나간다.
var BASE = (function () {
  var el = document.getElementById("app-base");
  return (el && el.dataset.base) || "";
})();

(function () {
  "use strict";

  var body = document.body;
  var endDay = body.dataset.endDay;
  var today = body.dataset.today;

  var WEEKDAY_KR = ["일", "월", "화", "수", "목", "금", "토"];
  var WEEKDAY_KR_SHORT = ["일", "월", "화", "수", "목", "금", "토"];

  function fmtHm(totalSec) {
    var sec = Math.max(0, Math.round(totalSec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    return h + "시간 " + m + "분";
  }

  function shiftDay(dayStr, delta) {
    var d = new Date(dayStr + "T00:00:00");
    d.setDate(d.getDate() + delta);
    var y = d.getFullYear();
    var mo = String(d.getMonth() + 1).padStart(2, "0");
    var da = String(d.getDate()).padStart(2, "0");
    return y + "-" + mo + "-" + da;
  }

  function weekdayOf(dayStr) {
    var d = new Date(dayStr + "T00:00:00");
    return d.getDay();
  }

  if (!endDay) return;

  var days = [];
  for (var i = 6; i >= 0; i--) days.push(shiftDay(endDay, -i));

  Promise.all(
    days.map(function (d) {
      return fetch(BASE + "/api/day/" + d)
        .then(function (res) {
          return res.ok ? res.json() : null;
        })
        .catch(function () {
          return null;
        });
    })
  ).then(function (results) {
    render(days, results);
  });

  function render(days, results) {
    var loading = document.getElementById("week-loading");
    if (loading) loading.hidden = true;

    var palette = null;
    for (var i = 0; i < results.length; i++) {
      if (results[i] && results[i].palette) {
        palette = results[i].palette;
        break;
      }
    }
    palette = palette || { order: [], labels: {}, categories: {} };

    // ── 일별 활동 시간 / 달성 집계 ────────────────────────────────────
    var perDayActive = days.map(function (d, idx) {
      var r = results[idx];
      return r ? r.stats.active_sec || 0 : 0;
    });
    var totalActive = perDayActive.reduce(function (a, b) { return a + b; }, 0);
    var achievedSum = 0;
    var totalSum = 0;
    results.forEach(function (r) {
      if (!r) return;
      achievedSum += r.stats.achieved_count || 0;
      totalSum += r.stats.total_count || 0;
    });
    var achievementPct = totalSum > 0 ? Math.round((achievedSum / totalSum) * 100) : 0;

    var bestIdx = 0;
    for (var i2 = 1; i2 < perDayActive.length; i2++) {
      if (perDayActive[i2] > perDayActive[bestIdx]) bestIdx = i2;
    }
    var bestHasData = perDayActive[bestIdx] > 0;

    // ── 카테고리별 시간 합계(슬롯 카운트 * slot_minutes) ────────────────
    var catSeconds = {};
    results.forEach(function (r) {
      if (!r || !r.slots) return;
      var slotSec = (r.slot_minutes || 10) * 60;
      r.slots.forEach(function (s) {
        if (!(s.category in palette.categories)) return; // 구조 상태(off/away/unknown) 제외
        catSeconds[s.category] = (catSeconds[s.category] || 0) + slotSec;
      });
    });

    fillStats(perDayActive, totalActive, achievementPct, achievedSum, totalSum, bestIdx, bestHasData, days);
    fillChart(days, perDayActive);
    fillCategoryBreakdown(palette, catSeconds);
  }

  function fillStats(perDayActive, totalActive, achievementPct, achievedSum, totalSum, bestIdx, bestHasData, days) {
    var grid = document.getElementById("stats-grid");
    if (grid) grid.hidden = false;

    setText("stat-active", fmtHm(totalActive));
    setText("stat-active-sub", "하루 평균 " + fmtHm(totalActive / 7));

    setText("stat-achievement", achievementPct + "%");
    setText("stat-achievement-sub", "(" + achievedSum + "/" + totalSum + ")");

    if (bestHasData) {
      setText("stat-best-day", WEEKDAY_KR[weekdayOf(days[bestIdx])] + "요일");
      setText("stat-best-day-sub", days[bestIdx] + " · " + fmtHm(perDayActive[bestIdx]));
    } else {
      setText("stat-best-day", "기록 없음");
      setText("stat-best-day-sub", " ");
    }
  }

  function fillChart(days, perDayActive) {
    var card = document.getElementById("weekly-card");
    var chart = document.getElementById("week-chart");
    if (!chart) return;
    if (card) card.hidden = false;

    var max = Math.max.apply(null, perDayActive.concat([1])); // 0 나눗셈 방지
    chart.innerHTML = "";
    days.forEach(function (d, idx) {
      var col = document.createElement("div");
      var bar = document.createElement("i");
      bar.className = "week-bar" + (d === today ? " is-today" : "");
      var pct = Math.max(2, Math.round((perDayActive[idx] / max) * 100));
      bar.style.height = pct + "%";
      bar.title = d + " · " + fmtHm(perDayActive[idx]);
      var label = document.createElement("span");
      label.textContent = WEEKDAY_KR_SHORT[weekdayOf(d)];
      col.appendChild(bar);
      col.appendChild(label);
      chart.appendChild(col);
    });
  }

  function fillCategoryBreakdown(palette, catSeconds) {
    var card = document.getElementById("category-breakdown");
    var legend = document.getElementById("week-legend");
    if (!legend) return;

    var order = (palette.order || []).filter(function (catId) {
      return (catSeconds[catId] || 0) > 0;
    });
    order.sort(function (a, b) {
      return (catSeconds[b] || 0) - (catSeconds[a] || 0);
    });
    if (order.length === 0) return;
    if (card) card.hidden = false;

    legend.innerHTML = "";
    order.forEach(function (catId) {
      var item = document.createElement("span");
      item.className = "legend-item";
      var swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.background = palette.categories[catId] || "";
      item.appendChild(swatch);
      item.appendChild(document.createTextNode((palette.labels[catId] || catId) + " " + fmtHm(catSeconds[catId])));
      legend.appendChild(item);
    });
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  // ── 테마 토글(day 페이지와 동일한 동작) ──────────────────────────────
  var THEME_KEY = "lt-theme";
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }
  var savedTheme = null;
  try {
    savedTheme = localStorage.getItem(THEME_KEY);
  } catch (e) {
    savedTheme = null;
  }
  if (savedTheme) applyTheme(savedTheme);
  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      var effectiveCurrent = current || (prefersDark ? "dark" : "light");
      var next = effectiveCurrent === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* 무시 */
      }
    });
  }
})();
