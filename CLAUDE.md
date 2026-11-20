# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It is the contributor guide and is written in Korean; the user-facing docs are `README.md` (English) and `README.ko.md`.

## 프로젝트 개요

Claude Code(Pro/Max)의 사용 한도를 바탕화면에 상시 표시하는 **Windows 전용** tkinter 위젯. Python 표준 라이브러리만 사용한다 (pip 의존성 추가 금지가 설계 원칙).

Windows 전용인 이유는 세 가지다: Tk의 `-transparentcolor` 속성, `msvcrt` 파일 락, 시작 프로그램 폴더. `main.py`의 `__main__` 가드가 `sys.platform != "win32"`면 안내 후 `exit(1)` 한다 — 가드를 빼면 `-transparentcolor`에서 `TclError`로 조용히 죽는다.

## 명령어

```bash
# 문법 검사
python -m py_compile main.py usage_api.py i18n.py theme.py history.py

# API 호출·파싱 단독 테스트 (위젯 없이 실측값 출력). 인자로 언어 코드 지정 가능
PYTHONIOENCODING=utf-8 python usage_api.py
PYTHONIOENCODING=utf-8 python usage_api.py ko

# 위젯 실행 (개발 시)
python main.py

# 콘솔창 없는 실행 (사용자용)
run_widget.bat
```

테스트 프레임워크는 없다. 검증은 `usage_api.py` 단독 실행으로 실측한 뒤 위젯을 띄워 눈으로 확인하고, 값이 Claude Code의 `/usage` 명령과 일치하는지 대조하는 방식이다.

**개발 시 함정**: 중복 실행 방지 락(아래) 때문에 위젯이 이미 떠 있으면 `python main.py`가 **조용히 종료된다**(exit 0, 출력 없음). 코드를 고쳤는데 반영이 안 되는 것처럼 보이면 기존 인스턴스부터 확인할 것.

## 아키텍처

두 계층으로 엄격히 분리되어 있으며 이 경계를 넘는 코드를 만들지 말 것:

- **`usage_api.py`** — 데이터 계층. `~/.claude/.credentials.json`에서 OAuth 토큰을 읽어 `GET https://api.anthropic.com/api/oauth/usage`를 호출하고, 응답을 `[{"id", "label", "used_pct", "resets_at"}]` 리스트로 정규화. 오류는 구분된 예외(`TokenMissingError`/`TokenExpiredError`/`RateLimitedError`/`NetworkError`/`ParseError`)로 던진다.
- **`main.py`** — UI 계층. 무테두리 tkinter 위젯 + 폴링. API 호출은 백그라운드 스레드에서, UI 갱신은 `queue` → `root.after()` 경유로 **메인 스레드에서만**. 설정(창 위치 등)은 `config.json`에 자동 저장.

여기에 leaf 모듈이 셋 붙는다:

- **`i18n.py`** — 문자열 테이블(EN/KO). 두 계층 모두 import 하지만 **자신은 어느 쪽도 import 하지 않는다**. 이 방향을 뒤집으면 계층 경계가 깨진다.
  - 새 UI 문자열은 하드코딩하지 말고 EN·KO 양쪽에 키를 추가한다.
  - **문자열이 아니라 키를 들고 다닐 것.** 모듈 로드 시점에 문자열로 굳히면 `language` 설정이 안 먹는다 (`main.py`의 `ERROR_KEYS`, `usage_api.py`의 `KIND_KEYS`/`LEGACY_KEYS`가 그래서 키 매핑이다).
  - `i18n.t()`는 절대 던지지 않는다 — 없는 키는 키 자체를, 포맷 실패는 원본 템플릿을 돌려준다.
  - 언어는 `load_config()`가 딱 한 번 적용한다(`i18n.set_language`). 설정을 읽는 유일한 지점이라 UI 생성 전 호출 순서가 보장된다.
- **`history.py`** — 주간 누적 % 기록(`usage_history.json`, 추적 안 함)과 요일별 사용량 계산. API가 하루 단위 값을 주지 않아서 위젯이 직접 기록한다. 읽기·쓰기 실패는 던지지 않는다(불변조건 5). 기록은 메인 스레드(`_drain_queue`)에서만 쓴다.
- **`theme.py`** — 색·글꼴·치수 토큰. `main.py`만 import 하고, 자신은 아무것도 import 하지 않는다. 기준 문서는 루트의 `DESIGN.md`이고 두 곳이 어긋나면 `DESIGN.md`가 맞다.
  - 색은 전부 hex로 미리 계산해 둔다(tkinter에 투명도·color-mix·그림자 없음). `theme.KEY`(투명 키)와 같은 색을 팔레트에 넣지 말 것.
  - UI는 카드 전체를 `Canvas` 하나에 매번 다시 그린다(`Widget._redraw`). 위젯을 부분 갱신하는 코드를 따로 만들지 말고 상태를 바꾼 뒤 `_redraw()`를 부른다.

