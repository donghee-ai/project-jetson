"""`lt doctor` 의 점검 항목들.

## 왜 갈랐나 (2026-09-07)

`cmd_doctor` 가 **507줄**이었다. 실사에서 지적됐고, 지적된 그날 내가 항목 둘
(수집 소스 · CI 도달)을 더 넣어 **더 키웠다.**

항목이 한 함수 안에 있으면 두 가지를 못 한다:

- **항목 하나만 따로 테스트할 수 없다.** 지금까지 doctor 의 판정 로직은
  테스트가 없었다 — 507줄 함수를 부르려면 DB·AW·LLM·Slack 이 다 있어야 한다
- **새 항목을 넣을 때 §1 의 여섯 질문을 강제할 자리가 없다**
  (언제 꺼지나 · 원인을 고친 직후 · 미확인 상태 · 두 번 세지 않나 · …)

이제 항목은 전부 `check_*(cfg, report)` 이고, `Report` 하나만 있으면 부를 수 있다.

## 순서는 그대로다

`run_all` 이 부르는 순서가 화면에 나오는 순서이고, **옮기기 전과 같다.**
`db` 가 연 커넥션을 `data` 가 쓰기 때문에 그 둘은 순서가 의미를 갖는다.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from lifetrainer import db, devdb, timeutil
from lifetrainer.config import Config


@dataclass
class Report:
    """점검 결과를 모으는 곳. `(상태, 이름, 설명)` 세 쪽짜리다.

    ★ 화면에 바로 찍지 않고 모으는 이유: 마지막에 **OK/WARN/FAIL 개수**를 세야 하고,
      FAIL 이 있으면 조치 목록을 따로 다시 출력하기 때문이다.
    """

    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append(("OK", name, detail))

    def warn(self, name: str, detail: str) -> None:
        self.checks.append(("WARN", name, detail))

    def fail(self, name: str, detail: str) -> None:
        self.checks.append(("FAIL", name, detail))

    @property
    def fails(self) -> list[tuple[str, str, str]]:
        return [c for c in self.checks if c[0] == "FAIL"]

    @property
    def warns(self) -> list[tuple[str, str, str]]:
        return [c for c in self.checks if c[0] == "WARN"]


def check_db(cfg: Config, report: Report) -> sqlite3.Connection | None:
    """DB · 스키마 버전 · **어느 DB 를 보고 있나**"""
    # DB 존재 / 스키마 버전
    conn: sqlite3.Connection | None = None
    if cfg.db_path.exists():
        try:
            conn = db.connect(cfg.db_path)
            version = db.schema_version(conn)
            # ★ **어느 DB 를 보고 있는지 doctor 가 말한다.** `--dev` 를 준 것을 잊고
            #   "운영이 이상하다" 고 읽는 것을 막는다 — 이 저장소가 nvm/시스템 Node 로
            #   이미 겪은 부류다(*경로가 아니라 실물을 본다*).
            role = devdb.role_of(conn)
            role_note = "  ★ 개발 사본" if role == devdb.ROLE_DEV else ""
            if version == db.SCHEMA_VERSION:
                report.ok("DB", f"{cfg.db_path} (schema v{version}){role_note}")
            elif version == 0:
                report.warn("DB", f"{cfg.db_path} 존재하지만 스키마 미적용 — `lt init-db` 실행 필요")
            else:
                report.warn(
                    "DB",
                    f"{cfg.db_path} 스키마 버전 {version} (코드 기준 v{db.SCHEMA_VERSION}) — 마이그레이션 확인 필요",
                )
        except Exception as exc:  # noqa: BLE001
            report.fail("DB", f"{cfg.db_path} 열기 실패: {exc} — 파일 손상 시 `lt init-db` 로 재생성 검토")
    else:
        report.fail("DB", f"{cfg.db_path} 없음 — `lt init-db` 실행 필요")

    # 1-b. 프라이빗 모드 — ★ **정보로만 낸다. WARN 으로 올리지 않는다.**
    #
    #   프라이빗이 켜져 있는 것은 고장이 아니라 사람이 그렇게 정한 상태다.
    #   여기서 노란불을 켜면 "켤 때마다 doctor 가 운다" 가 되고, 그러면 사람이
    #   doctor 를 안 본다 (CLAUDE.md §1 — 안 울려야 할 때 우는 검사).
    #
    #   그래도 **적기는 한다.** 커버리지가 왜 낮은지 물었을 때 답이 여기 있어야 한다.
    if conn is not None:
        try:
            from lifetrainer import privacy

            st = privacy.state(conn)
            if st.active:
                left = max(0.0, st.until_ts - st.server_ts) / 60.0
                report.ok("프라이빗", f"켜짐 — {left:.0f}분 남음. 이 시간은 수집되지 않는다")
            else:
                today = conn.execute(
                    "SELECT COUNT(*) AS c FROM private_span WHERE revoked = 0 AND end_ts > ?",
                    (st.server_ts - 86400,),
                ).fetchone()["c"]
                report.ok("프라이빗", f"꺼짐 (최근 24시간 구간 {today}개)")
        except Exception as exc:  # noqa: BLE001
            report.warn("프라이빗", f"상태 조회 실패: {exc}")
    return conn


def check_activitywatch(cfg: Config, report: Report) -> None:
    """ActivityWatch 연결과 버킷 목록"""
    # ActivityWatch 연결 + 버킷 목록
    try:
        from lifetrainer.collect.aw_client import AWClient

        client = AWClient(cfg.aw.base_url, api_key=cfg.aw.api_key, timeout=cfg.aw.timeout_sec)
        if client.ping():
            try:
                buckets = client.buckets()
                names = ", ".join(list(buckets)[:5]) + (" …" if len(buckets) > 5 else "")
                report.ok("ActivityWatch", f"{cfg.aw.base_url} 연결됨, 버킷 {len(buckets)}개: {names}")
            except Exception as exc:  # noqa: BLE001
                report.warn("ActivityWatch", f"ping 은 됐지만 버킷 조회 실패: {exc}")
        else:
            report.warn(
                "ActivityWatch",
                f"{cfg.aw.base_url} 연결 실패 — Windows PC 에 설치 필요"
                " (scripts/setup-activitywatch-windows.ps1). 그 전까지는 `lt synth` 로 합성 데이터 사용",
            )
    except Exception as exc:  # noqa: BLE001
        report.warn("ActivityWatch", f"클라이언트 생성 실패: {exc}")


def check_data(cfg: Config, report: Report, conn: sqlite3.Connection | None) -> None:
    """이벤트 · 롤업 · 워처 침묵 · 수집 소스 · CI 도달"""
    # 마지막 동기화 시각 / 이벤트 수 / 롤업 범위
    if conn is not None:
        try:
            cursor_row = conn.execute(
                "SELECT MAX(value) AS v FROM sync_state WHERE key LIKE 'aw_cursor:%'"
            ).fetchone()
            event_count = conn.execute("SELECT COUNT(*) FROM aw_event").fetchone()[0]
            if event_count:
                last_sync = f", 마지막 커서={timeutil.iso_utc(float(cursor_row['v']))}" if cursor_row and cursor_row["v"] else ""
                report.ok("이벤트", f"총 {event_count}개{last_sync}")
            else:
                report.warn("이벤트", "0개 — `lt sync` 또는 `lt synth --days 14` 로 데이터 생성")

            rollup_row = conn.execute("SELECT MIN(day) AS a, MAX(day) AS b FROM slot").fetchone()
            if rollup_row and rollup_row["a"]:
                report.ok("롤업", f"{rollup_row['a']} ~ {rollup_row['b']}")
            else:
                report.warn("롤업", "롤업된 날짜가 없음 — `lt rollup --range <시작> <끝>` 실행 필요")

            # ── 롤업이 **지금도 돌고 있나** ─────────────────────────────
            #
            # ★ 2026-09-01 에 생겼다. 그날 마이그레이션 하나가 밀려서 롤업이 10분마다
            #   죽었는데, **아무것도 그걸 안 잡았다.** doctor 는 "롤업 08-12 ~ 09-01"
            #   이라고 OK 를 찍었다 — 범위는 맞았고, 그 범위가 멈춰 있다는 것만 몰랐다.
            #
            # ★ 왜 `aw_cursor` 로 안 재나 (이걸로 쟀으면 매일 아침 헛경보다):
            #   커서는 **PC 에 새 이벤트가 있을 때만** 전진한다. 노트북을 꺼 두면
            #   밤새 안 움직이는데 그건 고장이 아니다. AW 에 못 닿는 것은 위의
            #   ActivityWatch 항목이 이미 따로 말한다 — 한 사건에 경보는 하나다.
            #
            # ★ `slot.updated_at` 은 다르다. 롤업은 이벤트가 없어도 오늘 144칸을
            #   전부 다시 쓴다(빈 칸은 off). **PC 가 꺼져 있어도 전진한다.**
            #   멈췄다면 멈춘 것은 우리 쪽이다.
            #
            # 언제 꺼지나: 롤업이 한 번 성공하면 즉시. 누적이 아니라 마지막 시각이다.
            STALE_WARN_H = 1.0   # 10분 주기 기준 6회 연속 누락. 재부팅 정도로는 안 운다
            fresh = conn.execute("SELECT MAX(updated_at) AS m FROM slot").fetchone()
            if fresh is None or fresh["m"] is None:
                # ★ 실패가 아니라 **미확인**이다. 갓 만든 DB 는 아직 롤업한 적이 없다.
                report.ok("롤업 신선도", "아직 롤업한 적 없음 — `lt rollup --today`")
            else:
                age_h = (time.time() - float(fresh["m"])) / 3600.0
                if age_h > STALE_WARN_H:
                    report.warn(
                        "롤업 신선도",
                        f"마지막 갱신 {age_h:.1f}시간 전 — 10분마다 돌아야 한다."
                        " `journalctl --user -u lifetrainer-sync.service -n 30` 로 원인 확인",
                    )
                else:
                    report.ok("롤업 신선도", f"{age_h * 60:.0f}분 전")

            # ── 워처 하나가 **혼자** 조용한가 ──────────────────────────
            #
            # ★ 2026-09-04 에 생겼다. 폰 웹 워처가 닷새(08-29~09-03), 미디어 워처가
            #   15시간(09-02~03) 멈춰 있었는데 아무것도 안 잡았다. 위의 "롤업 신선도"는
            #   *우리 쪽 파이프가 도나* 를 보는데, 이번 고장은 **파이프는 도는데 원료
            #   하나가 끊긴 것**이라 그대로 통과한다.
            #
            # ★ 왜 절대 시간으로 안 재나: 밤에는 모든 버킷이 같이 조용하다. "N시간째
            #   조용" 으로 걸면 매일 아침 운다. 그래서 **같은 기기의 세션 버킷과 비교**
            #   한다 — 기기가 살아 있는데 이 워처만 조용하면 그건 고장이다.
            #   기기를 안 쓰면(밤·꺼짐·프라이빗) 세션도 같이 끊겨 조건이 성립하지 않는다.
            #
            # ★ 왜 `aw_bucket.last_seen` 을 못 쓰나: `upsert_bucket` 이 **이벤트가 없어도**
            #   매 sync 마다 now 로 갱신한다. 죽은 워처의 버킷도 계속 신선해 보인다.
            #   그래서 `MAX(ts_end)` 를 본다 (`idx_aw_event_bkt_end` 가 덮는다).
            #
            # 임계값은 실측으로 정했다. 같은 방법을 두 번 돌렸다 —
            # 30분 간격 표본 중 **기기가 살아 있던 시점**만 세고, 발동한 *날* 수를 본다:
            #
            #     대상    6h    12h                    24h   48h
            #     media   5일   1일(실제 사고 09-03)   0일   0일   ← 12h 가 딱 그 하루만 잡는다
            #     unlock  3일   1일(실제 사고 09-04)   0일   0일   ← 〃 (09-04 재측정, 표본 547)
            #     web     13일  9일                    8일   6일
            #
            # ★ **웹은 아직 안 넣는다.** 48시간에도 6일 발동하는데, 관측 기간 대부분
            #   웹 워처가 **실제로 죽어 있었다**(08-22~25 권한 꺼짐, 08-29~09-03 크래시).
            #   저 발동은 오검출이 아니라 진짜다 — 즉 **깨끗한 구간이 없어 임계값을
            #   정할 근거가 없다.** 09-03 16:00 에 살아났으니 일주일 뒤 다시 재서 넣는다.
            #   근거 없는 숫자를 지금 박으면 그게 그대로 굳는다.
            #
            # ★ unlock 은 09-04 에 더했다. **media 를 넣은 그날 unlock 이 죽어 있었다** —
            #   09-03 15:12 이후 조용한데 폰은 계속 쓰이고 있었고, 23일간 최장 간격은
            #   10.2시간(밤)이었다. 하나를 붙이면서 옆의 것을 안 본 것이라,
            #   목록을 아래 표로 뺐다. 다음에 웹을 넣을 때는 한 줄이다.
            #
            # 언제 꺼지나: 그 워처가 이벤트를 하나 넣는 즉시.
            SESSION_FRESH_H = 2.0   # 기기가 "살아 있다"고 볼 기준
            # (버킷 접미사, 화면에 쓸 이름, 뒤처짐 경보 기준 시간)
            WATCHED_PHONE_BUCKETS = (
                ("-media", "media", 12.0),
                ("-unlock", "unlock", 12.0),
            )
            rows = conn.execute(
                """
                SELECT d.name AS device, b.bucket_id, MAX(e.ts_end) AS last_end
                FROM aw_event e
                JOIN aw_bucket b ON b.bucket_id = e.bucket_id
                JOIN device d ON d.id = b.device_id
                WHERE d.kind = 'phone'
                GROUP BY d.name, b.bucket_id
                """
            ).fetchall()
            now = time.time()
            by_device: dict[str, dict[str, float]] = {}
            for row in rows:
                by_device.setdefault(row["device"], {})[row["bucket_id"]] = float(row["last_end"])

            quiet: list[str] = []
            for device, ends in by_device.items():
                # 세션 = 앱 사용 버킷. 미디어·unlock·web 은 세션이 아니다.
                session_ends = [
                    v for k, v in ends.items()
                    if not any(k.endswith(sfx) for sfx in ("-media", "-unlock", "-web"))
                ]
                if not session_ends:
                    continue
                session_end = max(session_ends)
                if (now - session_end) / 3600.0 > SESSION_FRESH_H:
                    continue  # 기기가 조용하다 — 워처 탓이 아니다
                for suffix, label, warn_h in WATCHED_PHONE_BUCKETS:
                    last = next((v for k, v in ends.items() if k.endswith(suffix)), None)
                    if last is None:
                        # ★ 옳지만 아직 증명 못 한 상태다. 한 번도 안 튼 기기를 고장으로
                        #   세지 않는다 (CLAUDE.md §1).
                        continue
                    lag_h = (session_end - last) / 3600.0
                    if lag_h > warn_h:
                        quiet.append(f"{device} {label} {lag_h:.0f}시간 뒤처짐")
            if not rows:
                report.ok("워처 침묵", "폰 기기가 없다")
            elif quiet:
                report.warn(
                    "워처 침묵",
                    ", ".join(quiet)
                    + " — 폰이 쓰이는 중인데 그 워처만 조용하다."
                    " 알림 접근/접근성 권한이 앱 업데이트로 풀렸을 수 있다",
                )
            else:
                report.ok("워처 침묵", "폰 워처가 기기와 같이 살아 있다")

            # ── 수집 소스가 **죽어 있나** ──────────────────────────────
            #
            # ★ 2026-09-07 에 생겼다. 그날 `source` 표를 눈으로 보니
            #   BAIR 가 11회 연속 실패(9.7일째), Microsoft Research 중복 항목이 404 였다.
            #   **`fail_count` 를 읽는 코드가 하나도 없었다** — 세기만 하고 아무도 안 봤다.
            #   *만들었다 ≠ 그게 실제로 불린다* (저장소 규칙 §2).
            #
            #   같은 날 더 나쁜 것도 나왔다: arXiv 8개는 `last_status` 가 전부 NULL 이었다.
            #   `_mark_source_result` 가 `url_state` 를 `source.url`("cat:cs.CL")로 찾는데
            #   실제 요청 URL 은 파라미터가 붙은 다른 주소라 **언제나 못 찾았다**.
            #   그래서 아래 두 번째 갈래가 있다 — 결과가 **안 적히는 것** 자체가 고장이다.
            #   (그건 "실패 0건" 과 구분이 안 되므로 fail_count 로는 영영 안 보인다.)
            #
            # 언제 꺼지나: 그 소스가 **한 번 성공하면 즉시**. 누적이 아니다
            #   (`_mark_source_result` 가 성공 시 fail_count 를 0 으로 되돌린다).
            #
            # 왜 3회인가: 백오프가 붙어 있어 3회면 이미 수 시간~하루다. 1회로 잡으면
            #   남의 서버 502 한 번에 매번 노란불이 켜지고, 그러면 사람이 doctor 를 안 본다.
            #
            # 한 번도 안 받아 본 소스(`last_fetched IS NULL`)는 **미확인이지 실패가 아니다.**
            SOURCE_FAIL_WARN = 3
            dead = conn.execute(
                "SELECT name, last_status, fail_count FROM source "
                "WHERE enabled = 1 AND fail_count >= ? ORDER BY fail_count DESC, name",
                (SOURCE_FAIL_WARN,),
            ).fetchall()
            # ★ `fail_count = 0` 을 같이 본다 (2026-09-07 에 좁혔다).
            #
            #   처음엔 `last_status IS NULL` 만 봤는데, 그러면 **연결 실패**가 걸린다 —
            #   TCP 가 안 붙으면 HTTP 상태 자체가 없으므로 NULL 이 정상이다.
            #   실제로 붙이자마자 Hacker News 가 걸렸고(일시적 연결 실패, fail_count=1),
            #   그건 이미 위 "연속 실패" 갈래가 볼 일이다 — **한 사건에 경보는 하나다.**
            #
            #   찾으려던 것은 *"실패로도 안 세지고 상태도 없는"* 자리다. arXiv 가 정확히
            #   그랬다(fail_count 0 · last_status NULL). 그 조합만 남긴다.
            mute = conn.execute(
                "SELECT name FROM source "
                "WHERE enabled = 1 AND last_fetched IS NOT NULL "
                "  AND last_status IS NULL AND fail_count = 0 "
                "ORDER BY name",
            ).fetchall()
            if dead or mute:
                parts = []
                if dead:
                    parts.append(
                        "연속 실패: "
                        + ", ".join(f"{r['name']}({r['fail_count']}회/{r['last_status'] or '응답없음'})" for r in dead)
                    )
                if mute:
                    names = [r["name"] for r in mute]
                    shown = ", ".join(names[:3]) + (f" 외 {len(names) - 3}개" if len(names) > 3 else "")
                    parts.append(f"결과가 안 적히는 소스 {len(names)}개: {shown}")
                report.warn(
                    "수집 소스",
                    " · ".join(parts) + " — `lt collect --once` 로 재현하고,"
                    " 주소가 죽었으면 config/sources.yaml 에서 enabled: false",
                )
            else:
                total = conn.execute("SELECT COUNT(*) AS n FROM source WHERE enabled = 1").fetchone()["n"]
                report.ok("수집 소스", f"{total}개 모두 정상")

        except Exception as exc:  # noqa: BLE001
            report.warn("DB 조회", f"이벤트/롤업 조회 실패: {exc}")


def check_llm(cfg: Config, report: Report) -> None:
    """LLM 헬스와 실제 모델 id (포트만 보면 엉뚱한 모델을 오래 쓴다)"""
    # LLM 헬스 + 실제 모델 id
    try:
        from lifetrainer.llm.client import LLMClient

        llm = LLMClient(cfg)
        if llm.health():
            model_id = llm.model_id()
            if model_id == cfg.llm.model:
                report.ok("LLM", f"{cfg.llm.base_url} 정상, 모델={model_id}")
            elif model_id:
                report.warn(
                    "LLM",
                    f"{cfg.llm.base_url} 응답하지만 모델 불일치 (실제={model_id!r}, 설정={cfg.llm.model!r})"
                    " — 포트만 보고 넘어가면 엉뚱한 모델을 오래 쓸 수 있다. config 를 실제 모델에 맞추세요.",
                )
            else:
                report.warn("LLM", f"{cfg.llm.base_url} 헬스체크는 통과했지만 모델 id 조회 실패")
        else:
            report.warn("LLM", f"{cfg.llm.base_url} 연결 실패 — llama-server 가 떠 있는지 확인 (Phase 3 기능만 영향)")
    except Exception as exc:  # noqa: BLE001
        report.warn("LLM", f"클라이언트 생성 실패: {exc}")


def check_slack(cfg: Config, report: Report) -> None:
    """Slack 토큰과 채널"""
    # Slack 토큰 / 채널
    if cfg.slack.bot_token:
        if cfg.slack.default_channel:
            report.ok("Slack", f"토큰 있음 (mode={cfg.slack.mode}), 채널={cfg.slack.default_channel}")
        else:
            report.warn(
                "Slack",
                "토큰은 있지만 default_channel 미설정 — config/lifetrainer.toml [slack].default_channel 설정 필요",
            )
    else:
        report.warn(
            "Slack",
            "봇 토큰 없음(openclaw 폴백 포함) — 리포트 발송 불가."
            " config/lifetrainer.toml [slack].bot_token 또는 openclaw_config 확인",
        )


def check_font(cfg: Config, report: Report) -> None:
    """한글 폰트"""
    # 한글 폰트
    try:
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        candidates = [cfg.report.font_family, "NanumGothic", "Noto Sans CJK KR", "DejaVu Sans"]
        found = next((c for c in candidates if c in available), None)
        if found and found != "DejaVu Sans":
            report.ok("한글 폰트", f"{found} 사용 가능")
        elif found == "DejaVu Sans":
            report.warn(
                "한글 폰트",
                "한글 폰트 없음 — DejaVu Sans 로 대체, PNG 의 한글이 두부(□)로 깨질 수 있음."
                " `sudo apt install fonts-nanum` 권장",
            )
        else:
            report.warn("한글 폰트", "폰트 후보를 하나도 찾지 못함")
    except Exception as exc:  # noqa: BLE001
        report.warn("한글 폰트", f"확인 실패: {exc}")


def check_disk(cfg: Config, report: Report) -> None:
    """디스크 여유"""
    # 디스크 여유
    try:
        target = cfg.data_dir if cfg.data_dir.exists() else cfg.root
        usage = shutil.disk_usage(target)
        free_gb = usage.free / (1024**3)
        if free_gb > 2.0:
            report.ok("디스크", f"{free_gb:.1f} GB 여유 ({target})")
        elif free_gb > 0.5:
            report.warn("디스크", f"{free_gb:.1f} GB 여유 ({target}) — 여유가 줄고 있음, 오래된 백업/PNG 정리 검토")
        else:
            report.fail("디스크", f"{free_gb:.1f} GB 여유 ({target}) — 공간을 확보하지 않으면 쓰기가 실패할 수 있음")
    except Exception as exc:  # noqa: BLE001
        report.warn("디스크", f"확인 실패: {exc}")


def check_queue(cfg: Config, report: Report, conn: sqlite3.Connection | None) -> None:
    """잡 큐 상태"""
    # 큐 상태
    if conn is not None:
        try:
            from lifetrainer.llm.queue import stats as queue_stats

            from lifetrainer.llm.queue import health as queue_health

            qs = queue_stats(conn)
            detail = ", ".join(f"{k}={v}" for k, v in sorted(qs.items())) if qs else "비어 있음"

            # ★ 조회가 됐다고 OK 가 아니다 (2026-08-21). 요약 잡 277건이 503 으로
            #   죽어 있었는데 이 항목이 개수만 나열하고 OK 를 찍어 이틀을 못 봤다.
            #
            # ★ 그렇다고 **누적 실패율**도 아니다 (2026-08-28, issues/0016).
            #   `purge_done` 이 종료 잡을 14일 뒤에 걷어가므로 done 은 회전하는데
            #   failed 는 그 창이 지나야 사라진다. 그래서 원인을 고친 뒤에도 2주 동안
            #   노란불이 남고, **그 사이 새 실패가 들어와도 구분이 안 된다.**
            #   상시 켜진 신호는 신호가 아니다.
            #
            #   판정 축은 "얼마나 실패했나" 가 아니라 **"지금 실패하고 있나"** 다.
            h = queue_health(conn)
            RECENT_H, BACKLOG_H, MIN_SAMPLE, RATE = 48.0, 6.0, 10, 0.10
            since_h = h.hours_since_last_failure

            if h.stale_running:
                # 워커가 잡을 집다 죽으면 실패가 아니라 여기 남는다. 실패율엔 안 잡힌다.
                report.warn("큐", f"{detail} · running 인데 lease 만료 {h.stale_running}건"
                           " — 워커가 잡을 집은 채 죽었다. `lt queue stats` 확인")
            elif h.oldest_queued_age > BACKLOG_H * 3600:
                # backlog 도 실패율엔 안 잡힌다 — 아무것도 실패하지 않고 그냥 안 돈다.
                report.warn("큐", f"{detail} · 가장 오래된 대기 잡 {h.oldest_queued_age/3600:.1f}시간"
                           f" (기준 {BACKLOG_H:.0f}h) — 워커가 도는지 확인")
            elif (since_h is not None and since_h <= RECENT_H
                  and h.recent_finished >= MIN_SAMPLE and h.recent_rate >= RATE):
                worst = f" · 최악 {h.worst_kind[0]} {h.worst_kind[1]}/{h.worst_kind[2]}" if h.worst_kind else ""
                top = _top_job_error(conn)
                report.warn("큐", f"{detail} · 최근 {h.window_days}일 실패율 {h.recent_rate:.0%}"
                           f" ({h.recent_failed}/{h.recent_finished}){worst}"
                           f" · 마지막 실패 {since_h:.0f}시간 전"
                           + (f" — 대표 사유: {top}" if top else ""))
            elif since_h is not None and h.success_since_last_failure:
                # 무더기로 죽었어도 그 뒤로 계속 성공했으면 **고쳐진 것**이다.
                report.ok("큐", f"{detail} · 마지막 실패 {since_h/24:.1f}일 전, "
                         f"이후 {h.success_since_last_failure:,}건 연속 성공")
            else:
                report.ok("큐", detail)
        except Exception as exc:  # noqa: BLE001
            report.warn("큐", f"조회 실패: {exc}")

    # 8-B. 웹 검색 키 (없으면 대화가 URL 을 추측한다 — docs/issues/h-0006-the-model-cites-one-source-and-stays-there.md)


def check_embed(cfg: Config, report: Report, conn: sqlite3.Connection | None) -> None:
    """임베딩(RAG) 적재와 밀린 양"""
    # 임베딩 (RAG) ──
    if cfg.embed.enabled:
        try:
            import requests as _rq

            n_vec = conn.execute("SELECT COUNT(*) FROM doc_embedding").fetchone()[0] if conn else 0
            n_doc = conn.execute("SELECT COUNT(*) FROM doc WHERE dup_of IS NULL").fetchone()[0] if conn else 0
            _rq.get(cfg.embed.base_url.rstrip("/") + "/models", timeout=3).raise_for_status()

            # ★ 행 수 비교만으로는 "밀리는 중" 과 "밀린 채 멈춤" 을 구분 못 한다 (2026-08-28).
            #   전에는 부족하면 무조건 `lt embed --all` 을 권했는데, 그건 증상을 손으로
            #   지우는 것이지 원인을 고치는 게 아니다. 실제 원인은 둘이었다:
            #   야간 한도가 요약 한도에 묶여 유입의 1/6 이었고, 서버가 누수로 죽으면
            #   배치가 그 자리에서 통째로 끝났다. **가장 오래 밀린 문서의 나이**가
            #   그 둘을 구분한다 — 어제 것만 밀렸으면 따라잡는 중이고,
            #   열흘 된 것이 남아 있으면 구조적으로 못 따라가는 것이다.
            stale = conn.execute(
                "SELECT MIN(d.fetched_at) FROM doc d"
                " LEFT JOIN doc_embedding e ON e.doc_id = d.id"
                " WHERE d.dup_of IS NULL AND e.doc_id IS NULL"
            ).fetchone()[0] if conn else None
            # 설정과 다른 모델·차원으로 만들어진 벡터는 검색에서 조용히 틀린 이웃을 준다.
            mism = conn.execute(
                "SELECT COUNT(*) FROM doc_embedding WHERE model <> ? OR dim <> ?",
                (cfg.embed.model, cfg.embed.dim),
            ).fetchone()[0] if conn else 0

            age_d = (time.time() - stale) / 86400 if stale else 0.0
            if mism:
                report.warn("임베딩", f"벡터 {n_vec}/{n_doc}건 · **{mism}건이 다른 모델·차원**"
                               f" (설정 {cfg.embed.model}/{cfg.embed.dim}) — `lt embed --all --force`")
            elif stale and age_d > 2:
                report.warn("임베딩", f"벡터 {n_vec}/{n_doc}건 · 가장 오래 밀린 문서 {age_d:.0f}일"
                               f" — 야간 한도({cfg.nightly.embed_limit})가 유입을 못 따라가는지 본다")
            elif stale:
                report.ok("임베딩", f"{cfg.embed.model} · 벡터 {n_vec}/{n_doc}건 ·"
                             f" 밀린 것 {n_doc - n_vec}건 (가장 오래된 것 {age_d * 24:.0f}시간 — 따라잡는 중)")
            else:
                report.ok("임베딩", f"{cfg.embed.model} · 벡터 {n_vec}/{n_doc}건 · 밀린 것 없음")
        except Exception as exc:  # noqa: BLE001
            report.warn("임베딩", f"서버에 닿지 않습니다 ({cfg.embed.base_url}) — 검색이 키워드로만 간다: {exc}")

    try:
        from lifetrainer.llm.websearch import providers_available

        have = providers_available(cfg)
        if have["serper"]:
            # 공급자는 Serper 하나다(2026-08-25 에 네이버 경로를 지웠다). 하나뿐인
            # 것이 정상 구성이라 OK 를 준다 — 못 하는 일을 WARN 으로 권하지 않는다.
            report.ok("웹 검색", "Serper (구글 경유) — 한국어 질의는 gl=kr 로 간다")
        else:
            report.warn(
                "웹 검색",
                "키 없음 — 대화가 주소를 추측한다. "
                "config/lifetrainer.toml 의 [search] 에 serper_api_key 를 넣는다 "
                "(serper.dev, 2,500건 무료). 네이버는 API HUB 이관으로 신규 발급 불가 "
                "— HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md",
            )
    except Exception as exc:  # noqa: BLE001
        report.warn("웹 검색", f"확인 실패: {exc}")


def check_agent(cfg: Config, report: Report) -> None:
    """OpenClaw 에이전트 배선"""
    # OpenClaw 에이전트 배선
    #
    # ★ 이 배선은 **저장소 밖**(`~/.openclaw/openclaw.json`)에 있다. 기기를 다시
    #   세우거나 `openclaw` 를 재설치하면 조용히 사라지는데, 그때 증상은
    #   "에이전트가 툴을 하나도 안 부른다" 뿐이라 원인을 찾기 어렵다. 여기서 본다.
    try:
        _check_agent_wiring(cfg, report)
    except Exception as exc:  # noqa: BLE001 - 진단이 진단 때문에 죽으면 안 된다
        report.warn("에이전트", f"확인 실패: {exc}")


def _top_job_error(conn: sqlite3.Connection) -> str:
    """실패 잡의 가장 흔한 오류를 한 줄로. 없거나 조회에 실패하면 빈 문자열.

    `lt doctor` 는 진단 도구라 **무엇이 잘못됐는지까지** 말해야 한다. 개수만 보여주면
    사람이 sqlite3 를 열어야 하고, 그 한 단계가 실제로 이틀을 잡아먹었다.
    """
    try:
        row = conn.execute(
            "SELECT error, COUNT(*) AS c FROM job WHERE state = 'failed' AND error IS NOT NULL"
            " GROUP BY substr(error, 1, 60) ORDER BY c DESC LIMIT 1"
        ).fetchone()
    except Exception:  # noqa: BLE001 - 진단이 진단 때문에 죽으면 안 된다
        return ""
    if row is None or not row["error"]:
        return ""
    text = " ".join(str(row["error"]).split())
    return f"{text[:70]}… ×{row['c']}" if len(text) > 70 else f"{text} ×{row['c']}"


def check_ci_reach(report: Report) -> None:
    """CI 가 **얼마나 오래** 이 코드를 못 봤나 (2026-09-07).

    ★ 왜 생겼나: 실사에서 안 올라간 커밋이 13개였고, CI 가 마지막으로 본 것은 이틀 전
      커밋이었다. 워크플로 자체는 잘 짜여 있다 — 검사기를 fixture 로 자기시험하고,
      문서 지표 표절을 막고, 테스트를 소켓 차단 상태로 돌린다. **다만 안 돌았다.**
      *만들었다 ≠ 그게 실제로 불린다* (저장소 규칙 §2).

    ★ 왜 **개수**가 아니라 **나이**인가: 커밋이 몇 개 밀렸는지는 고장이 아니다 —
      하루에 열 번 커밋하는 날도 있다. 문제는 *"CI 가 며칠째 못 봤나"* 다.
      같은 날 작업하는 동안에는 안 울고, 사흘 넘게 묵으면 운다.

    언제 꺼지나: **push 하는 즉시.** 누적이 아니라 가장 오래된 미푸시 커밋의 나이다.

    ★ 네트워크를 안 쓴다. `origin/main` 로컬 ref 만 본다 — doctor 는 빨라야 한다.
      그래서 다른 기기에서 push 했으면 `git fetch` 전까지 낡은 값을 볼 수 있다.
      그 경우도 안전한 쪽으로 틀린다(있지도 않은 밀림을 말할 뿐, 놓치지 않는다).
    """
    import subprocess

    STALE_DAYS = 3.0
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%ct", "origin/main..HEAD"],
            capture_output=True, text=True, timeout=10, check=False,  # 반환코드는 아래서 직접 본다
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
    except Exception:  # noqa: BLE001 - git 이 없거나 저장소가 아니면 할 말이 없다
        return
    if proc.returncode != 0:
        # 업스트림이 없는 사본이다 — 실패가 아니라 **해당 없음**이다.
        return

    stamps = [int(line) for line in proc.stdout.split() if line.isdigit()]
    if not stamps:
        report.ok("CI 도달", "안 올라간 커밋 없음 — CI 가 HEAD 를 봤다")
        return

    age_days = (time.time() - min(stamps)) / 86400.0
    if age_days > STALE_DAYS:
        report.warn(
            "CI 도달",
            f"커밋 {len(stamps)}개가 {age_days:.1f}일째 안 올라갔다 — 그동안 CI 가 아무것도 안 봤다."
            " `git push` (pre-push 훅이 make check 를 돌린다)",
        )
    else:
        report.ok("CI 도달", f"안 올라간 커밋 {len(stamps)}개 (가장 오래된 것 {age_days:.1f}일)")


def _check_agent_wiring(cfg: Config, report: Report) -> None:
    """에이전트 쪽 점검 — 예산 · 워크스페이스 · OpenClaw 등록.

    **모델을 부르지 않는다.** `doctor` 는 빨라야 하고, 한 턴이 26초다.
    부르지 않고 알 수 있는 것만 본다.
    """
    import json as _json

    from lifetrainer.agent import catalog as catalog_mod
    from lifetrainer.agent import prompt as prompt_mod
    from lifetrainer.agent.config import build_sandbox, load_settings
    from lifetrainer.llm.client import estimate_tokens

    settings = load_settings(cfg)
    if not settings.enabled:
        report.warn("에이전트", "config 의 [agent] enabled=false — 꺼져 있다")
        return

    # ① 예산. 넘으면 첫 턴이 길어지고 ctx 20480 에서 압축이 걸린다.
    # ★ 이름을 `budget` 으로 둔다 — `report` 는 이 함수의 인자(Report)다.
    #   옮길 때 이 자리에서 실제로 부딪혔고, doctor 출력 비교가 잡았다.
    budget = catalog_mod.budget_report()
    sandbox = build_sandbox(cfg, settings)
    prompt_tokens = int(round(estimate_tokens(prompt_mod.build_agents_md(sandbox))))
    over = []
    if budget["total"] > budget["budget"]:
        over.append(f"툴 {budget['total']}/{budget['budget']}")
    if prompt_tokens > prompt_mod.PROMPT_TOKEN_BUDGET:
        over.append(f"프롬프트 {prompt_tokens}/{prompt_mod.PROMPT_TOKEN_BUDGET}")
    total = budget["total"] + prompt_tokens + catalog_mod.FRAMEWORK_TOKENS
    if over:
        report.warn("에이전트 예산", f"{', '.join(over)} 초과 — lt agent budget 로 무엇이 큰지 본다")
    else:
        report.ok("에이전트 예산", f"툴 {budget['count']}개 {budget['total']} + 프롬프트 {prompt_tokens} → 약 {total} 토큰")

    # ② 감옥이 실제로 서 있나. `Sandbox.build` 가 **없는 폴더를 버리므로**,
    #    오타 하나로 읽기가 통째로 비어도 조용하다.
    if not sandbox.read_roots:
        report.warn("에이전트 감옥", "허용 폴더가 하나도 없다 — [agent] read_roots 경로를 확인한다")
    elif not sandbox.write_roots:
        report.warn("에이전트 감옥", "쓰기 폴더가 없다 — 메모를 남길 수 없다")
    else:
        report.ok("에이전트 감옥", sandbox.describe().replace("\n", " · "))

    # ③ 워크스페이스 프롬프트가 코드와 같은가.
    workspace = Path(settings.workspace)
    if not workspace.is_absolute():
        workspace = cfg.root / workspace
    target = workspace / prompt_mod.WORKSPACE_FILE
    if not target.is_file():
        report.warn("에이전트 프롬프트", f"{target} 이 없다 — bash scripts/install-agent.sh")
    elif target.read_text(encoding="utf-8") != prompt_mod.build_agents_md(sandbox):
        # 게이트웨이가 워크스페이스를 다시 시드했거나 모델이 고쳐 썼을 수 있다.
        report.warn("에이전트 프롬프트", "코드가 만드는 내용과 다르다 — lt agent prompt 로 다시 쓴다")
    else:
        report.ok("에이전트 프롬프트", f"{target.name} 최신")

    # ④ Slack 위임이 실제로 닿는가.
    #
    # ★ 이 항목이 없어서 **위임이 조용히 강등된 채 몇 시간을 돌았다.** 개발 셸에는
    #   nvm PATH 가 있어 `openclaw` 가 찾아졌지만 systemd 서비스에는 없었다.
    #   로그에 WARNING 이 찍혔는데, 답이 빠르고 그럴듯해서 아무도 안 봤다.
    if settings.slack:
        from lifetrainer.agent import delegate

        binary = delegate.resolve_bin(settings.openclaw_bin)
        if not binary:
            report.fail(
                "에이전트 Slack 위임",
                "openclaw 실행 파일을 못 찾았다 — 자연어 DM 이 조용히 옛 경로로 간다. "
                "config 의 [agent] openclaw_bin 에 절대 경로를 넣는다",
            )
        elif not binary.startswith(("/usr/local/", "/usr/bin/")):
            # 서비스 PATH 밖(nvm 등)이면 찾아지긴 해도 버전 매니저에 묶여 있다.
            #
            # ★ 2026-08-28: 게이트웨이 유닛도 같이 본다. 유닛은 이미 시스템 Node 로
            #   **실행**되는데(`/usr/local/bin/node`), 실행하는 **스크립트**는 여전히
            #   nvm 안의 node_modules 다. 즉 nvm 을 지우면 CLI 뿐 아니라 게이트웨이도
            #   같이 죽는다. 여기를 안 보고 있어서 절반만 옮긴 상태가 안 드러났다.
            gw = ""
            try:
                gw = subprocess.run(
                    ["systemctl", "--user", "show", "openclaw-gateway", "-p", "ExecStart", "--value"],
                    capture_output=True, text=True, timeout=5, check=False,  # 없으면 빈 문자열이면 된다
                ).stdout
            except Exception as exc:  # noqa: BLE001
                gw = f"<확인 실패: {exc}>"
            if "/.nvm/" in gw:
                also = " · **게이트웨이 스크립트도 같은 곳에 있다**"
            elif gw.startswith("<확인 실패"):
                # ★ 못 봤으면 못 봤다고 말한다. 조용히 "괜찮음" 으로 넘어가면
                #   절반만 옮겨진 상태가 이번에도 안 드러난다.
                also = f" · 게이트웨이 경로 {gw}"
            else:
                also = ""
            report.warn(
                "에이전트 Slack 위임",
                f"{binary} — 버전 매니저 경로다. nvm 을 갈아엎으면 조용히 끊긴다{also}. "
                "옮기는 법: `bash life-trainer/deploy/install-openclaw-system.sh` (sudo 로 감싸지 말 것) "
                "(`operate/notes/agent-gateway.md §4-10`)",
            )
        else:
            report.ok("에이전트 Slack 위임", binary)
    else:
        report.ok("에이전트 Slack 위임", "꺼짐 ([agent] slack=false — 자연어는 빠른 경로로)")

    # ⑤ 압축 설정이 **지금의 ctx** 와 맞는가.
    #
    # ★ 이 검사가 없어서 대화가 한 시간쯤 이어지자 `Context overflow` 로 죽었다.
    #   08-15 에 ctx 40,960 기준으로 정한 값이 08-23 의 ctx 축소 뒤에도 남아 있었고,
    #   그때의 재계산이 **시스템 프롬프트를 빼먹었다** (HISTORY 2026-08-24).
    #   같은 누락이 `main` 에이전트를 이미 죽여 놨다 — 한 곳에서 본 원인을
    #   옆으로 밀어 보지 않은 것이 이 사고의 절반이다.
    _check_compaction(cfg, total, report)

    # ⑥ OpenClaw 쪽 등록. 설정 파일만 읽는다 — 게이트웨이를 부르지 않는다.
    openclaw_path = Path(cfg.slack.openclaw_config)
    if not openclaw_path.is_file():
        report.warn("에이전트 등록", f"{openclaw_path} 가 없다 — OpenClaw 가 설치되지 않았다")
        return
    try:
        raw = _json.loads(openclaw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        report.warn("에이전트 등록", f"openclaw.json 을 읽지 못했다: {exc}")
        return

    agents = [a.get("id") for a in (raw.get("agents") or {}).get("list") or []]
    server = ((raw.get("mcp") or {}).get("servers") or {}).get(catalog_mod.SERVER_NAME)
    missing = []
    if settings.agent_id not in agents:
        missing.append(f"에이전트 '{settings.agent_id}'")
    if not server:
        missing.append(f"MCP 서버 '{catalog_mod.SERVER_NAME}'")
    elif server.get("enabled") is False:
        missing.append(f"MCP 서버 '{catalog_mod.SERVER_NAME}' 가 꺼져 있다")
    if missing:
        report.warn("에이전트 등록", f"{', '.join(missing)} 없음 — bash scripts/install-agent.sh")
    else:
        report.ok("에이전트 등록", f"agent={settings.agent_id} · mcp={catalog_mod.SERVER_NAME}")


def _check_compaction(cfg: Config, prompt_tokens: int, report: Report) -> None:  # noqa: ANN001
    """`시스템 프롬프트 + keepRecent + reserve` 가 ctx 안에 드는가.

    OpenClaw 의 압축 설정은 **전역**이라 ctx 를 바꿔도 따라오지 않는다. 남는 자리가
    한 턴치도 안 되면 대화가 길어지는 순간 죽는다 — 그때 사용자가 보는 것은
    영어 오류 문장 하나뿐이다.
    """
    import json as _json

    path = Path(cfg.slack.openclaw_config)
    if not path.is_file():
        return
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return

    agents = raw.get("agents") or {}
    comp = (agents.get("defaults") or {}).get("compaction") or {}
    keep = int(comp.get("keepRecentTokens") or 0)
    reserve = int(comp.get("reserveTokens") or 0)
    models = ((raw.get("models") or {}).get("providers") or {}).get("llamacpp", {}).get("models") or []
    ctx = int(models[0].get("contextWindow") or 0) if models else 0
    if not (ctx and keep and reserve):
        return

    # 한 턴이 쓰는 최소치. 남는 자리가 이보다 작으면 곧 넘친다.
    headroom = ctx - (prompt_tokens + keep + reserve)
    detail = f"ctx {ctx} − (프롬프트 {prompt_tokens} + keepRecent {keep} + reserve {reserve}) = 여유 {headroom}"
    if headroom < 2000:
        report.fail(
            "에이전트 압축 여유",
            f"{detail} — 대화가 길어지면 Context overflow 로 죽는다. "
            "`openclaw config set agents.defaults.compaction.keepRecentTokens 3000` 등으로 낮춘다",
        )
    elif headroom < 5000:
        report.warn("에이전트 압축 여유", f"{detail} — 빠듯하다")
    else:
        report.ok("에이전트 압축 여유", detail)


# ★ 순서가 곧 화면 순서다. 옮기기 전 `cmd_doctor` 와 **같은 순서**로 둔다 —
#   출력이 한 글자라도 달라지면 옮기다 뭔가 흘린 것이다(그걸로 검증했다).
CHECKS = (
    check_activitywatch,
    check_llm,
    check_slack,
    check_font,
    check_disk,
    check_agent,
)


def run_all(cfg: Config) -> Report:
    """모든 점검을 돌려 `Report` 를 돌려준다. 화면 출력은 호출부(`cli.cmd_doctor`)가 한다.

    ★ 출력과 판정을 갈라 둔 이유: 판정만 테스트할 수 있어야 하기 때문이다.
      507줄 함수일 때는 그게 안 됐다.
    """
    report = Report()
    conn = check_db(cfg, report)
    try:
        check_activitywatch(cfg, report)
        check_data(cfg, report, conn)
        check_llm(cfg, report)
        check_slack(cfg, report)
        check_font(cfg, report)
        check_disk(cfg, report)
        check_queue(cfg, report, conn)
        check_embed(cfg, report, conn)
        check_agent(cfg, report)
        check_ci_reach(report)
    finally:
        if conn is not None:
            conn.close()
    return report
