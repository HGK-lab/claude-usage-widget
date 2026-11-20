"""UI 문자열 테이블 (en/ko).

main.py(UI)와 usage_api.py(데이터) 양쪽이 import 하는 leaf 모듈이다. 두 계층
사이의 의존을 만들지 않으려고 여기에 둔다 — i18n은 어느 쪽도 import 하지 않는다.

t()는 절대 예외를 던지지 않는다. 없는 키나 어긋난 포맷 인자는 그대로 통과시켜
화면에 이상한 문자열을 띄울지언정 위젯을 죽이지 않는다 (불변조건 5).
"""

EN = {
    "app.title": "Claude Usage",

    # 한도 라벨 (usage_api가 조회)
    "limit.session": "Session (5h)",
    "limit.weekly_all": "Weekly (all)",
    "limit.weekly_scoped": "{model} weekly",

    # 초기화 시각
    "ampm.am": "AM",
    "ampm.pm": "PM",
    "time.hm": "{hour}:{minute:02d} {ampm}",
    "time.h": "{hour} {ampm}",
    # 리셋까지 남은 시간(왼쪽)과 리셋 시각(오른쪽)
    "reset.in.dh": "resets in {days}d {hours}h",
    "reset.in.hm": "resets in {hours}h {minutes}m",
    "reset.in.m": "resets in {minutes}m",
    "reset.at.later": "{weekday} {month}/{day} {time}",
    "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],

    # 상태줄
    "loading": "Loading…",
    "refreshing": "Refreshing…",
    "status.line": "{fresh} · every {interval} min",
    "status.fresh.now": "just updated",
    "status.fresh.mins": "updated {mins} min ago",
    "status.reserve_warn": "Into the {pct}% kept for {count} automations",
    "status.autostart_failed": "Could not change startup setting",

    # 최소화 배지
    "pill.init": "—%",
    "pill.sub": "weekly {pct:.0f}%",

    # 한도 행 라벨
    "flag.high": "over 70%",
    "flag.critical": "over 90%",
    "flag.reserve": "into reserve",
    "budget.left": "{days}d left",
    "budget.per_day": "≤{pct}% a day",
    "stale.age": "last value {mins} min ago",
    "stale.stamp": "not updated",

    # 우클릭 메뉴
    "menu.refresh": "Refresh",
    "menu.minimize": "Minimize / restore",
    "menu.topmost": "Always on top",
    "menu.autostart": "Run at startup",
    "menu.dark": "Dark theme",
    "menu.quit": "Quit",

    # 오류 (예외 문자열은 쓰지 않는다 — 불변조건 2)
    "error.token_missing": "Not signed in — log in with Claude Code",
    "error.token_expired": "Token expired — run Claude Code once",
    "error.rate_limited": "Rate limited — backing off",
    "error.network": "Offline — will retry",
    "error.parse": "⚠ Response format changed",
    "error.unexpected": "Unexpected error — will retry",
}

KO = {
    "app.title": "Claude 사용량",

    "limit.session": "세션 (5시간)",
    "limit.weekly_all": "주간 (전체)",
    "limit.weekly_scoped": "{model} 주간",

    "ampm.am": "오전",
    "ampm.pm": "오후",
    "time.hm": "{ampm} {hour}:{minute:02d}",
    "time.h": "{ampm} {hour}시",
    "reset.in.dh": "{days}일 {hours}시간 뒤 리셋",
    "reset.in.hm": "{hours}시간 {minutes}분 뒤 리셋",
    "reset.in.m": "{minutes}분 뒤 리셋",
    "reset.at.later": "{month}/{day} {weekday} {time}",
    "weekdays": ["월", "화", "수", "목", "금", "토", "일"],

    "loading": "불러오는 중…",
    "refreshing": "갱신 중…",
    "status.line": "{fresh}, {interval}분마다",
    "status.fresh.now": "방금 갱신",
    "status.fresh.mins": "{mins}분 전 갱신",
    "status.reserve_warn": "자동화 {count}개 몫 {pct}%까지 침범",
    "status.autostart_failed": "자동 실행 설정 실패",

    "pill.init": "—%",
    "pill.sub": "주간 {pct:.0f}%",

    "flag.high": "70% 넘음",
    "flag.critical": "90% 넘음",
    "flag.reserve": "예약분 침범",
    "budget.left": "남은 {days}일",
    "budget.per_day": "하루 {pct}%까지",
    "stale.age": "마지막 값 {mins}분 전",
    "stale.stamp": "갱신 못 함",

    "menu.refresh": "새로고침",
    "menu.minimize": "최소화/복원",
    "menu.topmost": "항상 위",
    "menu.autostart": "시작 시 자동 실행",
    "menu.dark": "어두운 테마",
    "menu.quit": "종료",

    "error.token_missing": "로그인 정보 없음 — Claude Code 로그인 필요",
    "error.token_expired": "토큰 만료 — Claude Code를 한 번 실행하세요",
    "error.rate_limited": "요청 제한 — 간격을 늘려 재시도",
    "error.network": "오프라인 — 자동 재시도 예정",
    "error.parse": "⚠ 데이터 형식 변경됨",
    "error.unexpected": "예기치 못한 오류 — 자동 재시도 예정",
}

LANGUAGES = {"en": EN, "ko": KO}
DEFAULT_LANGUAGE = "en"

_active = EN


def set_language(code) -> str:
    """활성 언어를 바꾸고 실제로 적용된 코드를 반환한다.

    모르는 코드는 조용히 영어로 폴백한다 — 손으로 고친 config.json 때문에
    위젯이 안 뜨는 일이 없어야 한다 (불변조건 5).
    """
    global _active
    key = str(code).lower() if code is not None else DEFAULT_LANGUAGE
    key = key if key in LANGUAGES else DEFAULT_LANGUAGE
    _active = LANGUAGES[key]
    return key


def t(key: str, **fmt) -> str:
    """활성 언어의 문자열을 포맷해 반환한다. 실패해도 던지지 않는다."""
    template = _active.get(key, EN.get(key, key))
    if not fmt:
        return template
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError, TypeError):
        return template


def weekdays() -> list:
    """월요일이 0인 요일 이름 목록 (datetime.weekday() 인덱스와 같은 순서)."""
    value = _active.get("weekdays")
    return value if isinstance(value, list) and len(value) == 7 else EN["weekdays"]
