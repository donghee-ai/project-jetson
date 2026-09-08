# `/planner` 로 열면 계획 수정·삭제·체크가 전부 조용히 실패했다

**발견** 2026-08-23 · 사용자가 "계획 삭제도 안되는데 체크도 안되고"
**증상** 페이지는 멀쩡히 보이는데 **버튼만 안 먹는다.** 에러 화면도, 실패 표시도 없다.

## 깨진 가정

> "앱을 접두사 아래에 mount 했으니 앱 전체가 그 아래로 간다."

서버 쪽은 맞다 — `_PrefixMiddleware` 가 `SCRIPT_NAME` 을 세팅하고 `url_for` 는
`/planner/d/...` 를 만든다. **틀린 것은 브라우저 쪽이다.** `planner.js` 는 URL 을
`url_for` 로 만들지 않고 **문자열 상수**로 갖고 있었다:

```js
api("/api/plan", { method: "POST", ... })
api("/api/plan/" + id, { method: "DELETE" })
fetch("/api/day/" + d)          // week.js
```

`lt.example.com/planner/d/2026-08-23` 에서 이 요청은 `lt.example.com/api/plan` 으로
나간다. 터널 ingress 는 `^/planner` 만 통과시키므로 **404 로 막힌다.**

```
POST https://lt.example.com/api/plan/4/check          → 404   (터널이 차단)
POST https://lt.example.com/planner/api/plan/4/check  → 401   (도달함. 세션 필요)
```

## 왜 아무도 못 봤나

- **서버 렌더링된 것은 다 멀쩡했다.** 격자·계획 목록·기기 카드는 전부 서버가 그려서
  보내므로 화면상 아무 이상이 없다. 실패하는 것은 **사용자가 누를 때뿐**이다
- tailnet 직결(`100.64.0.2:8770`)에서는 접두사가 없어 **정상 동작한다.** 개발·검증을
  거기서 하면 영영 안 보인다
- 노출 경계 테스트는 GET 만 봤다 — 401/404 가 나오는지는 봤지만
  **성공 경로에서 JS 가 어디로 요청하는지**는 안 봤다

## 고친 방법

접두사를 **서버가 페이지에 실어 보내고** JS 가 그것을 붙인다:

```html
<div id="app-base" data-base="{{ url_prefix }}" hidden></div>   <!-- request.script_root -->
```
```js
if (url.charAt(0) === "/") url = BASE + url;
```

`planner.js`·`week.js` 둘 다. 회귀 테스트는 **페이지에 접두사가 실려 나가는지**를 본다 —
tailnet 은 `data-base=""`, 접두사 경로는 `data-base="/planner"`.

## 교훈

**mount 경로를 바꾸는 것은 서버만의 일이 아니다.** 클라이언트가 URL 을 문자열로 들고
있으면 서버 쪽 재배치가 조용히 어긋난다. 그리고 그 실패는 **화면에 안 나타난다** —
이 저장소의 "테스트 통과 ≠ 동작"과 같은 부류이되, 더 나쁜 쪽이다. 산출물을 열어봐도
안 보이고 **눌러봐야** 보인다.
