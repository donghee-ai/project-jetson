// Life Trainer 플래너 — 바닐라 JS, 외부 요청 없음(CDN 금지).
// 서버가 <script id="lt-initial-data"> 에 심어준 하루치 데이터를 읽어 격자/범례를 보조한다.
(function () {
  "use strict";

  var body = document.body;

  // ── 저장 뒤 화면 교체 ─────────────────────────────────────────────────
  //
  // ★ `location.reload()` 를 그냥 부르면 **눈이 아프다**(사용자 보고).
  //   흰 화면이 한 번 번쩍이고, 스크롤이 튀고, 방금 뭘 고쳤는지 확인할 틈이 없다.
  //   저장은 하루에도 여러 번 하는 동작이라 그 한 번이 계속 쌓인다.
  //
  //   그래서 세 가지를 한다:
  //     ① 스크롤 위치를 넘겨준다 — 새 화면이 보던 자리에서 시작한다
  //     ② 잠깐 흐려졌다가(190ms) 새 화면이 부드럽게 들어온다(240ms) — 번쩍임이 사라진다
  //     ③ `prefers-reduced-motion` 이면 흐림 없이 스크롤만 넘긴다
  var SCROLL_KEY = "lt:scroll";
  var FADE_MS = 190;

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function softReload() {
    try {
      // ★ 브라우저 자체 복원을 끈다. 켜 둔 채로 우리도 옮기면 **둘이 싸운다** —
      //   실측에서 900 에서 저장했더니 1492 로 튀었다. 복원은 한 곳만 해야 한다.
      if ("scrollRestoration" in history) history.scrollRestoration = "manual";
      sessionStorage.setItem(SCROLL_KEY, String(window.scrollY || 0));
    } catch (e) {
      /* 사파리 프라이빗 모드 등 — 스크롤 복원을 못 해도 저장은 끝났다 */
    }
    if (prefersReducedMotion()) {
      location.reload();
      return;
    }
    body.classList.add("page-leaving");
    window.setTimeout(function () {
      location.reload();
    }, FADE_MS);
  }

  // 새로 그려진 화면: 보던 자리로 돌려놓고 부드럽게 들인다.
  (function restoreAfterReload() {
    var saved = null;
    try {
      saved = sessionStorage.getItem(SCROLL_KEY);
      sessionStorage.removeItem(SCROLL_KEY);
    } catch (e) {
      /* 무시 */
    }
    if (saved !== null) {
      var y = Number(saved) || 0;
      // 레이아웃이 잡힌 뒤에 옮겨야 한다 — 지금 부르면 0 으로 되돌아온다.
      // 두 번 부르는 이유: 첫 프레임에는 이미지·폰트가 아직이라 문서 높이가
      // 모자라 `scrollTo` 가 잘린다. `load` 뒤에 한 번 더 맞춘다.
      window.requestAnimationFrame(function () {
        window.scrollTo(0, y);
      });
      window.addEventListener("load", function () {
        window.scrollTo(0, y);
        // 다 옮겼으면 브라우저 기본 동작을 돌려준다 — 뒤로가기까지 망가뜨리지 않는다.
        if ("scrollRestoration" in history) history.scrollRestoration = "auto";
      });
      if (!prefersReducedMotion()) body.classList.add("page-entering");
    }
  })();
  var readOnly = body.dataset.readOnly === "true";
  var day = body.dataset.day;
  var dataEl = document.getElementById("lt-initial-data");
  var initialData = null;
  try {
    initialData = dataEl ? JSON.parse(dataEl.textContent) : null;
  } catch (e) {
    initialData = null;
  }
  var palette = (initialData && initialData.palette) || { order: [], labels: {} };
  // 구조 상태 이름은 서버가 팔레트에서 실어 준다 (`structural_labels`).
  // 전에는 여기와 app.py 두 곳에 리터럴이 있어서 상태를 더할 때 한쪽만 고쳐졌다.
  var structuralLabels = (initialData && initialData.palette && initialData.palette.structural_labels) || {};

  // ── 테마 토글 (prefers-color-scheme 은 CSS 가 이미 처리, 여기는 수동 오버라이드만) ──
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
  // ?theme=dark|light 로 직접 링크·검증용 오버라이드를 줄 수 있다(저장하지는 않는다 —
  // 링크를 껐다 켜면 원래 선택으로 돌아온다). prefers-color-scheme 를 흉내내기 힘든
  // 자동화 캡처 도구에서도 다크 모드를 확인할 수 있게 하기 위함이다.
  var urlTheme = null;
  try {
    urlTheme = new URLSearchParams(location.search).get("theme");
  } catch (e) {
    urlTheme = null;
  }
  if (urlTheme === "light" || urlTheme === "dark") {
    applyTheme(urlTheme);
  } else if (savedTheme) {
    applyTheme(savedTheme);
  }

  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var prefersDark =
        window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      var effectiveCurrent = current || (prefersDark ? "dark" : "light");
      var next = effectiveCurrent === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* localStorage 를 쓸 수 없어도 화면 전환 자체는 계속 동작한다 */
      }
    });
  }

  function labelOf(category) {
    return (palette.labels && palette.labels[category]) || structuralLabels[category] || category;
  }

  // ── eyebrow: 요일 (harugyeol .eyebrow) ────────────────────────────────
  // 서버는 boundary_hour 계산까지 끝낸 day 문자열만 준다. 요일 이름은
  // 순수 표시용이라 여기서 클라이언트가 계산한다(app.py 는 건드리지 않는다).
  var WEEKDAY_KR = ["일", "월", "화", "수", "목", "금", "토"];
  var eyebrowEl = document.getElementById("day-eyebrow");
  if (eyebrowEl && day) {
    var parsed = new Date(day + "T00:00:00");
    eyebrowEl.textContent = isNaN(parsed.getTime()) ? "DAILY PLAN" : WEEKDAY_KR[parsed.getDay()] + "요일 · DAILY PLAN";
  }

  // 헤더의 시계와 격자 재생헤드는 같은 Date 인스턴스에서 갱신한다. 서로 다른
  // 타이머가 각자 시각을 계산하면 분 경계에서 두 값이 잠깐 어긋날 수 있다.
  var liveClockTime = document.getElementById("live-clock-time");

  function hhmm(now) {
    return String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0");
  }

  function updateClock(now) {
    if (liveClockTime) liveClockTime.textContent = hhmm(now);
  }

  // ── NOW 라인 (harugyeol .now-line) ────────────────────────────────────
  // 오늘 보기일 때만 표시한다. today 는 서버가 boundary_hour 기준으로 계산해
  // body[data-today] 에 심어준 값이라, 새벽 시간대(06시 이전)도 올바르게
  // "어제"로 취급된다.
  var grid = document.getElementById("grid");
  var nowLine = document.getElementById("now-line");
  var nowLineDot = document.getElementById("now-line-dot");
  var nowTimeChip = document.getElementById("now-time-chip");
  var todayBoundary = body.dataset.today;

  function updateNowLine(now) {
    if (!nowLine || !grid) return;
    if (day !== todayBoundary) {
      nowLine.hidden = true;
      if (nowTimeChip) nowTimeChip.hidden = true;
      return;
    }
    var row = grid.querySelector('.grid-row[data-hour="' + now.getHours() + '"]');
    if (!row) {
      nowLine.hidden = true;
      if (nowTimeChip) nowTimeChip.hidden = true;
      return;
    }
    // .grid-row 는 CSS grid-template-columns 트릭을 위해 display:contents 다 —
    // 자체 박스가 없어 getBoundingClientRect() 가 (0,0) 근처 값을 준다. 세로 위치는
    // 그 안의 실제 셀(.cell, 박스가 있다) 기준으로 계산해야 한다.
    var cells = row.querySelectorAll(".cell");
    if (!cells.length) {
      nowLine.hidden = true;
      return;
    }
    var gridRect = grid.getBoundingClientRect();
    var rowCellRect = cells[0].getBoundingClientRect();
    var slotMinutes = Number(body.dataset.slotMinutes) || 10;
    var colIndex = Math.min(Math.floor(now.getMinutes() / slotMinutes), cells.length - 1);
    var targetCell = cells[colIndex];
    var cellRect = targetCell.getBoundingClientRect();
    var minuteWithinSlot = now.getMinutes() % slotMinutes;
    var progress = minuteWithinSlot / slotMinutes;

    nowLine.style.top = rowCellRect.top - gridRect.top + "px";
    nowLine.style.left = cellRect.left - gridRect.left + cellRect.width * progress + "px";
    nowLine.style.height = rowCellRect.height + "px";
    nowLine.hidden = false;

    if (nowLineDot) nowLineDot.style.left = "0";
    if (nowTimeChip) {
      nowTimeChip.textContent = hhmm(now);
      nowTimeChip.style.top = rowCellRect.top - gridRect.top + rowCellRect.height / 2 - 7 + "px";
      nowTimeChip.hidden = false;
    }
  }

  function updateLiveUi() {
    var now = new Date();
    updateClock(now);
    updateNowLine(now);
  }

  updateLiveUi();
  window.setInterval(updateLiveUi, 1000);
  window.addEventListener("resize", updateLiveUi);

  // ── "지금 집중 중" 카드 (harugyeol .now-card) ─────────────────────────
  // 지어낸 문구는 없다 — plans[] 중 현재 시각이 start_min..end_min 안에 걸친
  // 항목을 실제로 찾아 보여줄 뿐이고, 없으면 카드를 그냥 숨긴다.
  (function renderNowCard() {
    var card = document.getElementById("now-card");
    if (!card || day !== todayBoundary) return;
    var now = new Date();
    var nowMin = now.getHours() * 60 + now.getMinutes();
    var plans = (initialData && initialData.plans) || [];
    var current = null;
    var next = null;
    for (var i = 0; i < plans.length; i++) {
      var p = plans[i];
      if (typeof p.start_min === "number" && typeof p.end_min === "number" && nowMin >= p.start_min && nowMin < p.end_min) {
        current = p;
        break;
      }
      if (typeof p.start_min === "number" && p.start_min > nowMin && (!next || p.start_min < next.start_min)) next = p;
    }
    var focus = current || next;
    if (!focus) return;

    card.hidden = false;
    var titleEl = document.getElementById("now-card-title");
    var subEl = document.getElementById("now-card-sub");
    var timeEl = document.getElementById("now-card-time");
    var kickerEl = document.getElementById("now-card-kicker");
    if (titleEl) titleEl.textContent = focus.title || "";
    if (timeEl) timeEl.textContent = focus.time_range || "";
    if (kickerEl) kickerEl.textContent = current ? "지금 집중 중" : "다음 계획";
    if (subEl) {
      if (current) {
        subEl.textContent = focus.category ? labelOf(focus.category) : "계획한 시간";
      } else {
        var waitMin = Math.max(1, focus.start_min - nowMin);
        subEl.textContent = waitMin + "분 뒤" + (focus.category ? " · " + labelOf(focus.category) : "");
      }
    }

    var checkBtn = document.getElementById("now-card-check");
    if (checkBtn) {
      if (!current) {
        checkBtn.hidden = true;
      } else if (current.checked) {
        checkBtn.disabled = true;
        checkBtn.textContent = "완료됨";
      } else {
        checkBtn.addEventListener("click", function () {
          api("/api/plan/" + current.id + "/check", {
            method: "POST",
            body: JSON.stringify({ day: day, checked: true }),
          })
            .then(function () {
              softReload();
            })
            .catch(function (err) {
              window.alert("체크 실패: " + err.message);
            });
        });
      }
    }
  })();

  if (readOnly) return; // 나머지는 전부 쓰기 동작이다.

  // ── 공통 fetch 헬퍼 ──────────────────────────────────────────────────
  // ★ 앱이 `/planner` 아래에 mount 될 수 있다(터널·Cloudflare Access 를 경로 하나로
  //   막기 위한 접두사). 절대 경로로 부르면 그 밖으로 나가 404 가 난다.
  var BASE = (function () {
    var el = document.getElementById("app-base");
    return (el && el.dataset.base) || "";
  })();

  function api(url, options) {
    if (url.charAt(0) === "/") url = BASE + url;
    options = options || {};
    options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    return fetch(url, options).then(function (res) {
      return res
        .json()
        .catch(function () {
          return {};
        })
        .then(function (payload) {
          if (!res.ok) {
            var msg = (payload && payload.message) || res.status + " " + res.statusText;
            throw new Error(msg);
          }
          return payload;
        });
    });
  }

  // ── 계획 체크 토글 ───────────────────────────────────────────────────
  document.querySelectorAll(".check-input").forEach(function (el) {
    el.addEventListener("change", function () {
      var id = el.dataset.planId;
      var checked = el.checked;
      api("/api/plan/" + id + "/check", {
        method: "POST",
        body: JSON.stringify({ day: day, checked: checked }),
      }).catch(function (err) {
        el.checked = !checked;
        window.alert("체크 실패: " + err.message);
      });
    });
  });

  // ── 오늘만 건너뛰기 ──────────────────────────────────────────────────
  document.querySelectorAll(".btn-skip").forEach(function (el) {
    el.addEventListener("click", function () {
      var id = el.dataset.planId;
      if (!window.confirm("오늘만 이 계획을 건너뛸까요?")) return;
      api("/api/plan/" + id + "/skip", { method: "POST", body: JSON.stringify({ day: day }) })
        .then(function () {
          softReload();
        })
        .catch(function (err) {
          window.alert("건너뛰기 실패: " + err.message);
        });
    });
  });

  // ── 삭제 (되돌릴 수 없다 — 반드시 확인) ─────────────────────────────
  document.querySelectorAll(".btn-delete").forEach(function (el) {
    el.addEventListener("click", function () {
      var id = el.dataset.planId;
      var title = el.dataset.planTitle || "";
      if (!window.confirm('"' + title + '" 계획을 삭제할까요? 되돌릴 수 없습니다.')) return;
      api("/api/plan/" + id, { method: "DELETE" })
        .then(function () {
          softReload();
        })
        .catch(function (err) {
          window.alert("삭제 실패: " + err.message);
        });
    });
  });

  // ── 계획 추가 폼 ─────────────────────────────────────────────────────
  var modeRadios = document.querySelectorAll('input[name="mode"]');
  var weekdayPicker = document.getElementById("weekday-picker");
  var dayPicker = document.getElementById("day-picker");

  function syncMode() {
    var mode = document.querySelector('input[name="mode"]:checked');
    var isOneoff = !!mode && mode.value === "oneoff";
    if (weekdayPicker) weekdayPicker.hidden = isOneoff;
    if (dayPicker) dayPicker.hidden = !isOneoff;
  }
  modeRadios.forEach(function (r) {
    r.addEventListener("change", syncMode);
  });
  syncMode();

  document.querySelectorAll(".preset").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var days = btn.dataset.preset.split("");
      document.querySelectorAll('input[name="weekday"]').forEach(function (cb) {
        cb.checked = days.indexOf(cb.value) !== -1;
      });
    });
  });

  var planForm = document.getElementById("plan-form");
  var planFormError = document.getElementById("plan-form-error");
  var planSummary = document.getElementById("plan-form-summary");
  var planSubmit = document.getElementById("plan-submit");
  var planCancel = document.getElementById("plan-cancel");
  var addPlanBox = document.getElementById("add-plan");

  // ── 수정 ─────────────────────────────────────────────────────────────
  // ★ 추가 폼을 그대로 재사용한다. 수정용 폼을 따로 만들면 "10분 단위" 같은 규칙이
  //   두 벌이 되고 언젠가 갈라진다(반복 실패 2번). 여기서는 값을 채우고 보내는
  //   주소만 POST → PATCH 로 바꾼다.
  // ── 시간 = 셀렉트 두 개 ──────────────────────────────────────────────
  // ★ `<input type="time">` 을 버린 이유는 planner.html 주석에 있다.
  //   여기서는 "HH:MM 문자열 ↔ 셀렉트 두 개" 변환만 한 곳에서 한다.
  var SLOT_MIN = 10;

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function snapMinute(m) {
    // 옛 데이터에 09:13 같은 값이 남아 있어도 **열리게** 한다. 못 고르는 값이
    // 셀렉트에 들어가면 브라우저가 첫 항목으로 되돌려 놓아 조용히 시각이 바뀐다.
    var v = Math.round(m / SLOT_MIN) * SLOT_MIN;
    return Math.min(50, Math.max(0, v));
  }

  function setTimeFields(prefix, hhmm) {
    if (!planForm) return;
    var hEl = planForm.elements[prefix + "_h"];
    var mEl = planForm.elements[prefix + "_m"];
    if (!hEl || !mEl) return;
    var parts = String(hhmm || "").split(":");
    if (parts.length !== 2) return;
    var h = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    if (isNaN(h) || isNaN(m)) return;
    hEl.value = pad2(Math.min(23, Math.max(0, h)));
    mEl.value = pad2(snapMinute(m));
  }

  function readTimeFields(fd, prefix) {
    var h = fd.get(prefix + "_h");
    var m = fd.get(prefix + "_m");
    return h && m ? h + ":" + m : "";
  }

  function resetPlanForm() {
    if (!planForm) return;
    planForm.reset();
    planForm.elements.plan_id.value = "";
    if (planSummary) planSummary.textContent = "계획 추가";
    if (planSubmit) planSubmit.textContent = "계획 저장";
    if (planCancel) planCancel.hidden = true;
    syncMode();
  }

  function fillPlanForm(d) {
    if (!planForm) return;
    planForm.elements.plan_id.value = d.planId;
    planForm.elements.title.value = d.title || "";
    planForm.elements.category.value = d.category || "";
    setTimeFields("start", d.start);
    setTimeFields("end", d.end);
    var oneoff = d.kind === "oneoff";
    document.querySelectorAll('input[name="mode"]').forEach(function (r) {
      r.checked = (r.value === "oneoff") === oneoff;
    });
    if (oneoff && d.day) planForm.elements.day.value = d.day;
    var wd = d.weekdays || "";
    document.querySelectorAll('input[name="weekday"]').forEach(function (cb) {
      cb.checked = wd.indexOf(cb.value) !== -1;
    });
    syncMode();
    if (planSummary) planSummary.textContent = "계획 수정 — " + (d.title || "");
    if (planSubmit) planSubmit.textContent = "수정 저장";
    if (planCancel) planCancel.hidden = false;
    if (addPlanBox) addPlanBox.open = true;
    // ★ 부드러운 스크롤 + 자동 포커스를 **폰에서 같이 하면 화면이 요동친다.**
    //   `focus()` 가 키보드를 띄우면 뷰포트가 줄고, 브라우저가 포커스된 칸을
    //   보이게 하려고 다시 스크롤하는데, 그때 smooth 애니메이션이 아직 돌고 있어
    //   둘이 서로를 밀어낸다(사용자 보고: "수정하려 하면 화면이 엄청 반짝거린다").
    //
    //   그래서 스크롤은 **즉시**로 하고, 자동 포커스는 **마우스가 있는 기기에서만**
    //   한다. 폰에서는 사용자가 칸을 직접 누르는 편이 빠르고 조용하다.
    planForm.scrollIntoView({ block: "nearest" });
    var finePointer = window.matchMedia && window.matchMedia("(hover: hover)").matches;
    if (finePointer) planForm.elements.title.focus();
  }

  document.querySelectorAll(".btn-edit").forEach(function (el) {
    el.addEventListener("click", function () {
      fillPlanForm({
        planId: el.dataset.planId,
        title: el.dataset.planTitle,
        category: el.dataset.planCategory,
        start: el.dataset.planStart,
        end: el.dataset.planEnd,
        kind: el.dataset.planKind,
        weekdays: el.dataset.planWeekdays,
        day: el.dataset.planDay,
      });
    });
  });

  if (planCancel) planCancel.addEventListener("click", resetPlanForm);

  function showFormError(msg) {
    if (!planFormError) return;
    planFormError.hidden = false;
    planFormError.textContent = msg;
  }

  if (planForm) {
    planForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (planFormError) {
        planFormError.hidden = true;
        planFormError.textContent = "";
      }
      var fd = new FormData(planForm);
      var mode = fd.get("mode");
      var reqBody = {
        title: fd.get("title"),
        category: fd.get("category") || null,
        start: readTimeFields(fd, "start"),
        end: readTimeFields(fd, "end"),
      };
      if (!reqBody.start || !reqBody.end) {
        showFormError("시작/종료 시각을 입력하세요.");
        return;
      }
      if (mode === "oneoff") {
        reqBody.day = fd.get("day");
        reqBody.kind = "oneoff";
      } else {
        var weekdays = fd.getAll("weekday").join("");
        if (!weekdays) {
          showFormError("반복 요일을 하나 이상 고르세요.");
          return;
        }
        reqBody.weekdays = weekdays;
        reqBody.kind = "recurring";
      }
      // 수정이면 같은 본문을 PATCH 로 보낸다.
      var editing = planForm.elements.plan_id.value;
      var url = editing ? "/api/plan/" + encodeURIComponent(editing) : "/api/plan";
      api(url, { method: editing ? "PATCH" : "POST", body: JSON.stringify(reqBody) })
        .then(function () {
          softReload();
        })
        .catch(function (err) {
          showFormError(err.message);
        });
    });
  }

  // ── 격자: 드래그(데스크톱) / 탭-탭(모바일) 범위 선택 ────────────────
  // grid 는 위(NOW 라인 절)에서 이미 선언했다 — 여기서는 재사용만 한다.
  var caption = document.getElementById("selection-caption");
  var chooser = document.getElementById("category-chooser");
  var chooserTitle = document.getElementById("chooser-title");
  var chooserOptions = document.getElementById("chooser-options");
  var chooserClear = document.getElementById("chooser-clear");
  var chooserCancel = document.getElementById("chooser-cancel");
  var chooserPurge = document.getElementById("chooser-purge");

  var pendingStart = null; // 탭-탭 모드: 첫 탭에서 기록한 슬롯
  var dragStart = null; // pointerdown 시점의 슬롯
  var dragging = false; // 실제로 움직였는지 (그냥 탭과 구분하기 위함)
  var downPoint = null;

  function cellAt(slot) {
    return grid.querySelector('.cell[data-slot="' + slot + '"]');
  }

  function clearHighlights() {
    grid.querySelectorAll(".cell.pending-start, .cell.in-selection").forEach(function (c) {
      c.classList.remove("pending-start", "in-selection");
    });
  }

  function highlightRange(a, b) {
    clearHighlights();
    var lo = Math.min(a, b),
      hi = Math.max(a, b);
    for (var s = lo; s <= hi; s++) {
      var c = cellAt(s);
      if (c) c.classList.add("in-selection");
    }
  }

  // labelOf 는 위(eyebrow/now-card 절)에서 이미 선언했다.

  function setCaption(text) {
    if (caption) caption.textContent = text || " ";
  }

  // 고르개에 실리는 **구조 상태**. 활동과 색 변수 이름이 다르다.
  var STRUCT_OPTIONS = { sleep: 1 };

  /* 지금 보고 있는 층. 차트 모듈에도 같은 이름의 함수가 있지만 그건 다른 IIFE 안이라
     여기서 못 쓴다 — **둘 다 같은 원본(격자의 클래스)을 읽으므로** 값은 어긋나지 않는다. */
  function viewingPlan() {
    return !!grid && grid.classList.contains("show-plan");
  }

  function colorVarOf(catId) {
    return STRUCT_OPTIONS[catId] ? "var(--structural-" + catId + ")" : "var(--cat-" + catId + ")";
  }

  function openChooser(startSlot, endSlot) {
    var lo = Math.min(startSlot, endSlot),
      hi = Math.max(startSlot, endSlot);
    highlightRange(lo, hi);
    /* ★ 고르개는 **보고 있는 층**을 고친다 (2026-09-05).
         계획 ON 이면 계획을, OFF 면 실제 보정을 바꾼다. 같은 버튼이 화면에 안 보이는
         층을 고치면 사람이 무엇을 바꿨는지 모른다 — 토글이 이미 어느 층인지 말하고
         있으니 그걸 따른다. */
    var plan = viewingPlan();
    // ★ `textContent` 라 마크다운이 안 먹는다 — `**계획**` 이 별표째로 찍혔다.
    chooserTitle.textContent = plan
      ? "계획 " + (hi - lo + 1) + "칸 — 하기로 한 것을 정합니다. 실제 기록은 안 바뀝니다."
      : "선택: " + (hi - lo + 1) + "칸 — 카테고리를 고르거나, 보정을 해제하거나, 기록을 지웁니다.";
    chooserClear.textContent = plan ? "계획 지우기" : "보정 해제";
    // 계획 층에서는 "기록 지우기" 를 감춘다 — 계획을 보면서 실제 기록을 지우는 것은
    // 화면에 안 보이는 것을 지우는 일이다.
    if (chooserPurge) chooserPurge.hidden = plan;
    chooserOptions.innerHTML = "";
    // ★ 활동 9개 + **수면**. 수면은 활동이 아니라 구조 상태지만 고를 수 있어야 한다 —
    //   추정(자리비움·결측 3시간)이 못 잡는 낮잠·중간에 깬 밤을 사람이 여기서 고친다.
    //   보정은 추정을 이기고, 오늘의 수면 합계도 이 보정을 같이 센다.
    (palette.order || []).concat(["sleep"]).forEach(function (catId) {
      var opt = document.createElement("button");
      opt.type = "button";
      opt.className = "chooser-option";
      var swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = colorVarOf(catId);
      if (STRUCT_OPTIONS[catId]) opt.classList.add("chooser-option-structural");
      opt.appendChild(swatch);
      opt.appendChild(document.createTextNode(labelOf(catId)));
      opt.addEventListener("click", function () {
        if (viewingPlan()) applyPlan(lo, hi + 1, catId);
        else applyOverride(lo, hi + 1, catId);
      });
      chooserOptions.appendChild(opt);
    });
    chooser.hidden = false;
    chooser.dataset.lo = String(lo);
    chooser.dataset.hi = String(hi);
  }

  function closeChooser() {
    chooser.hidden = true;
    pendingStart = null;
    clearHighlights();
    setCaption("");
  }

  function applyOverride(startSlot, endSlot, category) {
    api("/api/slot", {
      method: "POST",
      body: JSON.stringify({ day: day, start_slot: startSlot, end_slot: endSlot, category: category }),
    })
      .then(function () {
        softReload();
      })
      .catch(function (err) {
        window.alert("보정 실패: " + err.message);
      });
  }

  /* 계획을 그 구간에 적는다. 취소는 `clearPlan` 이고, 지우면 그 시간은 다시
     실제 기록만 남는다 — 계획은 **겹쳐 그리는 층**이지 기록을 바꾸지 않는다. */
  function applyPlan(startSlot, endSlot, category) {
    api("/api/plan-slot", {
      method: "POST",
      body: JSON.stringify({ day: day, start_slot: startSlot, end_slot: endSlot, category: category }),
    })
      .then(softReload)
      .catch(function (err) {
        window.alert("계획 지정 실패: " + err.message);
      });
  }

  function clearPlan(startSlot, endSlot) {
    api("/api/plan-slot", {
      method: "DELETE",
      body: JSON.stringify({ day: day, start_slot: startSlot, end_slot: endSlot }),
    })
      .then(softReload)
      .catch(function (err) {
        window.alert("계획 지우기 실패: " + err.message);
      });
  }

  function clearOverride(startSlot, endSlot) {
    api("/api/slot", {
      method: "DELETE",
      body: JSON.stringify({ day: day, start_slot: startSlot, end_slot: endSlot }),
    })
      .then(function () {
        softReload();
      })
      .catch(function (err) {
        window.alert("보정 해제 실패: " + err.message);
      });
  }

  if (chooserClear) {
    chooserClear.addEventListener("click", function () {
      var lo = Number(chooser.dataset.lo),
        hi = Number(chooser.dataset.hi);
      if (viewingPlan()) clearPlan(lo, hi + 1);
      else clearOverride(lo, hi + 1);
    });
  }
  if (chooserCancel) {
    chooserCancel.addEventListener("click", closeChooser);
  }

  /* ── 블럭 단위 삭제 (2026-09-05) ────────────────────────────────────
     전에는 "지금부터 N분" 뿐이라 **어제 오후의 그 한 칸**을 지울 방법이 없었다.
     좌표는 슬롯으로 보낸다 — 격자가 슬롯으로 말하고 슬롯→시각 변환은 서버에
     이미 한 곳 있다. 여기서 epoch 를 계산하면 그 변환이 두 곳이 된다. */
  if (chooserPurge) {
    chooserPurge.addEventListener("click", function () {
      var lo = Number(chooser.dataset.lo),
        hi = Number(chooser.dataset.hi);
      var cells = hi - lo + 1;
      // ★ 확인을 받는다. 되돌릴 수는 있지만(휴지통) 사람을 놀라게 하면 안 된다.
      if (!window.confirm(
            cells + "칸(" + cells * slotMinutes() + "분)의 기록을 지웁니다.\n" +
            "앱 이름·창 제목이 화면과 집계에서 사라집니다. 되돌릴 수 있습니다.")) {
        return;
      }
      api("/api/private/purge", {
        method: "POST",
        body: JSON.stringify({ day: day, start_slot: lo, end_slot: hi + 1, confirm: true }),
      })
        .then(function (res) {
          closeChooser();
          // ★ **순서가 중요하다.** `softReload()` 는 `location.reload()` 라서
          //   클라이언트 상태가 통째로 날아간다 — 먼저 띄우면 바가 바로 사라진다.
          //   그래서 새로고침을 **건너게** 넘긴다 (스크롤 복원과 같은 방식).
          rememberUndo((res && res.events) || 0);
          softReload();
        })
        .catch(function (err) {
          window.alert("삭제 실패: " + err.message);
        });
    });
  }

  function slotMinutes() {
    return Number(document.body.dataset.slotMinutes) || 10;
  }

  /* 삭제 직후에만 뜨는 되돌리기.

     삭제 → `location.reload()` 라서 **새로고침을 건너야** 한다. 스크롤 복원이 쓰는
     것과 같은 `sessionStorage` 방식이다. 오래된 표식은 안 띄운다 — 어제 지운 것에
     대고 "되돌리기" 를 보여주면 그건 다른 삭제를 되돌린다. */
  var UNDO_KEY = "lt-undo-offer";
  var UNDO_TTL_MS = 30000;

  function rememberUndo(events) {
    try {
      sessionStorage.setItem(UNDO_KEY, JSON.stringify({ events: events, at: Date.now() }));
    } catch (e) {
      /* 저장을 못 해도 삭제는 이미 성립했다 */
    }
  }

  (function showUndoAfterReload() {
    var bar = document.getElementById("undo-bar");
    if (!bar) return;
    var raw = null;
    try {
      raw = sessionStorage.getItem(UNDO_KEY);
      sessionStorage.removeItem(UNDO_KEY);   // 한 번만 띄운다
    } catch (e) {
      return;
    }
    if (!raw) return;
    var info;
    try {
      info = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!info || Date.now() - (info.at || 0) > UNDO_TTL_MS) return;
    bar.querySelector("#undo-text").textContent = "기록 " + info.events + "건을 지웠습니다.";
    bar.hidden = false;
    window.setTimeout(function () {
      bar.hidden = true;
    }, 15000);
  })();

  /* ── 기록 목록에서도 고치고 지운다 (2026-09-05) ──────────────────────
     격자에서만 되면 "이 줄" 을 고치려고 **격자에서 그 칸을 다시 찾아야 한다.**
     목록은 이미 슬롯 번호를 들고 있다(`.activity-marker` 의 data-slot-*) —
     같은 고르개를 그대로 연다. 좌표를 다시 계산하지 않는다.

     여는 방법이 둘인 이유: 마우스는 **우클릭**, 손가락은 **꾹 누르기**.
     탭은 원래 하던 일(격자에서 그 구간을 비춘다)을 그대로 둔다. */
  var LONG_PRESS_MS = 500;
  var LONG_PRESS_SLOP = 10;   // 이만큼 움직이면 스크롤이지 길게 누르기가 아니다

  function slotsOfItem(item) {
    var m = item.querySelector(".activity-marker");
    if (!m) return null;
    var a = Number(m.dataset.slotStart),
      b = Number(m.dataset.slotEnd);
    return isNaN(a) || isNaN(b) ? null : [a, b];
  }

  document.querySelectorAll(".activity-item").forEach(function (item) {
    function openFor(ev) {
      var r = slotsOfItem(item);
      if (!r) return;
      if (ev && ev.cancelable) ev.preventDefault();
      openChooser(r[0], r[1]);
    }

    item.addEventListener("contextmenu", openFor);

    var timer = null,
      from = null;
    function cancel() {
      if (timer) window.clearTimeout(timer);
      timer = null;
      from = null;
      item.classList.remove("pressing");
    }
    item.addEventListener("pointerdown", function (ev) {
      if (ev.pointerType === "mouse") return;   // 마우스에는 우클릭이 있다
      from = [ev.clientX, ev.clientY];
      item.classList.add("pressing");
      timer = window.setTimeout(function () {
        timer = null;
        item.classList.remove("pressing");
        openFor(null);
      }, LONG_PRESS_MS);
    });
    item.addEventListener("pointermove", function (ev) {
      // ★ 목록은 스크롤된다. 손가락이 움직이면 **스크롤 의도**로 본다 —
      //   안 그러면 목록을 넘길 때마다 고르개가 튀어나온다.
      if (!from) return;
      if (Math.abs(ev.clientX - from[0]) > LONG_PRESS_SLOP ||
          Math.abs(ev.clientY - from[1]) > LONG_PRESS_SLOP) cancel();
    });
    item.addEventListener("pointerup", cancel);
    item.addEventListener("pointercancel", cancel);
    item.addEventListener("pointerleave", cancel);
  });

  /* ── 상시 되돌리기 (2026-09-07) ──────────────────────────────────
     삭제 직후 15초짜리 바만 있었다. 그 사이를 놓치면 되돌리는 길이 화면에서
     사라져서, 되돌리기가 있는데 **없는 것과 같았다.**
     휴지통에 되돌릴 것이 있으면 늘 보인다. */
  (function showUndoLine() {
    var line = document.getElementById("undo-line");
    if (!line) return;
    var info = null;
    try {
      var el = document.getElementById("lt-initial-data");
      info = el && JSON.parse(el.textContent).undoable;
    } catch (e) {
      return;
    }
    if (!info || !info.span_id) return;

    var when = "";
    if (info.start_ts) {
      var d = new Date(info.start_ts * 1000), e2 = new Date(info.end_ts * 1000);
      var hm = function (x) {
        return ("0" + x.getHours()).slice(-2) + ":" + ("0" + x.getMinutes()).slice(-2);
      };
      when = (d.getMonth() + 1) + "/" + d.getDate() + " " + hm(d) + "–" + hm(e2);
    }
    document.getElementById("undo-line-text").innerHTML =
      "지운 기록이 있습니다 — <strong>" + when + " · " + info.events + "건</strong>" +
      (info.trash > info.events ? " (휴지통 " + info.trash + "건)" : "");
    line.hidden = false;

    document.getElementById("undo-line-button").addEventListener("click", function () {
      api("/api/private/undo", { method: "POST", body: JSON.stringify({}) })
        .then(softReload)
        .catch(function (err) { window.alert("되돌리기 실패: " + err.message); });
    });
  })();

  var undoBtn = document.getElementById("undo-button");
  if (undoBtn) {
    undoBtn.addEventListener("click", function () {
      api("/api/private/undo", { method: "POST", body: JSON.stringify({}) })
        .then(function () {
          document.getElementById("undo-bar").hidden = true;
          softReload();
        })
        .catch(function (err) {
          window.alert("되돌리기 실패: " + err.message);
        });
    });
  }

  if (grid) {
    /* ★ 격자에서도 우클릭 (2026-09-05). 드래그는 여러 칸을 고를 때고, **한 칸만**
       고치려고 드래그를 시작했다 끝내는 건 번거롭다. 우클릭은 그 칸 하나를 연다.
       (안드로이드 크롬은 꾹 누르기가 contextmenu 를 쏘므로 거기서도 열린다.) */
    grid.addEventListener("contextmenu", function (ev) {
      var cell = ev.target.closest(".cell");
      if (!cell) return;
      var slot = Number(cell.dataset.slot);
      if (isNaN(slot)) return;
      ev.preventDefault();
      pendingStart = null;        // 반쯤 고르던 상태가 남아 있으면 다음 탭이 엉뚱해진다
      openChooser(slot, slot);
    });

    grid.addEventListener("pointerdown", function (ev) {
      var cell = ev.target.closest(".cell");
      if (!cell) return;
      pendingStart = null; // 드래그를 새로 시작하면 탭-탭 상태는 버린다
      downPoint = { x: ev.clientX, y: ev.clientY };
      dragStart = Number(cell.dataset.slot);
      dragging = false;
      highlightRange(dragStart, dragStart);
    });

    grid.addEventListener("pointermove", function (ev) {
      if (dragStart === null || downPoint === null) return;
      var dx = ev.clientX - downPoint.x,
        dy = ev.clientY - downPoint.y;
      if (!dragging && Math.hypot(dx, dy) > 8) dragging = true;
      if (!dragging) return;
      var el = document.elementFromPoint(ev.clientX, ev.clientY);
      var cell = el && el.closest && el.closest(".cell");
      if (cell) {
        var slot = Number(cell.dataset.slot);
        highlightRange(dragStart, slot);
        setCaption(
          labelOf(cell.dataset.category) +
            " (" +
            Math.min(dragStart, slot) +
            "~" +
            Math.max(dragStart, slot) +
            "칸)"
        );
      }
    });

    grid.addEventListener("pointerup", function (ev) {
      var cell = ev.target.closest(".cell");
      if (dragStart === null) return;

      if (dragging) {
        var el = document.elementFromPoint(ev.clientX, ev.clientY);
        var endCell = (el && el.closest && el.closest(".cell")) || cell;
        var endSlot = endCell ? Number(endCell.dataset.slot) : dragStart;
        openChooser(dragStart, endSlot);
      } else if (cell) {
        var slot = Number(cell.dataset.slot);
        setCaption(labelOf(cell.dataset.category) + " · " + slot + "번 칸");
        if (pendingStart === null) {
          pendingStart = slot;
          clearHighlights();
          cell.classList.add("pending-start");
        } else {
          var start = pendingStart;
          pendingStart = null;
          openChooser(start, slot);
        }
      }
      dragStart = null;
      downPoint = null;
      dragging = false;
    });
  }

  // ── 오늘의 기록 → 격자로 잇기 ─────────────────────────────────────────
  //
  // 기록 목록의 점을 누르면 격자에서 **그 시간대가 반짝인다.**
  // 목록은 "무엇을 했나", 격자는 "언제였나" 를 보여 주는데 둘이 따로 놀았다.
  //
  // ★ 슬롯 번호는 서버가 실어 준다(`data-slot-start/end`). 시각 문자열에서
  //   클라이언트가 다시 계산하면 하루 경계(06:00) 규칙이 두 벌이 된다.
  var FLASH_MS = 1400;
  var flashTimer = null;

  function clearFlash() {
    if (!grid) return;
    grid.querySelectorAll(".cell.flash").forEach(function (c) {
      c.classList.remove("flash");
      c.style.removeProperty("--flash-shadow");
    });
  }

  document.querySelectorAll(".activity-marker").forEach(function (marker) {
    marker.addEventListener("click", function () {
      if (!grid) return;
      var from = Number(marker.dataset.slotStart);
      var to = Number(marker.dataset.slotEnd);
      if (isNaN(from) || isNaN(to)) return;

      clearFlash();
      if (flashTimer) clearTimeout(flashTimer);

      // ★ 이어진 칸 **사이**에는 선을 긋지 않는다. 한 덩어리로 보여야
      //   "이 활동이 이만큼 이어졌다" 가 읽힌다 — 칸마다 테두리를 두르면
      //   같은 활동이 여섯 조각으로 잘려 보인다.
      //
      //   격자는 한 줄에 `cols` 칸(60분 / slot_minutes)이라 이웃은:
      //     좌 s-1 (줄 안일 때) · 우 s+1 (줄 안일 때) · 위 s-cols · 아래 s+cols
      var slotMin = Number(body.dataset.slotMinutes) || 10;
      var cols = Math.max(1, Math.round(60 / slotMin));
      var C = "var(--ink-primary)";

      var first = null;
      grid.querySelectorAll(".cell[data-slot]").forEach(function (cell) {
        var s = Number(cell.dataset.slot);
        if (s < from || s > to) return;

        var sides = [];
        if (s - cols < from) sides.push("inset 0 1px 0 0 " + C);
        if (s + cols > to) sides.push("inset 0 -1px 0 0 " + C);
        if (s % cols === 0 || s - 1 < from) sides.push("inset 1px 0 0 0 " + C);
        if (s % cols === cols - 1 || s + 1 > to) sides.push("inset -1px 0 0 0 " + C);

        cell.style.setProperty("--flash-shadow", sides.length ? sides.join(",") : "none");
        cell.classList.add("flash");
        if (!first) first = cell;
      });
      if (!first) return;

      // 격자는 세로로 길어 화면 밖일 때가 많다. 반짝이기 전에 보이게 한다.
      first.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
      flashTimer = setTimeout(clearFlash, FLASH_MS);
    });
  });
})();