### 예외 계약

`usage_api`는 **`UsageError` 계열만 던지는 것을 지향**하되 단정하지 않는다 — `_refresh_access_token`의 docstring이 자기가 못 잡는 예외(`http.client.IncompleteRead` 등)를 스스로 공시한다. 그 서술을 임의로 "안전하다"로 고쳐 쓰지 말 것. 사실이 아니면 사실대로 적는 게 이 파일의 규칙이다.

최종 방어선은 `main.py` `_poll_loop`의 `except Exception`이다. 이게 없으면 미처리 예외에 **폴링 스레드가 조용히 죽어** 위젯이 낡은 값에서 멈추고 오류 표시도, 수동 새로고침도 안 먹는다(실제 있었던 버그). 상태줄 메시지는 `ERROR_KEYS`에 매핑된 것만 쓰고, 미지의 예외는 `UNEXPECTED_ERROR_KEY`로 뭉갠다 — `str(e)`를 그대로 띄우면 토큰이 샐 수 있다(불변조건 2).

### 중복 실행 방지

`acquire_single_instance_lock()`이 `.instance.lock`에 `msvcrt` 배타 락을 건다. 이미 실행 중이면 `None` → `sys.exit(0)`으로 **조용히 종료**(`Widget()` 생성 전이라 창이 깜빡이지도 않는다).

- 락 핸들은 `Widget._lock`으로 붙든다. GC로 닫히면 락이 풀린다.
- 강제 종료돼도 OS가 핸들을 닫으며 락이 풀린다 — **락 파일 존재 여부로 판단하지 말 것**. 파일은 남는다. 파일 기반 판정으로 바꾸면 "위젯이 영영 안 뜨는" 최악의 회귀가 된다.
- 락 로직 자체가 실패하면 `_LOCK_SKIPPED`로 **중복 방지를 포기하고 위젯은 띄운다**. 중복 창 < 위젯이 안 뜸 (불변조건 5).
- 락 파일은 `APP_DIR` 기준이다. 저장소를 다른 폴더로 복사하면 락도 분리되므로, 사용자의 인스턴스를 건드리지 않고 사본으로 검증할 수 있다.

### API 응답 파싱의 핵심

- 응답의 **`limits` 배열이 1차 소스**다: `{kind: session|weekly_all|weekly_scoped, percent, resets_at, scope.model.display_name}`. 최상위 키(`five_hour`, `seven_day`)는 구식 폴백(`_parse_legacy`).
- 모델별 한도(예: "Opus 5 weekly")는 `weekly_scoped` 항목으로 동적으로 온다 — 항목 수나 모델명을 하드코딩하지 말 것. 미지의 `kind`도 버리지 않고 kind 문자열을 라벨로 쓴다.
- 특정 행을 숨기고 싶으면 코드가 아니라 `config.json`의 `hidden_labels`를 쓴다 (라벨 부분일치, 대소문자 무시). **모델명 필터를 코드에 넣지 말 것** — 예전에 Fable 5 전용 날짜 상수가 박혀 있었고, 그게 개인 설정이 코드로 샌 사례다.
- `percent`/`utilization`은 **사용한 %** 다. 위젯도 `/usage`와 동일하게 사용한 %로 표시한다. (초기 버전에서 "남은 %"로 변환해 표시했다가 /usage와 안 맞는 버그가 있었음 — 반복 금지)

## 설정 (`config.json`)

`config.json`은 **추적하지 않는다**(사용자별 상태). 견본은 `config.example.json`이고, 파일이 없어도 `DEFAULT_CONFIG`로 정상 기동한다. 새 설정을 추가할 때는 `DEFAULT_CONFIG` → `load_config()` 검증 → `config.example.json` → README 두 개를 한 묶음으로 갱신할 것.

| 키 | 기본값 | 의미 |
|---|---|---|
| `x`, `y` | `null` | 창 위치 (드래그하면 자동 저장) |
| `topmost` | `true` | 항상 위 |
| `minimized` | `false` | 알약 배지로 축소된 상태 |
| `poll_interval` | `180` | 폴링 간격(초). **180 미만으로 내려가지 않는다** (불변조건 4) |
| `automation_reserve_pct` | `5` | 자동화 1개당 예약할 세션 한도 % (0~100으로 클램프) |
| `language` | `"en"` | `"en"` 또는 `"ko"`. 모르는 값은 조용히 영어 |
| `hidden_labels` | `[]` | 이 문자열이 라벨에 들어간 행을 숨김 (부분일치·대소문자 무시). `kind:<종류>`(예: `kind:session`)는 라벨 대신 id의 종류로, `model:<모델명>`(예: `model:opus`)은 id의 모델명과 정확히 같을 때만 맞춘다. 리스트가 아니면 `[]` |
| `auto_refresh_token` | `true` | 401 시 credentials 파일에 재발급 토큰을 쓸지 여부 |
| `theme` | `"light"` | `"light"` 또는 `"dark"`. 모르는 값은 밝은 테마 |

