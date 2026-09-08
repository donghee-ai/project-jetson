"""`POST /ingest/aw` — 폰이 밀어 넣는 경로.

**이 앱에서 공개 인터넷으로 열리는 유일한 엔드포인트다.** 그래서 "잘 들어간다"보다
"거절해야 할 것을 거절한다"를 더 많이 검사한다.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lifetrainer import db
from lifetrainer.collect import ingest
from lifetrainer.config import load_config
from lifetrainer.web import auth
from lifetrainer.web.app import create_app

PHONE = "phone-example"
ANDROID_BUCKET = "aw-watcher-android"


def _payload(*, hostname: str = PHONE, ts: str = "2026-08-19T09:00:00+00:00") -> dict:
    """aw-android 의 export 형식 그대로 (`/api/0/export` 출력 모양)."""
    return {
        "buckets": {
            ANDROID_BUCKET: {
                "id": ANDROID_BUCKET,
                "type": "currentwindow",
                "client": "aw-android",
                "hostname": hostname,
                "events": [
                    {"timestamp": ts, "duration": 120.0, "data": {"app": "com.kakao.talk"}},
                ],
            }
        }
    }


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path,
        db_path=tmp_path / "lt.db",
        ingest=replace(base.ingest, enabled=True, secret="test-ingest-secret"),
    )


@pytest.fixture()
def client(cfg):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    conn = db.open_db(cfg)  # 스키마 생성
    conn.close()
    return app.test_client()


def _post(client, cfg, payload, *, device=PHONE, ts=1_000_000.0, nonce="n1", secret=None):
    body = json.dumps(payload).encode("utf-8")
    secret = secret if secret is not None else cfg.ingest.secret
    sig = auth.sign_ingest(secret, device, ts, nonce, body)
    return client.post(
        "/ingest/aw",
        data=body,
        headers={
            "Authorization": f"{auth.INGEST_AUTH_SCHEME} {device}:{int(ts)}:{nonce}:{sig}",
            "Content-Type": "application/json",
        },
    )


def _register_phone(cfg, name=PHONE):
    conn = db.open_db(cfg)
    with db.transaction(conn):
        db.upsert_device(conn, name, kind="phone")
    conn.close()


# ── 인증 ─────────────────────────────────────────────────────────────────


def test_rejects_without_auth_header(client, cfg):
    resp = client.post("/ingest/aw", data=json.dumps(_payload()))
    assert resp.status_code == 401


def test_rejects_wrong_secret(client, cfg, monkeypatch):
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload(), secret="틀린-비밀키")
    assert resp.status_code == 401


def test_rejects_tampered_body(client, cfg, monkeypatch):
    """서명은 **본문에 묶여 있다** — 가로채서 내용만 바꾸면 통과하지 못한다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    body = json.dumps(_payload()).encode("utf-8")
    sig = auth.sign_ingest(cfg.ingest.secret, PHONE, 1_000_000.0, "n1", body)
    tampered = json.dumps(_payload(ts="2026-08-19T23:00:00+00:00")).encode("utf-8")
    resp = client.post(
        "/ingest/aw",
        data=tampered,
        headers={"Authorization": f"{auth.INGEST_AUTH_SCHEME} {PHONE}:1000000:n1:{sig}"},
    )
    assert resp.status_code == 401


def test_rejects_stale_timestamp(client, cfg, monkeypatch):
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    # 허용 오차(기본 300초) 밖
    resp = _post(client, cfg, _payload(), ts=1_000_000.0 - 3600)
    assert resp.status_code == 401
    assert "오차" in resp.get_json()["message"]