/* ── 계획 보기 토글 (2026-08-25) ────────────────────────────────────────
   격자가 "한 일"과 "하기로 한 일" 둘 다를 이미 들고 있다(`--cell-color`,
   `--plan-color`). 여기서는 클래스 하나만 붙였다 뗀다 — 서버를 다시 부르지
   않으므로 즉시 바뀌고, 두 값이 같은 응답에서 왔으므로 시점이 어긋날 수 없다.

   선택은 기억한다. 계획을 짜는 사람은 계획 보기를 켠 채로 여러 날을 넘긴다. */
(function () {
  var KEY = "lt-plan-view";
  var grid = document.getElementById("grid");
  var btn = document.getElementById("plan-toggle");
  var text = document.getElementById("plan-toggle-text");
  if (!grid || !btn || !text) return;

  var hint = document.getElementById("grid-hint");

  function apply(on) {
    grid.classList.toggle("show-plan", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    // ★ "계획 OFF" 는 **상태인지 동작인지 모호하다** — 끄라는 버튼인지 꺼져 있다는
    //   표시인지. 켜졌을 때만 상태를 말하고, 꺼져 있을 때는 할 일을 말한다.
    text.textContent = on ? "계획 보는 중" : "계획 보기";
    btn.title = on
      ? "지금은 하기로 한 것을 봅니다. 끄면 실제로 한 것이 보입니다"
      : "켜면 하기로 한 것이 그 시간 위에 겹쳐 보입니다";
    // ★ 안내문도 층을 따라간다. 계획을 보면서 "활동을 보정할 수 있습니다" 라고
    //   적혀 있으면, 눌렀을 때 무엇이 바뀌는지 말과 결과가 어긋난다.
    if (hint) {
      hint.textContent = on
        ? "칸을 골라 그 시간에 하기로 한 것을 정합니다. 실제 기록은 안 바뀝니다."
        : "드래그하거나 모바일에서 시작·끝 칸을 차례로 눌러 활동을 보정할 수 있습니다. 우클릭·꾹 누르기도 됩니다.";
    }
  }

  var saved = null;
  try {
    saved = localStorage.getItem(KEY);
  } catch (e) {
    saved = null;
  }
  apply(saved === "on");

  btn.addEventListener("click", function () {
    var next = btn.getAttribute("aria-pressed") !== "true";
    apply(next);
    try {
      localStorage.setItem(KEY, next ? "on" : "off");
    } catch (e) {
      /* 무시 — 토글 자체는 동작한다 */
    }
  });
})();

/* ── 보기 전환: 격자 ↔ 차트 ↔ 원그래프 (2026-09-01) ──────────────────────
   ★ **격자의 칸을 세지 않는다.** 처음엔 그렇게 짰다 — "같은 DOM 을 세면 두 뷰가
     어긋날 수 없다" 는 생각이었는데, 틀렸다. 칸은 그 슬롯의 **승자 하나**만
     들고 있어서 "코딩 7분 + 웹 3분" 이 "코딩 10분" 이 된다. 실제로 그렇게 쟀더니
     상단 '실제 활동' 과 42분이 어긋났다.
       집계 원천은 `slot_breakdown` 이고 `slot` 의 winner-takes-all 은 시각화
       전용이다 (life-trainer/CLAUDE.md §기본값).
     그래서 서버가 이미 그 원천에서 뽑아 둔 `stats.by_category` 를 쓴다 —
     상단 지표·리포트·`lt stats` 와 같은 값이다.

   ★ 계획 보기(ON)는 다르다. 계획은 슬롯 구간을 통째로 차지하므로 칸을 세는 것이
     정확하다 — 승자 다툼이 없다. 그래서 그때만 `data-plan-category` 를 센다.

   ★ 구조 상태(off/away/unknown)는 서버가 이미 뺐다(away/off). 계획 보기의
     칸 세기에서도 같은 기준으로 뺀다. */
(function () {
  var VIEW_KEY = "lt-day-view";
  // 구조 상태 목록도 팔레트에서 온다 — `colorVar()` 가 `--cat-*` 대신
  // `--structural-*` 를 내게 하려면 이 판정이 맞아야 한다.
  var STRUCTURAL = {};

  // 라벨은 위 모듈의 labelOf 를 못 쓴다(IIFE 안에 갇혀 있다). 같은 원본을
  // 다시 읽는다 — 값을 두 번 **계산**하는 게 아니라 한 원본을 두 번 **읽는** 것이다.
  var labels = { order: [], labels: {}, structural: {}, structural_labels: {} };
  try {
    var el = document.getElementById("lt-initial-data");
    labels = (el && JSON.parse(el.textContent).palette) || labels;
  } catch (e) {
    /* 라벨이 없어도 카테고리 id 로 그린다 */
  }
  Object.keys(labels.structural || {}).forEach(function (k) {
    STRUCTURAL[k] = 1;
  });
  var structLabels = (labels && labels.structural_labels) || {};
  function labelOf(cat) {
    return (labels.labels && labels.labels[cat]) || structLabels[cat] || cat;
  }

  var grid = document.getElementById("grid");
  var scroll = document.getElementById("grid-scroll");
  var panel = document.getElementById("chart-panel");
  var barBox = document.getElementById("chart-bar");
  var donutBox = document.getElementById("chart-donut");
  var emptyMsg = document.getElementById("chart-empty");
  var caption = document.getElementById("chart-caption");
  var btns = document.querySelectorAll("[data-view-choice]");
  if (!grid || !scroll || !panel || !barBox || !donutBox || !btns.length) return;

  // 한 칸이 몇 분인가. 격자 열 수에서 얻는다 — slots_per_day 를 여기 박아두면
  // 설정이 바뀔 때 조용히 틀린 시간이 나온다. (계획 보기에서만 쓴다)
  function minutesPerCell() {
    var cols = parseInt(getComputedStyle(grid).getPropertyValue("--grid-cols"), 10);
    return cols > 0 ? 60 / cols : 10;
  }

  // 실제 활동의 집계 원천. 서버가 slot_breakdown 에서 뽑아 둔 것을 그대로 쓴다.
  //
  // ★ `private_sec` 을 따로 읽는다 (2026-09-04). `by_category` 는 정의상
  //   "away/off 제외 = 분류된 시간" 이라 **프라이빗이 들어갈 자리가 없다.**
  //   격자는 서버가 렌더한 슬롯에서 구조 상태를 직접 그리니 보이는데 차트는 그 경로를
  //   안 타서, 프라이빗이 통째로 사라지고 **분모에서도 빠져** 나머지가 부풀었다.
  var byCategory = [];
  var byCategoryPlan = [];
  var privateSec = 0;
  try {
    var d0 = document.getElementById("lt-initial-data");
    var st0 = (d0 && JSON.parse(d0.textContent).stats) || {};
    byCategory = st0.by_category || [];
    byCategoryPlan = st0.by_category_plan || [];
    privateSec = st0.private_sec || 0;
  } catch (e) {
    byCategory = [];
  }

  function planOn() {
    return grid.classList.contains("show-plan");
  }

  /* [{cat, min}] 을 큰 것부터.
     실제 = slot_breakdown 집계(서버) · 계획 = 슬롯 세기(정확하다, 승자 다툼 없음). */
  function tally() {
    var out = [];
    var i;
    /* ★ 계획 보기는 **합쳐서** 센다 (2026-09-05).
       계획이 있던 시간은 계획 카테고리로, 나머지는 실제로 — 격자가 이미 그렇게
       겹쳐 그리므로 차트도 같은 하루를 세게 된다.

       전에는 격자에서 `data-plan-category` 칸만 셌다. 그래서 **계획이 없는 날
       계획 보기를 켜면 차트가 통째로 비었다** — 격자는 그대로 활동을 보여주는데.
       같은 토글이 두 화면에서 다른 뜻이었다.

       ★ 합계는 **서버가 만든다**(`by_category_plan`). 계획 없는 칸은 `slot_breakdown`
         (실측 원천)에서 와야 하는데, 격자 칸은 승자 하나만 들고 있어 "코딩 7분 +
         웹 3분" 이 "코딩 10분" 이 된다 (CLAUDE.md: 집계 원천은 slot_breakdown). */
    var src = planOn() ? byCategoryPlan : byCategory;
    for (i = 0; i < src.length; i++) {
      if (src[i].seconds > 0) out.push({ cat: src[i].category, min: src[i].seconds / 60 });
    }
    // 프라이빗은 활동이 아니지만 **하루에서 빠진 시간**이라 안 그리면 거짓말이 된다.
    // 자리비움·결측은 계속 뺀다 — 그건 "안 한 시간" 이지 "가린 시간" 이 아니다.
    if (privateSec > 0) out.push({ cat: "private", min: privateSec / 60 });
    out.sort(function (a, b) {
      return b.min - a.min;
    });
    return out;
  }

  function colorVar(cat) {
    return STRUCTURAL[cat] ? "var(--structural-" + cat + ")" : "var(--cat-" + cat + ")";
  }

  /* ★ 구조 상태는 **색이 아니라 빗금**으로 갈린다.
     `config/palette.yaml` 이 그렇게 정해 뒀다 — "away 가 135deg 단방향이므로
     private 는 135+45 로, 흑백·색각이상에서도 구분된다."
     프라이빗(#b0aea4)과 미분류(#898781)는 회색끼리라 **색만으로는 안 갈린다.**
     격자는 빗금을 쓰는데 차트만 안 쓰면 같은 하루가 두 화면에서 다르게 읽힌다. */
  function hatchClass(cat) {
    return STRUCTURAL[cat] ? " is-structural structural-" + cat : "";
  }

  // ★ 분을 따로 반올림하면 "13시간 60분" 이 나온다 (13시간 59.93분에서 실제로 봤다).
  //   **먼저 전체를 분으로 반올림**하고 그다음 나눈다.
  function hm(min) {
    var t = Math.round(min);
    var h = Math.floor(t / 60);
    var m = t % 60;
    if (h && m) return h + "시간 " + m + "분";
    if (h) return h + "시간";
    return m + "분";
  }

  /* ★ 막대 길이는 **1위 대비**다. 숫자는 **전체 대비**다 (2026-09-04).
     
     전에는 길이도 전체 대비라, 좁은 화면에서 1위가 30% 면 막대가 트랙의 1/3 만
     차고 나머지는 실선처럼 보였다 — 서로 비교가 안 됐다. 1위를 꽉 채우면 트랙
     전체가 눈금 역할을 해서 2위가 절반인지 1/5 인지가 바로 읽힌다.
     
     길이와 숫자가 다른 것을 재는 셈이라 **숫자를 반드시 같이 적는다.** 퍼센트가
     없으면 1위가 100% 인 줄로 읽힌다 — 그래서 값 칸은 없애면 안 된다. */
  function renderBar(rows, total) {
    var html = "";
    var top = 0;
    for (var k = 0; k < rows.length; k++) if (rows[k].min > top) top = rows[k].min;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var pct = total ? (r.min / total) * 100 : 0;
      var len = top ? (r.min / top) * 100 : 0;
      // 1위 대비 아주 작은 것도 **보이기는 해야** 한다. 0.8% 아래는 선으로 남는다.
      if (len > 0 && len < 1.5) len = 1.5;
      html +=
        '<div class="bar-row">' +
        '<span class="bar-name"><span class="bar-dot' + hatchClass(r.cat) + '" style="background:' + colorVar(r.cat) + '"></span>' +
        labelOf(r.cat) +
        "</span>" +
        '<span class="bar-track"><span class="bar-fill' + hatchClass(r.cat) + '" style="width:' + len.toFixed(1) + "%;background:" + colorVar(r.cat) + '"></span></span>' +
        '<span class="bar-value">' + hm(r.min) + '<small>' + Math.round(pct) + "%</small></span>" +
        "</div>";
    }
    barBox.innerHTML = html;
  }

  /* 원그래프는 SVG 하나에 stroke-dasharray 로 그린다 — 라이브러리 없이,
     색은 격자와 같은 CSS 변수를 그대로 쓴다. */
  function renderDonut(rows, total) {
    var R = 70;
    var C = 2 * Math.PI * R;
    var offset = 0;
    var arcs = "";
    var needHatch = {};
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var frac = total ? r.min / total : 0;
      var len = frac * C;
      // 구조 상태의 호는 패턴으로 칠한다 (위 hatchClass 와 같은 이유).
      var paint = STRUCTURAL[r.cat] ? 'url(#hatch-' + r.cat + ')' : colorVar(r.cat);
      if (STRUCTURAL[r.cat]) needHatch[r.cat] = 1;
      arcs +=
        '<circle class="donut-arc" cx="100" cy="100" r="' + R + '"' +
        ' stroke="' + paint + '"' +
        ' stroke-dasharray="' + len.toFixed(2) + " " + (C - len).toFixed(2) + '"' +
        ' stroke-dashoffset="' + (-offset).toFixed(2) + '">' +
        "<title>" + labelOf(r.cat) + " " + hm(r.min) + "</title></circle>";
      offset += len;
    }
    var legend = "";
    for (var j = 0; j < rows.length; j++) {
      var q = rows[j];
      legend +=
        '<li><span class="donut-dot' + hatchClass(q.cat) + '" style="background:' + colorVar(q.cat) + '"></span>' +
        labelOf(q.cat) +
        '<b>' + hm(q.min) + "</b></li>";
    }
    // 쓰인 구조 상태만 패턴을 만든다. `away` 는 135도 한 방향, `private` 은 135+45 —
    // `config/palette.yaml` 이 정한 그 각도를 그대로 옮긴다.
    var defs = "";
    Object.keys(needHatch).forEach(function (cat) {
      var c = "var(--structural-" + cat + ")";
      var lines =
        '<line x1="0" y1="0" x2="0" y2="6" stroke="' + c + '" stroke-width="2.2"/>';
      if (cat === "private") {
        lines += '<line x1="0" y1="0" x2="6" y2="0" stroke="' + c + '" stroke-width="2.2"/>';
      }
      defs +=
        '<pattern id="hatch-' + cat + '" width="6" height="6" patternUnits="userSpaceOnUse"' +
        ' patternTransform="rotate(45)">' +
        '<rect width="6" height="6" fill="' + c + '" fill-opacity="0.28"/>' + lines +
        "</pattern>";
    });

    donutBox.innerHTML =
      '<svg viewBox="0 0 200 200" class="donut-svg" aria-hidden="true">' +
      (defs ? "<defs>" + defs + "</defs>" : "") +
      '<circle class="donut-track" cx="100" cy="100" r="' + R + '"></circle>' +
      arcs +
      '<text x="100" y="94" class="donut-center-value">' + hm(total) + "</text>" +
      // ★ 이름을 "기록" 이라고 붙였다가 고쳤다. 상단 '실제 활동'(slot.active_sec,
      //   카테고리와 무관한 원시 관측량)과 이 합계(slot_breakdown 의 분류된 시간)는
      //   **다른 것을 재는 값**이라 같지 않다 — 실측 58분 차이. 같은 화면에 나란히
      //   두면서 이름이 같으면 사람은 어긋난 걸로 읽는다.
      '<text x="100" y="114" class="donut-center-label">' + "합계" + "</text>" +
      "</svg>" +
      '<ul class="donut-legend">' + legend + "</ul>";
  }

  function draw() {
    var all = tally();
    // ★ 합계는 **전부** 더한다 (by_category 와 같은 값이어야 한다).
    //   다만 1분 미만은 줄에서 뺀다 — 원그래프에서 보이지도 않으면서 범례만 채우고,
    //   "0분" 이라고 적히면 고장난 것처럼 읽힌다. 분 단위로 보여주는 화면에서
    //   30초 미만이 빠지는 것은 표시 반올림의 일부다.
    var rows = [];
    for (var n = 0; n < all.length; n++) {
      if (Math.round(all[n].min) >= 1) rows.push(all[n]);
    }
    if (caption) {
      // ★ 문구가 그림과 같은 말을 해야 한다. 총합에 프라이빗이 들어가고, 막대 길이는
      //   1위 대비다 — 둘 다 안 적으면 퍼센트를 길이로 읽는다.
      // ★ 보기마다 다른 말을 해야 한다. 원그래프에는 막대가 없는데 "막대 길이는 1위 대비"
      //   라고 적혀 있었다 — 문구가 그림과 다른 것을 설명하면 안 읽느니만 못하다.
      var isBar = panel.dataset.mode === "bar";
      // ★ 계획 보기는 이제 **합성**이다 — "하기로 한 시간" 만이라고 적으면 거짓이다.
      caption.textContent = planOn()
        ? "계획이 있던 시간은 계획으로, 나머지는 실제로 셉니다."
          + (isBar ? " 막대 길이는 1위 대비, 퍼센트는 전체 대비입니다." : "")
        : "분류된 활동 시간과 프라이빗입니다 (자리비움·결측 제외)."
          + (isBar ? " 막대 길이는 1위 대비, 퍼센트는 전체 대비입니다." : "")
          + " 위 '실제 활동'은 분류와 무관한 관측 총량이라 값이 다릅니다.";
    }
    var total = 0;
    for (var i = 0; i < all.length; i++) total += all[i].min;
    var empty = !rows.length || !total;
    // 비율은 **표시되는 총량**이 아니라 진짜 총량 대비로 낸다.
    emptyMsg.hidden = !empty;
    barBox.hidden = empty;
    donutBox.hidden = empty;
    if (empty) return;
    renderBar(rows, total);
    renderDonut(rows, total);
  }

  function apply(view) {
    var isGrid = view === "grid";
    scroll.hidden = !isGrid;
    panel.hidden = isGrid;
    panel.dataset.mode = view;
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-pressed", btns[i].dataset.viewChoice === view ? "true" : "false");
    }
    if (!isGrid) draw();
  }

  var saved = null;
  try {
    saved = localStorage.getItem(VIEW_KEY);
  } catch (e) {
    saved = null;
  }
  // URL 로도 고를 수 있게 한다 — 자동화 캡처는 localStorage 를 못 건드린다
  // (팔레트 고르개가 같은 이유로 그렇게 돼 있다).
  var urlView = null;
  try {
    urlView = new URLSearchParams(location.search).get("view");
  } catch (e) {
    urlView = null;
  }
  var initial = ["grid", "bar", "donut"].indexOf(urlView) >= 0 ? urlView : saved || "grid";
  apply(initial);

  for (var b = 0; b < btns.length; b++) {
    btns[b].addEventListener("click", function () {
      var name = this.dataset.viewChoice;
      apply(name);
      try {
        localStorage.setItem(VIEW_KEY, name);
      } catch (e) {
        /* 무시 — 이번 화면에는 적용된다 */
      }
    });
  }

  // 계획 토글이 눌리면 차트도 따라간다. 격자의 class 변화를 직접 본다 —
  // 토글 쪽 코드를 고치지 않아도 되고, 나중에 다른 경로로 켜져도 따라온다.
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (!panel.hidden) draw();
    }).observe(grid, { attributes: true, attributeFilter: ["class"] });
  }
})();

