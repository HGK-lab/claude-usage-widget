"""색·글꼴·치수 토큰 (DESIGN.md의 코드판).

main.py(UI)만 import 하는 leaf 모듈이다. 어떤 모듈도 import 하지 않는다.
tkinter는 투명도·그림자·color-mix가 없으므로 모든 색을 hex로 미리 계산해 둔다.
값을 바꾸면 DESIGN.md의 표도 같이 고칠 것 — 두 곳이 어긋나면 DESIGN.md가 기준이다.
"""

# 창 바깥(둥근 모서리 밖)을 뚫는 투명 키. 화면에 거의 없는 색이어야 하고,
# 아래 팔레트의 어떤 색과도 같으면 안 된다(그 색이 구멍으로 보인다).
KEY = "#010203"

# ── 팔레트 ────────────────────────────────────────────
# 키 의미
#   card        카드 바탕                 surface   라벨 상자·알약 칸 바탕
#   slot        빈 시간 칸                slot_past 지난 시간 칸
#   ink         본문·숫자                 ink2      요일 글자·현재 칸 테두리
#   muted       보조 글자·아이콘          rule      구분선·카드 외곽선
#   accent      사용량 막대(70% 미만)     high      사용량 막대(70% 이상)
#   flag_bg/fg  "70% 넘음" 같은 라벨      alert     위험 행의 % 숫자
#   reserve     자동화 예약선             outline   카드 외곽선

LIGHT = {
    "card": "#FAF9F5", "surface": "#FFFEFB",
    "slot": "#E6E3D9", "slot_past": "#B3B1A9",
    "ink": "#141413", "ink2": "#3D3D3A", "muted": "#69675F",
    "rule": "#D9D5CA", "outline": "#D9D5CA",
    "accent": "#D97757", "high": "#A9502F",
    "flag_bg": "#F5E0D5", "flag_fg": "#923F25",
    "alert": "#141413", "reserve": "#141413",
}

DARK = {
    "card": "#262624", "surface": "#1F1E1D",
    "slot": "#3A3936", "slot_past": "#6B6963",
    "ink": "#FAF9F5", "ink2": "#C2C0B6", "muted": "#A6A39A",
    "rule": "#3E3D39", "outline": "#45443F",
    "accent": "#D97757", "high": "#EB9A7C",
    "flag_bg": "#4A2E24", "flag_fg": "#F0A285",
    "alert": "#FAF9F5", "reserve": "#FAF9F5",
}

# 90% 이상이거나 자동화 예약선을 넘으면 카드 전체를 뒤집는다.
# 밝은 테마는 Slate 카드로, 어두운 테마는 Ivory 카드로. 구조는 그대로다.
LIGHT_CRITICAL = {
    "card": "#141413", "surface": "#1F1E1D",
    "slot": "#3D3D3A", "slot_past": "#5C5A54",
    "ink": "#FAF9F5", "ink2": "#FAF9F5", "muted": "#B5B4B1",
    "rule": "#3D3D3A", "outline": "#141413",
    "accent": "#D97757", "high": "#D97757",
    "flag_bg": "#D97757", "flag_fg": "#141413",
    "alert": "#D97757", "reserve": "#FAF9F5",
}

DARK_CRITICAL = dict(LIGHT, **{
    "outline": "#D97757",
    "high": "#A9502F", "alert": "#A9502F",
    "flag_bg": "#A9502F", "flag_fg": "#FFFFFF",
})

THEMES = {"light": (LIGHT, LIGHT_CRITICAL), "dark": (DARK, DARK_CRITICAL)}
DEFAULT_THEME = "light"


def palette(theme: str, critical: bool = False) -> dict:
    """테마 이름으로 팔레트를 고른다. 모르는 이름은 밝은 테마(불변조건 5)."""
    normal, crit = THEMES.get(theme, THEMES[DEFAULT_THEME])
    return crit if critical else normal


# 오류로 값이 낡았을 때 막대·숫자를 흐리게 그리는 대체 색.
# 투명도가 없으니 '흐림'은 막대를 slot_past로, 숫자를 muted로 바꾸는 것으로 대신한다.
STALE_KEYS = {"accent": "slot_past", "high": "slot_past", "ink": "muted", "alert": "muted"}


# ── 글꼴 ──────────────────────────────────────────────
# 앞에서부터 설치된 첫 글꼴을 쓴다. 한글 글리프가 없는 글꼴은 Windows가 글자 단위로
# 대체 글꼴을 찾아 준다.
FONT_TITLE = ("Noto Serif KR", "Source Serif 4", "Georgia")
FONT_BODY = ("Pretendard", "Segoe UI Variable Text", "Segoe UI")
FONT_NUM = ("Cascadia Mono", "Consolas")

# (크기, 굵기) — 크기는 pt
SIZE = {
    "title": (10, "bold"),
    "name": (9, "normal"),
    "pct": (12, "bold"),
    "small": (8, "normal"),
    "tiny": (8, "normal"),
    "flag": (7, "bold"),
    "pill_pct": (11, "bold"),
}


# ── 치수 (px) ─────────────────────────────────────────
CARD_W = 292          # 카드 폭
PAD_X = 16            # 카드 좌우 안쪽 여백
PAD_TOP = 12
PAD_BOTTOM = 12
RADIUS = 14           # 카드 모서리
SLOT_GAP = 2          # 시간 칸 사이
SLOT_H = 20           # 세션 칸 높이
SLOT_H_WEEK = 26      # 주간 칸 높이(요일 글자 자리 포함)
SLOT_R = 5            # 칸 모서리
USE_H = 4             # 사용량 막대 두께
USE_BOTTOM = 3        # 칸 아래쪽에서 막대까지
ROW_GAP = 12          # 한도 행 사이
PILL_H = 40