def test_rejects_replay(client, cfg, monkeypatch):
    """같은 nonce 를 두 번 쓰면 두 번째는 거절된다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    assert _post(client, cfg, _payload(), nonce="same").status_code == 200
    resp = _post(client, cfg, _payload(), nonce="same")
    assert resp.status_code == 401
    assert "재전송" in resp.get_json()["message"]


# ── 크기·형식·기기 ────────────────────────────────────────────────────────


def test_rejects_oversized_body(client, cfg, monkeypatch):
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    fat = {"buckets": {ANDROID_BUCKET: {"type": "currentwindow", "pad": "x" * 200}}}
    small_cfg = replace(cfg, ingest=replace(cfg.ingest, max_body_bytes=100))
    app = create_app(small_cfg)
    resp = _post(app.test_client(), small_cfg, fat)
    assert resp.status_code == 413


def test_rejects_unregistered_device(client, cfg, monkeypatch):
    """네트워크 경로는 **이미 등록된 기기만** 받는다 (비밀키가 새도 두 번째 방어선)."""
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload(), device="처음보는폰")
    assert resp.status_code == 400
    assert "등록되지 않은" in resp.get_json()["message"]


def test_disabled_by_default(cfg, monkeypatch):
    off = replace(cfg, ingest=replace(cfg.ingest, enabled=False))
    resp = _post(create_app(off).test_client(), off, _payload())
    assert resp.status_code == 404


# ── 실제 수신 ─────────────────────────────────────────────────────────────


def test_accepts_and_stores_as_android_type(client, cfg, monkeypatch):
    """폰 버킷은 `window` 가 아니라 `android` 로 저장되고 그 기기에 묶인다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload())
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["events"] == 1

    conn = db.open_db(cfg)
    row = conn.execute(
        "SELECT b.type, d.name, d.kind FROM aw_bucket b JOIN device d ON d.id = b.device_id "
        "WHERE b.bucket_id = ?",
        (ANDROID_BUCKET,),
    ).fetchone()
    assert (row["type"], row["name"], row["kind"]) == ("android", PHONE, "phone")
    assert conn.execute("SELECT COUNT(*) c FROM aw_event").fetchone()["c"] == 1
    conn.close()


def test_resend_is_idempotent(client, cfg, monkeypatch):
    """`aw_event` PK 가 `(bucket_id, ts)` 라 같은 데이터를 다시 보내도 늘지 않는다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    assert _post(client, cfg, _payload(), nonce="a").status_code == 200
    assert _post(client, cfg, _payload(), nonce="b").status_code == 200

    conn = db.open_db(cfg)
    assert conn.execute("SELECT COUNT(*) c FROM aw_event").fetchone()["c"] == 1
    conn.close()


def test_signed_device_wins_over_bucket_hostname(client, cfg, monkeypatch):
    """서명된 기기 이름이 정본이다 — 메타의 hostname 이 달라도 이름이 갈라지지 않는다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload(hostname="다른이름"))
    assert resp.status_code == 200

    conn = db.open_db(cfg)
    names = [r["name"] for r in conn.execute("SELECT name FROM device").fetchall()]
    assert names == [PHONE]
    conn.close()


# ── nonce 청소 ────────────────────────────────────────────────────────────


def test_nonces_are_pruned(client, cfg, monkeypatch):
    """nonce 가 `sync_state` 에 영영 쌓이면 5분마다 한 행씩 늘어난다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    _post(client, cfg, _payload(), nonce="old")

    def _count():
        conn = db.open_db(cfg)
        n = conn.execute(
            "SELECT COUNT(*) c FROM sync_state WHERE key LIKE 'ingest_nonce:%'"
        ).fetchone()["c"]
        conn.close()
        return n

    assert _count() == 1
    # 창을 훌쩍 넘긴 뒤 새 요청이 오면 옛 nonce 는 청소된다.
    monkeypatch.setattr("time.time", lambda: 1_000_000.0 + 10_000)
    _post(client, cfg, _payload(), ts=1_000_000.0 + 10_000, nonce="new")
    assert _count() == 1


# ── ingest 모듈 직접 ──────────────────────────────────────────────────────


# ── 수신 뒤 롤업 (2026-08-22) ─────────────────────────────────────────────
# 전에는 apply_payload 만 하고 끝냈다. 표 반영을 10분짜리 sync 타이머에 맡겼는데
# 그 타이머는 **오늘만** 롤업해서, 늦게 도착한 어제치가 영영 안 나타났다.


def test_ingest_rolls_up_the_day_it_touched(client, cfg, monkeypatch):
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload(ts="2026-08-19T09:00:00+00:00"))
    assert resp.status_code == 200

    # 09:00Z = 18:00 KST → 논리적 하루 2026-08-19
    assert resp.get_json()["rolled"] == ["2026-08-19"]

    conn = db.open_db(cfg)
    rows = conn.execute(
        "SELECT count(*) FROM slot_breakdown WHERE day = '2026-08-19'"
    ).fetchone()[0]
    conn.close()
    assert rows > 0, "수신했는데 슬롯이 안 생겼다"


def test_ingest_attributes_slots_to_the_sending_device(client, cfg, monkeypatch):
    """★ 이 값이 NULL 이면 웹이 폰과 노트북을 구분할 수 없다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    assert _post(client, cfg, _payload()).status_code == 200

    conn = db.open_db(cfg)
    devices = conn.execute(
        "SELECT DISTINCT d.name FROM slot_breakdown b JOIN device d ON d.id = b.device_id"
    ).fetchall()
    conn.close()
    assert [r[0] for r in devices] == [PHONE]


