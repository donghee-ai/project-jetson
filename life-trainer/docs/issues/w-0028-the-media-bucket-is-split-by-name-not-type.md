# 미디어 버킷을 이름으로 가른다 — 타입 분기를 두지 않기로 했다

- **번호**: `0028`
- **상태**: 🟣 `w` — watched. **고칠 수 있지만 재 보니 지금이 낫다.**
- **발견일**: 2026-09-04 (폰 쪽 작업 중 "고쳐야 한다"고 적었다가 재판정)

## 무엇이 "문제"인가

`aw-watcher-android-media` 가 `aw_bucket.type='android'` 로 들어간다. 앱 세션과 같은
타입이라 롤업이 **버킷 이름의 `-media` 접미사로** 갈라야 한다:

```python
def _bucket_kind(ev: dict) -> str:
    if str(ev.get("bucket_id") or "").endswith("-media"):
        return "media"
    return ev["btype"]
```

이름으로 가르는 것은 타입으로 가르는 것보다 약하다. 워처가 버킷 이름을 바꾸면 조용히 깨진다.

## 왜 안 고치나 — 재 본 것

`android/docs/phone-titles.md` §F4-b 가 *"워처 등록을 `type='media'` 로 바꾸면 그 한 줄이
사라진다"* 고 적어 뒀다. **세 가지가 틀렸다.**

**1. 폰은 이미 그렇게 보내고 있다.**

```
$ curl -H "Authorization: Bearer …" http://127.0.0.1:15600/api/0/buckets/
aw-watcher-android-media   type=media.playback
```

젯슨 `bucket_type()` 은 **메타 타입을 안 본다.** 버킷 ID 문자열로만 가르고, 그건
의도된 설계다 — 데스크톱 창 워처도 타입이 `"currentwindow"` 라 메타로는 안드로이드를
구분할 수 없기 때문이다. **폰을 고쳐도 아무것도 안 바뀐다.**

**2. `media` 분기를 더하면 기기 종류가 틀어진다.**

`device_kind_for()` 가 보는 `_PHONE_BUCKET_TYPES = {"android", "unlock"}` 에 `media` 가
없다. 미디어 버킷이 `media` 로 잡히면 그 경로에서 **`laptop` 으로 떨어진다** —
폰이 노트북으로 등록될 수 있다.

**3. 그 부재를 지키는 테스트가 이미 둘 있다.**

`tests/test_aw_sync.py::test_bucket_type_android_media_is_android` 는 근거까지 적어 뒀다:

> 미디어 재생 구간은 앱 세션과 겹치므로 `_activity_intervals` 의 `_union` 이 흡수한다.
> 합성 회귀 데이터로 앱 세션과 media 버킷이 이중 계산되지 않음을 확인했다.

즉 **이름으로 가르는 지금 방식이 재 보고 고른 것**이고, 타입 분기는 그 판단을 뒤집는다.

## 그래서 무엇을 했나

`phone-titles.md` §F4-b 의 잘못된 지시를 지웠다. 그대로 두면 다음 사람이 폰을 고치고
빌드하고 설치한 뒤 아무것도 안 바뀌는 것을 보게 된다.

## 언제 다시 볼까

- 워처가 버킷 **이름**을 바꿔야 할 일이 생기면 (그때 이름 규칙이 실제로 깨진다)
- `device_kind_for` 가 타입 목록이 아니라 다른 것으로 기기를 판정하게 바뀌면