// ── 프라이빗 토글 ────────────────────────────────────────────────────────
//
// ★ 독립 IIFE 인 이유: 위쪽 본문은 `if (readOnly) return;` 으로 일찍 빠져나가고
//   `api()` 헬퍼도 그 뒤에 있다. 프라이빗은 read_only 에서도 눌려야 하므로
//   (app.py `_READ_ONLY_EXEMPT`) 여기서 자기 fetch 를 갖는다.
//
// ★ 서버를 매초 찌르지 않는다. 응답의 `until_ts` 가 **절대 시각**이라 남은 시간은
//   브라우저가 스스로 센다. 서버 폴링은 다른 기기(폰 타일)에서 바뀐 것을 따라잡는
//   용도라 느려도 된다.
(function () {
  var btn = document.getElementById("private-toggle");
  if (!btn) return;
  var text = document.getElementById("private-toggle-text");
  var base = (function () {
    var el = document.getElementById("app-base");
    return (el && el.dataset.base) || "";
  })();

  var SERVER_POLL_MS = 60000; // 폰에서 켠 것을 따라잡는 주기
  var untilTs = null;         // 절대 시각(초). null 이면 꺼짐
  var skew = 0;               // 서버 시계 − 내 시계
  var busy = false;

  function now() {
    return Date.now() / 1000 + skew;
  }

  function send(method, body) {
    busy = true;
    render();
    return fetch(base + "/api/private", {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    })
      .then(function (res) {
        return res.json().then(function (p) {
          if (!res.ok) throw new Error((p && p.message) || res.status);
          return p;
        });
      })
      .then(absorb)
      .catch(function (err) {
        // 실패를 조용히 넘기지 않는다 — "켰다고 생각했는데 안 켜졌다"가 최악이다.
        window.alert("프라이빗 전환 실패: " + err.message);
      })
      .then(function () {
        busy = false;
        render();
      });
  }

  function absorb(payload) {
    if (!payload) return;
    if (typeof payload.server_ts === "number") skew = payload.server_ts - Date.now() / 1000;
    untilTs = payload.active ? payload.until_ts : null;
    render();
  }

  function render() {
    btn.hidden = false;
    var left = untilTs === null ? 0 : Math.ceil(untilTs - now());
    if (untilTs !== null && left <= 0) {
      // 만료됐다 — 화면만 먼저 끄고 서버에 확인한다.
      untilTs = null;
      left = 0;
      refresh();
    }
    var on = untilTs !== null;
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.disabled = busy;
    if (busy) {
      text.textContent = "…";
    } else if (on) {
      var m = Math.floor(left / 60);
      var sec = left % 60;
      text.textContent = m >= 1 ? m + "분 남음" : sec + "초 남음";
      btn.title = "누르면 지금 끕니다. 끈 뒤 시간부터 다시 기록됩니다.";
    } else {
      text.textContent = "프라이빗";
      btn.title = "켜면 그 시간은 수집되지 않는다";
    }
  }

  function refresh() {
    return fetch(base + "/api/private", { headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(absorb)
      .catch(function () {
        /* 네트워크가 끊겼을 뿐이다. 마지막으로 아는 상태를 그대로 보여준다. */
      });
  }

  btn.addEventListener("click", function () {
    if (busy) return;
    if (untilTs === null) {
      send("POST", {});
    } else if (window.confirm("프라이빗을 지금 끕니다.\n끈 시각부터 다시 기록되며, 프라이빗 구간의 기록은 돌아오지 않습니다.")) {
      send("DELETE");
    }
  });

  refresh();
  setInterval(render, 1000);
  setInterval(refresh, SERVER_POLL_MS);
})();