`load_config()`는 손으로 고친 값 때문에 위젯이 안 뜨는 일이 없도록 전부 강제 변환·클램프한다 (불변조건 5).

## 불변조건 (수정 시 절대 깨지 말 것)

1. `~/.claude/.credentials.json`은 **원칙적으로 읽기 전용** — 매 폴링마다 다시 읽어 Claude Code가 갱신한 토큰을 반영한다. 쓰기는 **401 응답 시 자동 재발급 경로 한 곳뿐**이며 다음 조건을 모두 지킨다:
   - `auto_refresh_token`이 `false`면(`fetch_usage(allow_refresh=False)`) 이 경로 자체가 비활성이다. **쓰기 경로를 여기 말고 다른 곳에 만들면 이 스위치가 거짓말이 된다.**
   - 401을 받으면 재발급 전에 credentials를 **다시 읽어** accessToken이 바뀌었는지 확인한다. 바뀌었으면(Claude Code가 이미 갱신) 그 토큰으로 재요청만 하고 재발급하지 않는다 — refreshToken 회전으로 CLI를 로그아웃시키는 것을 막기 위함. 이 재확인은 읽기 전용이라 `allow_refresh`와 무관하게 항상 수행한다.
   - 403은 만료가 아니므로 재발급 경로를 타지 않는다 (401만).
   - 쓰기는 같은 디렉터리 임시 파일 + `os.replace`로 **원자적**이어야 한다 (Claude Code가 동시에 읽어도 손상 없게).
   - 재발급 실패 시 예전처럼 `TokenExpiredError`("run Claude Code once")로 폴백한다.
   - 재발급 엔드포인트(`console.anthropic.com/v1/oauth/token`)는 비공식이므로 언제든 막힐 수 있다.
2. 토큰 값을 로그·화면·예외 메시지·설정 파일에 노출하지 않는다.
3. 필수 헤더 3종을 유지한다: `Authorization: Bearer`, `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/2.0.0`. **특히 User-Agent가 없으면 지속적 429가 발생하는 것이 검증된 사실.**
4. 폴링 간격 180초 미만 금지 (429 유발). 429 시 지수 백오프(2배씩, 최대 8배).
5. 어떤 오류에도 크래시하지 않고 상태줄 메시지로 표시한다. 설정값·언어 코드·응답 형식 어느 것도 위젯이 안 뜨는 이유가 되면 안 된다.
6. 비공식 엔드포인트이므로 파싱은 방어적으로 — 형식이 바뀌면 `ParseError`로 "⚠ Response format changed"가 표시되게 한다.
7. `_toggle_autostart`가 만드는 시작 프로그램 `.bat`은 **UTF-8(BOM 없이) + `chcp 65001 >nul`** 조합을 유지한다. cmd는 현재 코드페이지로 각 줄을 해석하므로 파일 인코딩과 `chcp` 줄의 **짝이 어긋나면 한글 경로가 깨져 실행이 실패**한다. 이 조합은 코드페이지 949·65001 양쪽에서 실측 확인했다 — 한쪽만 바꾸지 말 것.

## 검증할 때 주의

- **코드페이지를 파이프로 확인하지 말 것.** `cmd /c chcp`를 도구·파이프 너머로 실행하면 상속된 값(65001 등)이 나와 실제 콘솔(949)과 다르다. `.bat` 동작은 반드시 **실제 콘솔에서 실행**해 확인한다. 이 착각으로 멀쩡한 인코딩을 버그로 오진한 이력이 있다.
- 코드만 읽고 원인을 단정하지 말 것. 위 `.bat` 버그의 진짜 원인은 인코딩이 아니라 **경로 오타**였고, `pythonw`는 콘솔이 없어 실패해도 조용하다.
- 검증용으로 띄운 위젯 프로세스는 반드시 정리한다. 사용자가 쓰는 인스턴스를 죽이지 말 것 — 저장소 사본을 다른 폴더에서 띄우면 락이 분리된다.
- 언어를 건드렸으면 `python usage_api.py ko`와 위젯 양쪽에서 확인한다. 데이터 계층에도 번역되는 라벨이 있다.

## 에이전트 팀

`.claude/agents/`에 프로젝트 전용 서브에이전트가 팀별 폴더로 정의되어 있다:

- **`eval-team/`** (평가, 읽기 전용): `eval-accuracy`(표시값·파싱 정확성), `eval-code-quality`(스레드 안전성·보안 리뷰), `eval-ux`(UI/UX 평가)
- **`maint-team/`** (유지보수): `maint-bugfix`(재현→원인→최소 수정), `maint-feature`(기능 추가), `maint-verify`(수정 후 검증)

버그 수정이나 기능 추가 후에는 `maint-verify`로 검증하는 흐름을 권장한다.