def test_late_arriving_yesterday_is_rolled_too(client, cfg, monkeypatch):
    """폰은 Doze 로 밀려 어제치를 실어 온다. sync 타이머는 오늘만 돌아서 못 잡는다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    resp = _post(client, cfg, _payload(ts="2026-08-18T09:00:00+00:00"), nonce="n-old")
    assert resp.get_json()["rolled"] == ["2026-08-18"]


def test_rollup_failure_still_returns_200(client, cfg, monkeypatch):
    """이벤트는 이미 커밋됐다. 롤업 실패로 폰에게 재전송을 시키면 같은 게 또 올 뿐이다."""
    _register_phone(cfg)
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    import lifetrainer.web.app as web_app

    monkeypatch.setattr(
        web_app, "rollup_day", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    resp = _post(client, cfg, _payload())
    assert resp.status_code == 200
    assert resp.get_json()["rolled"] == []


def test_unknown_bucket_is_skipped_not_fatal(cfg):
    """상류가 새 워처를 붙여도 폰 수신 전체가 실패하면 안 된다."""
    conn = db.open_db(cfg)
    with db.transaction(conn):
        db.upsert_device(conn, PHONE, kind="phone")
    payload = _payload()
    payload["buckets"]["aw-watcher-미래것"] = {"type": "무언가", "events": []}
    with db.transaction(conn):
        result = ingest.apply_payload(conn, payload, device=PHONE)
    assert result.buckets == 1
    assert len(result.skipped) == 1
    conn.close()


def test_android_app_becomes_package_name(cfg):
    """★ 규칙은 **패키지명**에 걸려야 한다.

    aw-android 는 `data.app` 에 사람이 읽는 라벨("카카오톡")을, `data.package` 에
    패키지명을 넣는다. 라벨에 규칙을 걸면 폰 언어를 바꾸는 순간 조용히 안 맞는다.
    """
    conn = db.open_db(cfg)
    with db.transaction(conn):
        db.upsert_device(conn, PHONE, kind="phone")
    payload = _payload()
    payload["buckets"][ANDROID_BUCKET]["events"][0]["data"] = {
        "app": "카카오톡",
        "package": "com.kakao.talk",
        "classname": "com.kakao.talk.activity.main.MainActivity",
    }
    with db.transaction(conn):
        ingest.apply_payload(conn, payload, device=PHONE)

    row = conn.execute("SELECT app, title FROM aw_event").fetchone()
    assert row["app"] == "com.kakao.talk"   # 규칙이 걸리는 쪽
    assert row["title"] == "카카오톡"        # 사람이 읽는 쪽
    conn.close()


def test_android_without_package_keeps_label(cfg):
    """상류가 형태를 바꿔 `package` 가 없으면 라벨이라도 남긴다."""
    conn = db.open_db(cfg)
    with db.transaction(conn):
        db.upsert_device(conn, PHONE, kind="phone")
    payload = _payload()
    payload["buckets"][ANDROID_BUCKET]["events"][0]["data"] = {"app": "카카오톡"}
    with db.transaction(conn):
        ingest.apply_payload(conn, payload, device=PHONE)
    assert conn.execute("SELECT app FROM aw_event").fetchone()["app"] == "카카오톡"
    conn.close()


def test_infer_device_name_refuses_when_ambiguous():
    """hostname 이 섞여 있으면 고르지 않는다 — 두 기기가 한 이름으로 합쳐지면 못 되돌린다."""
    payload = _payload()
    payload["buckets"]["aw-watcher-window_pc"] = {
        "type": "currentwindow",
        "hostname": "DESKTOP-A",
        "events": [],
    }
    assert ingest.infer_device_name(payload) is None
    assert ingest.infer_device_name(_payload()) == PHONE
