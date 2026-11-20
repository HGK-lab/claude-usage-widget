"""Claude 사용량 바탕화면 위젯.

5시간 세션/주간 한도의 '사용한 %'를 시간 칸 위의 얇은 사용량 막대로 표시한다.
칸은 흐른 시간, 막대는 쓴 양이라 막대가 칠한 칸보다 앞서면 빨리 쓰는 중이다.
색·글꼴·치수는 theme.py(DESIGN.md의 코드판)에 있다.
API 호출은 백그라운드 스레드, UI 갱신은 tkinter 메인 스레드(queue 경유).
"""

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta
from pathlib import Path

import history
import i18n
import theme
import usage_api

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOCK_PATH = APP_DIR / ".instance.lock"
HISTORY_PATH = APP_DIR / "usage_history.json"   # 요일별 사용량용 주간 % 기록
STARTUP_BAT = (
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
    / "Start Menu" / "Programs" / "Startup" / "ClaudeUsageWidget.bat"
)

DEFAULT_CONFIG = {
    "x": None, "y": None, "topmost": True, "poll_interval": 180,
    "automation_reserve_pct": 5, "minimized": False,
    # 표시 언어("en"/"ko"), 라벨에 이 문자열이 들어간 행은 숨김(대소문자 무시),
    # 401 시 credentials 파일에 재발급 토큰을 쓸지 여부.
    "language": "en", "hidden_labels": [], "auto_refresh_token": True,
    # 색 테마("light"/"dark"). 모르는 값은 밝은 테마.
    "theme": theme.DEFAULT_THEME,
}

# 예외 타입 → i18n 키. 문자열이 아니라 키를 담는다 — 모듈 로드 시점에 문자열로
# 굳혀 두면 config의 language 설정이 오류 메시지에는 안 먹는다.
ERROR_KEYS = {
    usage_api.TokenMissingError: "error.token_missing",
    usage_api.TokenExpiredError: "error.token_expired",
    usage_api.RateLimitedError: "error.rate_limited",
    usage_api.NetworkError: "error.network",
    usage_api.ParseError: "error.parse",
}

# ERROR_KEYS에 없는 예외용 기본 키. 예외 문자열에 토큰이 섞여 있을 수 있으므로
# str(e)를 화면에 그대로 쓰지 않는다.
UNEXPECTED_ERROR_KEY = "error.unexpected"

# 한도 종류별 창 길이. 막대 바탕의 '흐른 시간' 음영에 쓴다. 여기 없는 종류(모델별
# 주간 등)는 음영 없이 사용량만 그린다 — 모델별 한도는 개수·이름이 동적이다.
WINDOW_LEN = {"session": timedelta(hours=5), "weekly_all": timedelta(days=7)}
# 구식 응답(_parse_legacy)의 id → limits 배열의 kind
LEGACY_KIND = {"five_hour": "session", "seven_day": "weekly_all"}
# hidden_labels에서 이름 대신 한도 종류로 숨기는 토큰의 머리말("kind:session").
# 세션·주간 전체처럼 모델명이 없는 한도는 이름이 언어마다 달라 이것으로 저장한다.
KIND_TOKEN = "kind:"
# 모델별 한도를 모델명으로 숨기는 토큰("model:opus"). 이름 일부가 아니라 id의
# 모델명과 정확히 같아야 맞는다 — "opus 5"가 "Opus 5.5"까지 숨기지 않게.
MODEL_TOKEN = "model:"
# 로컬 오류(시작 프로그램 등록 실패) 문구를 상태줄에 두는 시간. 계속 두면 "N분 전 갱신"이
# 영영 안 보인다. 실패 상태 자체는 메뉴 체크박스가 계속 보여 준다.
LOCAL_MSG_SECS = 600


def limit_kind(item_id: str) -> str:
    """usage_api가 만든 id("session:", "weekly_all:", "five_hour" …)에서 종류만."""
    kind = str(item_id).split(":", 1)[0]
    return LEGACY_KIND.get(kind, kind)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4),
                ("rcWork", ctypes.c_long * 4), ("dwFlags", ctypes.c_ulong)]


def monitor_work_area(x: int, y: int, fallback):
    """(x, y)에서 가장 가까운 모니터의 작업 영역 (왼, 위, 오른, 아래).

    winfo_screenwidth/height는 주 모니터만 알려 준다. 그것으로 당기면 보조
    모니터(음수 좌표 포함)에 둔 위젯이 주 모니터로 끌려온다. 실패하면 fallback.
    """
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_ulong]
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        pt = _Point(int(x), int(y))
        hmon = user32.MonitorFromPoint(pt, 2)        # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not hmon or not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return fallback
        return tuple(info.rcWork)
    except (AttributeError, OSError, TypeError, ValueError, OverflowError):
        return fallback


def left_button_down() -> bool:
    """논리적 왼쪽 버튼(Tk의 Button-1)이 지금 눌려 있는지. 모르면 True.

    GetAsyncKeyState는 물리 버튼을 보므로 좌우 바꾸기(SM_SWAPBUTTON)를 따른다.
    """
    try:
        user32 = ctypes.windll.user32
        vk = 0x02 if user32.GetSystemMetrics(23) else 0x01
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)
    except (AttributeError, OSError):
        return True


def flag_key(pct: float, reserve_hit: bool):
    """행 이름 옆 라벨. 임계 70/90은 /usage 색 경고와 같은 기준."""
    if reserve_hit:
        return "flag.reserve"
    if pct >= 90:
        return "flag.critical"
    if pct >= 70:
        return "flag.high"
    return None


def format_reset(dt: "datetime | None"):
    """(왼쪽: 남은 시간, 오른쪽: 리셋 시각). 남은 시간은 분 단위까지만."""
    if dt is None:
        return "", ""
    local = dt.astimezone()
    now = datetime.now(local.tzinfo)
    secs = max(0, int((local - now).total_seconds()))
    mins_total = (secs + 59) // 60      # 올림 — 59초 남았을 때 "0분"이 안 뜨게
    days, rem = divmod(mins_total, 24 * 60)
    hours, minutes = divmod(rem, 60)
    if days:
        left = i18n.t("reset.in.dh", days=days, hours=hours)
    elif hours:
        left = i18n.t("reset.in.hm", hours=hours, minutes=minutes)
    else:
        left = i18n.t("reset.in.m", minutes=minutes)

    ampm = i18n.t("ampm.am") if local.hour < 12 else i18n.t("ampm.pm")
    hour12 = local.hour % 12 or 12
    if secs < 24 * 3600:
        right = i18n.t("time.hm", ampm=ampm, hour=hour12, minute=local.minute)
    else:
        right = i18n.t(
            "reset.at.later", month=local.month, day=local.day,
            weekday=i18n.weekdays()[local.weekday()],
            time=i18n.t("time.h", ampm=ampm, hour=hour12))
    return left, right


def time_progress(kind: str, resets_at):
    """창 안에서 흐른 시간 비율(0~1). 칸을 그릴 수 없는 한도면 None."""
    span = WINDOW_LEN.get(kind)
    if span is None or resets_at is None:
        return None
    local = resets_at.astimezone()
    now = datetime.now(local.tzinfo)
    elapsed = (now - (local - span)) / span
    return min(1.0, max(0.0, elapsed))


def slot_labels(kind: str, resets_at):
    """주간 칸의 요일 글자. 창 시작(리셋 7일 전) 요일부터 7개."""
    if kind != "weekly_all" or resets_at is None:
        return None
    start = resets_at.astimezone() - WINDOW_LEN["weekly_all"]
    names = i18n.weekdays()
    return [names[(start + timedelta(days=i)).weekday()] for i in range(7)]


def _coerce_int(cfg, key, lo, hi=None):
    """cfg[key]를 int로 강제하고 범위에 클램프. 실패 시 DEFAULT_CONFIG 값."""
    try:
        value = int(cfg[key])
    except (KeyError, TypeError, ValueError, OverflowError):   # 1e400 → inf
        value = DEFAULT_CONFIG[key]
    value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    cfg[key] = value


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    # 최상위가 객체가 아니면([1, 2], "hi", null, 5) update가 던진다 — 기본값으로.
    if isinstance(raw, dict):
        cfg.update(raw)
    # 손으로 고친 config.json이 위젯을 깨거나(불변조건 5)
    # 폴링 하한 180초(불변조건 4)를 어기지 않도록 검증한다.
    _coerce_int(cfg, "poll_interval", 180)
    _coerce_int(cfg, "automation_reserve_pct", 0, 100)
    # 창 위치: 정수가 아니면("1561", 1561.5) geometry가 TclError로 죽는다.
    # 숫자로 읽히면 정수로, 아니면 None(기본 위치).
    for key in ("x", "y"):
        v = cfg.get(key)
        try:
            cfg[key] = None if v is None or isinstance(v, bool) else int(float(v))
        except (TypeError, ValueError, OverflowError):
            cfg[key] = None
    cfg["auto_refresh_token"] = bool(cfg.get("auto_refresh_token", True))
    raw_hidden = cfg.get("hidden_labels")
    cfg["hidden_labels"] = [
        s.strip().lower() for s in raw_hidden
        if isinstance(s, str) and s.strip()
    ] if isinstance(raw_hidden, list) else []
    if cfg.get("theme") not in theme.THEMES:
        cfg["theme"] = theme.DEFAULT_THEME
    # 설정을 읽는 유일한 지점이라 여기서 언어를 적용한다 — UI가 만들어지기 전에
    # 반드시 한 번 불려야 하고, 이 함수가 그 순서를 보장한다. 모르는 코드는
    # i18n이 영어로 폴백하며 실제 적용된 코드를 돌려준다.
    cfg["language"] = i18n.set_language(cfg.get("language"))
    return cfg


# 락을 걸지 못했지만 이미 실행 중인 것도 아닌 경우(락 로직 자체의 실패).
# 중복 방지를 포기하고 위젯은 띄운다 — 불변조건 5.
_LOCK_SKIPPED = object()


def acquire_single_instance_lock():
    """중복 실행 방지용 배타 락. 이미 실행 중이면 None.

    msvcrt의 배타 락은 프로세스가 어떻게 끝나든(작업 관리자 강제 종료 포함)
    OS가 핸들을 닫으며 함께 풀린다 — 죽은 락은 다음 실행이 그대로 회수한다.
    반환된 핸들은 프로세스가 사는 동안 열어둬야 하므로 호출자가 붙들어야 한다.
    """
    try:
        import msvcrt
        fh = open(LOCK_PATH, "a+b")
    except Exception:
        return _LOCK_SKIPPED
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:      # 다른 인스턴스가 쥐고 있음 — 오류가 아닌 정상 상황
        fh.close()
        return None
    except Exception:    # 예상 밖 실패 — 락 없이 진행
        fh.close()
        return _LOCK_SKIPPED
    return fh


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """부드러운 둥근 사각형 폴리곤. r은 짧은 변의 절반을 넘지 않게 줄인다."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _pick_family(candidates):
    fams = set(tkfont.families())
    for name in candidates:
        if name in fams:
            return name
    return "TkDefaultFont"


class Widget:
    def __init__(self, lock=None):
        self._lock = lock   # GC로 닫히지 않게 프로세스 생존 동안 붙든다
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._refresh_now = threading.Event()
        self.last_ok = None
        self.limits = None              # 마지막으로 성공한 응답(오류 때도 흐리게 남긴다)
        self.automations = 0
        self.minimized = bool(self.cfg["minimized"])
        self.err_key = None             # 상태줄에 띄울 오류 i18n 키 (폴링 오류만 — 값이 낡았다는 뜻)
        self.local_msg = None           # 폴링과 무관한 로컬 오류 i18n 키(시작 프로그램 등록 실패 등)
        self._refreshing = False
        self._redraw_pending = False    # 드래그 중 도착한 결과 — 놓을 때 다시 그린다
        self._panel_shift = (0, 0)      # 설정 패널이 화면 안에 들도록 당긴 양(닫을 때 되돌림)
        self._panel_clamped = False     # 이번에 연 패널을 이미 당겼는지(처음 그릴 때만 당긴다)
        self._local_msg_at = 0.0        # local_msg를 띄운 시각(monotonic)
        self._last_motion = 0.0         # 마지막 누름·끌기 시각(monotonic) — _dragging
        self._sx = self._sy = None      # 드래그 시작 좌표 (None = 드래그 중 아님)
        self._drag_ox = self._drag_oy = 0
        self._pressed = None            # 눌린 버튼 이름 (떼는 순간 같은 버튼이면 실행)
        self._draft = None              # 설정 패널이 열려 있으면 편집 중인 값(dict)
        self.history = history.load(HISTORY_PATH)   # [(epoch, 주간 누적%)]
        self._spin = 0.0                # 새로고침 아이콘 회전 각도
        self._spin_until = 0.0          # 이 시각까지는 갱신이 끝나도 계속 돈다(최소 한 바퀴)
        self._spinning = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self.root.configure(bg=theme.KEY)
        self.root.wm_attributes("-transparentcolor", theme.KEY)
        self.root.title(i18n.t("app.title"))

        self._make_fonts()
        self.canvas = tk.Canvas(self.root, bg=theme.KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self._build_menu()
        self._bind_events()

        self._redraw()
        self._place_window()
        self.root.deiconify()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(200, self._drain_queue)
        self.root.after(30_000, self._tick)

    # ── 글꼴 ────────────────────────────────────────────
    def _make_fonts(self):
        fam = {
            "title": _pick_family(theme.FONT_TITLE),
            "num": _pick_family(theme.FONT_NUM),
            "body": _pick_family(theme.FONT_BODY),
        }
        use = {"title": "title", "pct": "num", "pill_pct": "num"}
        self.f = {
            key: tkfont.Font(root=self.root, family=fam[use.get(key, "body")],
                             size=size, weight=weight)
            for key, (size, weight) in theme.SIZE.items()
        }

    def _lh(self, key):
        return self.f[key].metrics("linespace")

    # ── 메뉴·이벤트 ─────────────────────────────────────
    def _build_menu(self):
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg["topmost"]))
        self.autostart_var = tk.BooleanVar(value=STARTUP_BAT.exists())
        self.dark_var = tk.BooleanVar(value=self.cfg["theme"] == "dark")
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=i18n.t("menu.settings"), command=self.open_settings)
        menu.add_command(label=i18n.t("menu.refresh"), command=self.manual_refresh)
        menu.add_command(label=i18n.t("menu.minimize"), command=self.toggle_minimize)
        menu.add_checkbutton(label=i18n.t("menu.topmost"), variable=self.topmost_var,
                             command=self._toggle_topmost)
        menu.add_checkbutton(label=i18n.t("menu.autostart"), variable=self.autostart_var,
                             command=self._toggle_autostart)
        menu.add_checkbutton(label=i18n.t("menu.dark"), variable=self.dark_var,
                             command=self._toggle_theme)
        menu.add_separator()
        menu.add_command(label=i18n.t("menu.quit"), command=self.quit)
        self.menu = menu

    def _bind_events(self):
        c = self.canvas
        c.bind("<Button-1>", self._press)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", self._popup_menu)
        c.bind("<Motion>", self._hover)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        self._hovered = None

    def _popup_menu(self, e):
        # tk_popup이 포인터를 잡아 왼쪽 버튼의 ButtonRelease를 삼킬 수 있다.
        # 누른 상태를 남겨 두면 드래그 중으로 오인해 다시 그리기를 영영 미룬다.
        self._drop_press()
        self.menu.tk_popup(e.x_root, e.y_root)

    def _btn_at(self):
        for tag in self.canvas.gettags("current"):
            if tag.startswith("btn:"):
                return tag[4:]
        return None

    def _hover(self, _e):
        self._set_hover(self._btn_at())

    def _set_hover(self, name):
        if name == self._hovered:
            return
        P = self._palette()
        if self._hovered:
            self.canvas.itemconfigure(f"icon:{self._hovered}", fill=P["muted"])
        if name:
            self.canvas.itemconfigure(f"icon:{name}", fill=P["ink"])
        self.canvas.configure(cursor="hand2" if name else "")
        self._hovered = name

    # ── 팔레트 ──────────────────────────────────────────
    def _reserve(self):
        return min(self.automations * self.cfg["automation_reserve_pct"], 80)

    @staticmethod
    def _hides(token, label, item_id):
        """숨김 문자열 하나가 이 한도를 가리는지. "kind:session"은 id의 종류로,
        "model:opus"는 id의 모델명과 정확히 같은지로, 그 밖의 문자열(손으로 적은
        "fable" 등)은 이름(소문자)에 들어 있는지로 본다."""
        if token.startswith(KIND_TOKEN):
            return token[len(KIND_TOKEN):] == limit_kind(item_id)
        if token.startswith(MODEL_TOKEN):
            scope = str(item_id).partition(":")[2].strip().lower()
            return bool(scope) and token[len(MODEL_TOKEN):] == scope
        return token in label.lower()

    def _visible(self):
        hidden = self.cfg["hidden_labels"]
        return [it for it in (self.limits or [])
                if not any(self._hides(h, it["label"], it["id"]) for h in hidden)]

    def _reserve_warn(self):
        """세션이 자동화 예약선을 넘었으면 (개수, 예약%)."""
        reserve = self._reserve()
        if reserve <= 0:
            return None
        for it in self._visible():
            if limit_kind(it["id"]) == "session" and it["used_pct"] >= 100 - reserve:
                return self.automations, reserve
        return None

    def _stale(self):
        return self.err_key is not None and self.limits is not None

    def _critical(self):
        if self._stale():
            return False
        return (self._reserve_warn() is not None
                or any(it["used_pct"] >= 90 for it in self._visible()))

    def _palette(self):
        return theme.palette(self.cfg["theme"], self._critical())

    # ── 그리기 ──────────────────────────────────────────
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        self._hovered = None
        P = self._palette()
        if self._draft is not None:
            w, h = self._draw_settings(P)
        elif self.minimized:
            w, h = self._draw_pill(P)
        else:
            w, h = self._draw_card(P)
        c.configure(width=w, height=h)
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if self._draft is not None:
            if not self._panel_clamped:
                # 설정 패널은 카드보다 넓고 길다. 화면 밖으로 나가면 저장 단추를 못
                # 누르므로 창이 있는 모니터의 작업 영역 안으로 당긴다. 연 직후 한 번만
                # 당긴다 — 그 뒤 사용자가 옮긴 패널을 _tick이 다시 끌어오지 않게.
                # 카드·알약은 당기지 않는다(보조 모니터, 화면 끝에 둔 자리 존중).
                nx, ny = self._clamp_to_monitor(x, y, w, h)
                self._panel_shift = (nx - x, ny - y)
                self._panel_clamped = True
                x, y = nx, ny
        else:
            self._panel_clamped = False
            if self._panel_shift != (0, 0):
                # 패널을 닫았다: 열 때 당긴 만큼 되돌린다(패널을 끈 이동은 유지).
                sx, sy = self._panel_shift
                x, y = x - sx, y - sy
                self._panel_shift = (0, 0)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._redraw_pending = False
        # 다시 그리면 버튼이 사라지거나 옮겨 간다. 손 모양 커서가 남지 않게
        # 지금 포인터 아래 항목으로 다시 맞춘다(_hovered는 위에서 None으로 비웠다).
        c.configure(cursor="")
        self._set_hover(self._btn_at())

    def _clamp_to_monitor(self, x, y, w, h):
        fallback = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        left, top, right, bottom = monitor_work_area(x + w // 2, y + 12, fallback)
        return (max(left, min(x, right - w)), max(top, min(y, bottom - h)))

    def _draw_card(self, P):
        c, f = self.canvas, self.f
        W = theme.CARD_W
        x0, x1 = theme.PAD_X, W - theme.PAD_X
        y = theme.PAD_TOP

        # 제목 + 아이콘 버튼(설정 메뉴, 접기, 새로고침, 닫기)
        head_h = 24
        cy = y + head_h / 2
        c.create_text(x0, cy, text=i18n.t("app.title"), anchor="w",
                      font=f["title"], fill=P["ink"])
        bx = x1 - 10
        for name in ("close", "refresh", "min", "menu"):
            self._icon(name, bx, cy, P, angle=self._spin if name == "refresh" else 0.0)
            if name == "refresh":
                self._refresh_xy = (bx, cy)
            bx -= 26
        y += head_h + 4

        rows_p = P
        if self._stale():
            rows_p = dict(P, **{k: P[v] for k, v in theme.STALE_KEYS.items()})

        visible = self._visible()
        if self.limits is None:
            c.create_text(x0, y + 4, text=i18n.t("loading"), anchor="nw",
                          font=f["name"], fill=P["muted"])
            y += self._lh("name") + 8
        else:
            for i, item in enumerate(visible):
                if i:
                    y += theme.ROW_GAP
                y = self._draw_limit(item, x0, x1, y, rows_p)

        if self._stale():
            mins = int((time.time() - self.last_ok) // 60) if self.last_ok else 0
            y = self._label_box(x0, x1, y + 8, i18n.t("stale.age", mins=mins),
                                i18n.t("stale.stamp"), P, grey=True)

        # 상태줄
        y += 10
        c.create_line(x0, y, x1, y, fill=P["rule"])
        y += 7
        text, strong = self._status_text()
        item = c.create_text(x0, y, text=text, anchor="nw", width=x1 - x0,
                             font=f["small"], fill=P["ink2"] if strong else P["muted"])
        y = c.bbox(item)[3] + theme.PAD_BOTTOM

        bg = _round_rect(c, 1, 1, W - 1, y - 1, theme.RADIUS,
                         fill=P["card"], outline=P["outline"], width=1)
        c.tag_lower(bg)
        return W, int(y)

    def _icon(self, name, cx, cy, P, name_as=None, angle=0.0):
        """아이콘 버튼. 투명한 히트 영역 + 선 아이콘, 둘 다 btn:<name> 태그.

        name은 그릴 모양, name_as는 눌렀을 때의 동작 이름(기본은 name과 같다).
        angle(도)만큼 시계 방향으로 돌려 그린다 — 새로고침 회전 애니메이션용.
        """
        c = self.canvas
        act = name_as or name
        tags = ("btn", f"btn:{act}")
        itags = tags + (f"icon:{act}",)
        c.create_rectangle(cx - 12, cy - 12, cx + 12, cy + 12, fill=P["card"],
                           outline="", tags=tags)
        self._icon_lines(name, cx, cy, P["muted"], itags, angle)

    def _icon_lines(self, name, cx, cy, col, itags, angle=0.0):
        c = self.canvas
        kw = dict(fill=col, width=1.6, capstyle="round", tags=itags)
        rot = math.radians(angle)

        def at(r, deg):
            """중심에서 반지름 r, 각도 deg(시계 방향, 0 = 오른쪽)인 점."""
            a = math.radians(deg) + rot
            return cx + r * math.cos(a), cy + r * math.sin(a)

        if name == "close":
            c.create_line(cx - 4, cy - 4, cx + 4, cy + 4, **kw)
            c.create_line(cx + 4, cy - 4, cx - 4, cy + 4, **kw)
        elif name == "min":
            c.create_line(cx - 5, cy, cx + 5, cy, **kw)
        elif name == "refresh":
            # 원호는 create_arc 대신 smooth 선으로 근사한다. arc는 색이 fill이 아니라
            # outline이라 icon:<name> 태그 하나로 호버 색을 한 번에 바꿀 수 없다.
            pts = []
            for deg in range(30, 321, 25):
                pts += at(5, deg)
            c.create_line(*pts, smooth=True, **kw)
            ex, ey = at(5, 320)
            # 화살촉: 호 끝에서 접선 방향으로 짧은 두 선
            c.create_line(*at(5, 320), *at(8.5, 300), **kw)
            c.create_line(ex, ey, *at(2.2, 290), **kw)
        elif name == "menu":
            # 톱니바퀴: 테 + 이 8개. 테도 선으로 그려야 호버 때 fill로 한 번에 바뀐다.
            ring = []
            for deg in range(0, 361, 30):
                ring += at(3.6, deg)
            c.create_line(*ring, smooth=True, **kw)
            for deg in range(0, 360, 45):
                c.create_line(*at(5.2, deg), *at(7, deg), **dict(kw, width=2.2))

    def _draw_limit(self, item, x0, x1, y, P):
        c, f = self.canvas, self.f
        kind = limit_kind(item["id"])
        pct = item["used_pct"]
        reserve = self._reserve() if kind == "session" else 0
        reserve_hit = reserve > 0 and pct >= 100 - reserve
        fkey = flag_key(pct, reserve_hit)
        alert = fkey in ("flag.critical", "flag.reserve")

        top_h = max(self._lh("name"), self._lh("pct"))
        cy = y + top_h / 2
        name = c.create_text(x0, cy, text=item["label"], anchor="w",
                             font=f["name"], fill=P["ink"])
        c.create_text(x1, cy, text=f"{pct:.0f}%", anchor="e", font=f["pct"],
                      fill=P["alert"] if alert else P["ink"])
        if fkey:
            fx = c.bbox(name)[2] + 8
            text = i18n.t(fkey)
            tw = f["flag"].measure(text)
            _round_rect(c, fx, cy - 8, fx + tw + 14, cy + 8, 8,
                        fill=P["flag_bg"], outline="")
            c.create_text(fx + 7, cy, text=text, anchor="w", font=f["flag"],
                          fill=P["flag_fg"])
        y += top_h + 4

        bar = P["high"] if pct >= 70 else P["accent"]
        if kind == "weekly_all" and item["resets_at"] is not None:
            y = self._daily_graph(x0, x1, y + 2, item, P)
        else:
            y = self._bar(x0, x1, y + 2, pct, bar,
                          time_progress(kind, item["resets_at"]), reserve, P)

        left, right = format_reset(item["resets_at"])
        if left or right:
            y += 4
            c.create_text(x0, y, text=left, anchor="nw", font=f["small"], fill=P["muted"])
            c.create_text(x1, y, text=right, anchor="ne", font=f["small"], fill=P["muted"])
            y += self._lh("small")
        return y

    def _bar(self, x0, x1, y, pct, color, progress, reserve, P):
        """한 줄 막대. 바탕(slot) 위에 흐른 시간만큼 옅은 음영, 그 위에 사용량.

        사용량 끝이 음영보다 앞서면 시간보다 빨리 쓰는 중이다.
        """
        c = self.canvas
        h = theme.BAR_H
        r = h / 2
        _round_rect(c, x0, y, x1, y + h, r, fill=P["slot"], outline="")
        if progress:
            pw = (x1 - x0) * progress
            if pw >= 1:
                _round_rect(c, x0, y, x0 + pw, y + h, r, fill=P["slot_past"], outline="")
        w = (x1 - x0) * max(0.0, min(pct, 100)) / 100
        if w >= 1:
            _round_rect(c, x0, y, x0 + w, y + h, r, fill=color, outline="")
        if reserve > 0:
            rx = x0 + (x1 - x0) * (100 - reserve) / 100
            c.create_line(rx, y - 3, rx, y + h + 3, fill=P["reserve"], width=2)
        return y + h

    def _daily_graph(self, x0, x1, y, item, P):
        """주간: 요일별 사용량 막대그래프 + 하루 예산 점선. 반환: 다음 y."""
        c, f = self.canvas, self.f
        pct = item["used_pct"]
        resets = item["resets_at"].astimezone()
        start = (resets - WINDOW_LEN["weekly_all"]).timestamp()
        now = time.time()
        usage = history.daily_usage(self.history, start, now)
        today = min(6, max(0, int((now - start) // history.DAY)))
        labels = slot_labels("weekly_all", item["resets_at"])

        days_left = max(1.0, (resets.timestamp() - now) / history.DAY)
        budget = max(0.0, (100 - pct) / days_left)
        known = [u for u in usage if u is not None]
        scale = max(known + [budget, 100 / 7]) * 1.15

        H = theme.DAY_H
        gap = theme.DAY_GAP
        cw = (x1 - x0 - gap * 6) / 7
        base = y + H
        for i in range(7):
            cx0 = x0 + i * (cw + gap)
            _round_rect(c, cx0, y, cx0 + cw, base, theme.DAY_R,
                        fill=P["slot"], outline="")
            u = usage[i]
            if u is not None and u > 0:
                bh = max(2.0, H * min(1.0, u / scale))
                col = P["high"] if u > budget * 1.05 else P["accent"]
                _round_rect(c, cx0, base - bh, cx0 + cw, base, theme.DAY_R,
                            fill=col, outline="")
            if i == today:
                _round_rect(c, cx0 - 2, y - 2, cx0 + cw + 2, base + 2, theme.DAY_R + 2,
                            fill="", outline=P["ink2"], width=1.5)
        # 하루 예산 기준선 — 막대가 이 선을 넘은 날은 예산보다 많이 쓴 날이다
        by = base - H * min(1.0, budget / scale)
        c.create_line(x0, by, x1, by, fill=P["ink2"], dash=(3, 3))

        y = base + 4
        for i in range(7):
            c.create_text(x0 + i * (cw + gap) + cw / 2, y, text=labels[i], anchor="n",
                          font=f["tiny"], fill=P["ink"] if i == today else P["muted"])
        y += self._lh("tiny")

        # 범례: 점선 + "하루 예산 N%"
        y += 4
        ly = y + self._lh("small") / 2
        c.create_line(x0, ly, x0 + 14, ly, fill=P["ink2"], dash=(3, 3))
        c.create_text(x0 + 20, ly, text=i18n.t("budget.legend", pct=round(budget)),
                      anchor="w", font=f["small"], fill=P["ink2"])
        return y + self._lh("small")

    def _label_box(self, x0, x1, y, left, stamp, P, grey=False):
        """점선 상자: 왼쪽 글자 + 오른쪽 이중 테두리 도장."""
        c, f = self.canvas, self.f
        h = 30
        _round_rect(c, x0, y, x1, y + h, 8, fill=P["surface"], outline=P["rule"],
                    dash=(3, 2))
        cy = y + h / 2
        c.create_text(x0 + 10, cy, text=left, anchor="w", font=f["small"],
                      fill=P["ink2"])
        col = P["muted"] if grey else P["high"]
        tw = f["small"].measure(stamp)
        sx1 = x1 - 8
        sx0 = sx1 - tw - 14
        c.create_rectangle(sx0, cy - 10, sx1, cy + 10, outline=col, width=1.5)
        c.create_rectangle(sx0 + 3, cy - 7, sx1 - 3, cy + 7, outline=col, width=1)
        c.create_text((sx0 + sx1) / 2, cy, text=stamp, font=f["small"], fill=col)
        return y + h

    def _draw_pill(self, P):
        """접힌 알약: 세션 막대 + 세션 % + 주간 %."""
        c, f = self.canvas, self.f
        H = theme.PILL_H
        visible = self._visible()
        session = next((it for it in visible if limit_kind(it["id"]) == "session"), None)
        weekly = next((it for it in visible if limit_kind(it["id"]) == "weekly_all"), None)
        main = session or max(visible, key=lambda it: it["used_pct"], default=None)

        bx0, bx1 = 14, 58
        if main:
            pct = main["used_pct"]
            progress = time_progress(limit_kind(main["id"]), main["resets_at"])
            self._bar(bx0, bx1, H / 2 - theme.BAR_H / 2, pct,
                      P["high"] if pct >= 70 else P["accent"], progress, 0, P)
        else:
            self._bar(bx0, bx1, H / 2 - theme.BAR_H / 2, 0, P["accent"], None, 0, P)

        tx = bx1 + 10
        pct_text = f"{main['used_pct']:.0f}%" if main else i18n.t("pill.init")
        if weekly and weekly is not main:
            t1 = c.create_text(tx, H / 2 - 7, text=pct_text, anchor="w",
                               font=f["pill_pct"], fill=P["ink"])
            t2 = c.create_text(tx, H / 2 + 9, text=i18n.t("pill.sub", pct=weekly["used_pct"]),
                               anchor="w", font=f["tiny"], fill=P["muted"])
            right = max(c.bbox(t1)[2], c.bbox(t2)[2])
        else:
            t1 = c.create_text(tx, H / 2, text=pct_text, anchor="w",
                               font=f["pill_pct"], fill=P["ink"])
            right = c.bbox(t1)[2]
        W = int(right + 16)
        bg = _round_rect(c, 1, 1, W - 1, H - 1, H / 2, fill=P["card"],
                         outline=P["outline"], width=1)
        c.tag_lower(bg)
        return W, H

    # ── 설정 패널 ───────────────────────────────────────
    # 카드와 같은 캔버스에 그린다. 값은 _draft에만 바꾸고 저장할 때 cfg로 옮긴다.
    # 입력칸(Entry) 대신 −/+ 단추를 쓴다 — 손으로 친 값의 검증·포커스 처리가 필요 없다.
    RESERVE_RANGE = (0, 100)         # 자동화 1개당 예약 % (load_config와 같은 범위)
    INTERVAL_RANGE = (3, 60)         # 갱신 간격(분). 하한 3분 = 불변조건 4.
    # 상한은 −/+로 올릴 수 있는 끝일 뿐이다. config.json에 이미 더 큰 값이 있으면
    # 그 값까지는 그대로 둔다(저장만 눌렀는데 간격이 바뀌면 안 된다).

    def open_settings(self):
        if self._draft is not None:
            return
        minutes = max(self.INTERVAL_RANGE[0], self.cfg["poll_interval"] // 60)
        self._draft = {
            "language": self.cfg["language"],
            "theme": self.cfg["theme"],
            "hidden_labels": list(self.cfg["hidden_labels"]),
            "automation_reserve_pct": self.cfg["automation_reserve_pct"],
            "poll_minutes": minutes,
            # 열 때 값. 저장할 때 이와 같으면 cfg를 건드리지 않는다 — 분 단위로
            # 내림한 값(200초 → 3분)이 원래 초 값을 덮어쓰지 않게.
            "_orig_reserve": self.cfg["automation_reserve_pct"],
            "_orig_minutes": minutes,
        }
        self._redraw()

    def _limit_labels(self):
        """칩으로 보여 줄 (한도 이름, 숨길 때 저장할 문자열, id). 숨긴 것도 포함.

        이름("Opus 주간", "세션 (5시간)")은 언어마다 달라서 그대로 저장하면 언어를
        바꿀 때 숨김이 풀린다. 그래서 모델별 한도는 모델명 토큰("model:opus")을,
        모델명이 없는 한도는 종류 토큰("kind:session")을 저장한다.
        """
        seen = []
        for it in self.limits or []:
            scope = str(it["id"]).partition(":")[2].strip()
            token = (MODEL_TOKEN + scope.lower() if scope
                     else KIND_TOKEN + limit_kind(it["id"]))
            if all(it["label"] != s[0] for s in seen):
                seen.append((it["label"], token, it["id"]))
        return seen

    def _settings_action(self, name):
        d = self._draft
        if d is None:
            return
        action, _, arg = name.partition(":")
        if action == "set_cancel":
            self._draft = None
        elif action == "set_save":
            self._apply_settings()
            return
        elif action == "set_lang":
            d["language"] = arg
        elif action == "set_theme":
            d["theme"] = arg
        elif action == "set_chip":
            chips = d.get("_chips") or []
            if not arg.isdigit() or int(arg) >= len(chips):
                return
            label, token, item_id = chips[int(arg)]
            hits = [h for h in d["hidden_labels"] if self._hides(h, label, item_id)]
            if hits:        # 숨김 → 보임: 이 이름에 걸리는 숨김 문자열을 모두 뺀다
                d["hidden_labels"] = [h for h in d["hidden_labels"] if h not in hits]
            else:
                d["hidden_labels"].append(token)
        elif action == "set_res":
            lo, hi = self.RESERVE_RANGE
            d["automation_reserve_pct"] = min(hi, max(lo, d["automation_reserve_pct"] + int(arg)))
        elif action == "set_int":
            lo, hi = self.INTERVAL_RANGE
            hi = max(hi, d["_orig_minutes"])
            d["poll_minutes"] = min(hi, max(lo, d["poll_minutes"] + int(arg)))
        self._redraw()

    def _apply_settings(self):
        d, self._draft = self._draft, None
        lang_changed = d["language"] != self.cfg["language"]
        self.cfg["theme"] = d["theme"]
        self.dark_var.set(d["theme"] == "dark")
        self.cfg["hidden_labels"] = d["hidden_labels"]
        if d["automation_reserve_pct"] != d["_orig_reserve"]:
            self.cfg["automation_reserve_pct"] = d["automation_reserve_pct"]
        if d["poll_minutes"] != d["_orig_minutes"]:
            # 폴링 스레드는 다음 대기부터 새 간격을 쓴다. 하한 180초(불변조건 4).
            self.cfg["poll_interval"] = max(180, d["poll_minutes"] * 60)
        if lang_changed:
            self.cfg["language"] = i18n.set_language(d["language"])
            self.root.title(i18n.t("app.title"))
            self.menu.destroy()
            self._build_menu()          # 메뉴 글자는 만들 때 굳으므로 다시 만든다
        self._save_config()
        if lang_changed:
            # 한도 이름은 usage_api가 응답을 받을 때 번역한다 — 다시 받아야 바뀐다.
            self.manual_refresh()
        else:
            self._redraw()

    def _draw_settings(self, P):
        c, f = self.canvas, self.f
        d = self._draft
        W = theme.SETTINGS_W
        x0, x1 = theme.PAD_X, W - theme.PAD_X
        y = theme.PAD_TOP

        cy = y + 12
        c.create_text(x0, cy, text=i18n.t("settings.title"), anchor="w",
                      font=f["title"], fill=P["ink"])
        self._icon("close", x1 - 10, cy, P, name_as="set_cancel")
        y += 30
        c.create_line(0, y, W, y, fill=P["rule"])

        def group(title, y):
            y += 10
            c.create_text(x0, y, text=title, anchor="nw", font=f["small"], fill=P["muted"])
            return y + self._lh("small") + 4

        def label(text, y, hint=None):
            if hint is None:
                c.create_text(x0, y + 16, text=text, anchor="w", font=f["name"], fill=P["ink"])
                return y + 32
            c.create_text(x0, y + 4, text=text, anchor="nw", font=f["name"], fill=P["ink"])
            c.create_text(x0, y + 6 + self._lh("name"), text=hint, anchor="nw",
                          font=f["small"], fill=P["muted"])
            return y + 12 + self._lh("name") + self._lh("small")

        # 표시
        y = group(i18n.t("settings.display"), y)
        ny = label(i18n.t("settings.language"), y)
        self._segmented(x1, y + 16, [("en", i18n.t("settings.language.en")),
                                     ("ko", i18n.t("settings.language.ko"))],
                        d["language"], "set_lang", P)
        y = ny
        ny = label(i18n.t("settings.theme"), y)
        self._segmented(x1, y + 16, [("light", i18n.t("settings.theme.light")),
                                     ("dark", i18n.t("settings.theme.dark"))],
                        d["theme"], "set_theme", P)
        y = ny + 6
        c.create_line(0, y, W, y, fill=P["rule"])

        # 보일 한도
        y = group(i18n.t("settings.limits"), y)
        labels = self._limit_labels()
        d["_chips"] = labels            # 칩 번호는 이 목록 기준(_settings_action)
        if not labels:
            c.create_text(x0, y, text=i18n.t("settings.limits_empty"), anchor="nw",
                          font=f["small"], fill=P["muted"])
            y += self._lh("small") + 10
        else:
            cx, cy0 = x0, y
            for i, (text, _token, item_id) in enumerate(labels):
                shown = not any(self._hides(h, text, item_id) for h in d["hidden_labels"])
                tw = f["small"].measure(text) + 20
                if cx + tw > x1 and cx > x0:
                    cx, cy0 = x0, cy0 + 30
                tags = ("btn", f"btn:set_chip:{i}")
                _round_rect(c, cx, cy0, cx + tw, cy0 + 24, 12, tags=tags,
                            fill=P["flag_bg"] if shown else P["card"],
                            outline="" if shown else P["rule"])
                c.create_text(cx + tw / 2, cy0 + 12, text=text, font=f["small"], tags=tags,
                              fill=P["flag_fg"] if shown else P["muted"])
                cx += tw + 6
            y = cy0 + 24 + 10
        c.create_line(0, y, W, y, fill=P["rule"])

        # 동작
        y = group(i18n.t("settings.behavior"), y)
        ny = label(i18n.t("settings.reserve"), y,
                   hint=i18n.t("settings.reserve_hint", count=self.automations))
        self._stepper(x1, (y + ny) / 2, f"{d['automation_reserve_pct']}%", "set_res", P)
        y = ny
        ny = label(i18n.t("settings.interval"), y, hint=i18n.t("settings.interval_hint"))
        self._stepper(x1, (y + ny) / 2, i18n.t("settings.minutes", n=d["poll_minutes"]),
                      "set_int", P)
        y = ny + 6

        # 취소 / 저장
        c.create_line(0, y, W, y, fill=P["rule"])
        y += 10
        bx = x1
        for key, primary in (("save", True), ("cancel", False)):
            text = i18n.t(f"settings.{key}")
            tw = f["name"].measure(text) + 24
            tags = ("btn", f"btn:set_{key}")
            _round_rect(c, bx - tw, y, bx, y + 30, 8, tags=tags,
                        fill=P["ink"] if primary else P["card"],
                        outline="" if primary else P["rule"])
            c.create_text(bx - tw / 2, y + 15, text=text, font=f["name"], tags=tags,
                          fill=P["card"] if primary else P["ink"])
            bx -= tw + 8
        y += 30 + theme.PAD_BOTTOM

        bg = _round_rect(c, 1, 1, W - 1, y - 1, theme.RADIUS,
                         fill=P["card"], outline=P["outline"], width=1)
        c.tag_lower(bg)
        return W, int(y)

    def _segmented(self, right, cy, options, selected, action, P):
        """오른쪽 정렬 분절 단추. 고른 칸만 카드색으로 떠 보인다."""
        c, f = self.canvas, self.f
        widths = [f["small"].measure(text) + 18 for _, text in options]
        total = sum(widths) + 4 + 2 * (len(options) - 1)
        x = right - total
        _round_rect(c, x, cy - 13, right, cy + 13, 9, fill=P["slot"], outline="")
        x += 2
        for (key, text), w in zip(options, widths):
            on = key == selected
            tags = ("btn", f"btn:{action}:{key}")
            _round_rect(c, x, cy - 11, x + w, cy + 11, 7, tags=tags,
                        fill=P["card"] if on else P["slot"], outline="")
            c.create_text(x + w / 2, cy, text=text, font=f["small"], tags=tags,
                          fill=P["ink"] if on else P["muted"])
            x += w + 2

    def _stepper(self, right, cy, value, action, P):
        """[− 값 +] 단추. 범위는 _settings_action이 클램프한다."""
        c, f = self.canvas, self.f
        w = 96
        x = right - w
        _round_rect(c, x, cy - 13, right, cy + 13, 8, fill=P["card"], outline=P["muted"])
        for sign, bx in (("-1", x + 13), ("1", right - 13)):
            name = f"{action}:{sign}"
            tags = ("btn", f"btn:{name}")
            c.create_rectangle(bx - 12, cy - 12, bx + 12, cy + 12, fill=P["card"],
                               outline="", tags=tags)
            kw = dict(fill=P["muted"], width=1.6, capstyle="round",
                      tags=tags + (f"icon:{name}",))
            c.create_line(bx - 4, cy, bx + 4, cy, **kw)
            if sign == "1":
                c.create_line(bx, cy - 4, bx, cy + 4, **kw)
        c.create_text(x + w / 2, cy, text=value, font=f["small"], fill=P["ink"])

    def _place_window(self):
        self.root.update_idletasks()
        x, y = self.cfg["x"], self.cfg["y"]
        if x is None or y is None:
            x = self.root.winfo_screenwidth() - 320
            y = 60
        # 저장된 자리에서 가장 가까운 모니터 기준으로, 적어도 100×60은 보이게 한다.
        # 그 모니터가 빠졌으면 MONITOR_DEFAULTTONEAREST가 남은 모니터를 고른다.
        # 모니터는 _clamp_to_monitor와 같은 기준점(창 위쪽 가운데)으로 고른다.
        fallback = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        w = self.root.winfo_reqwidth()
        left, top, right, bottom = monitor_work_area(x + w // 2, y + 12, fallback)
        x = min(max(left, x), right - 100)
        y = min(max(top, y), bottom - 60)
        self.root.geometry(f"+{x}+{y}")

    # ── 상태줄 ──────────────────────────────────────────
    def _fresh_text(self):
        if self.last_ok:
            mins = int((time.time() - self.last_ok) // 60)
            fresh = (i18n.t("status.fresh.now") if mins < 1
                     else i18n.t("status.fresh.mins", mins=mins))
        else:
            fresh = i18n.t("loading")
        interval = max(1, self.cfg["poll_interval"] // 60)
        return i18n.t("status.line", interval=interval, fresh=fresh)

    def _status_text(self):
        """(문구, 강조 여부). 갱신 중 > 폴링 오류 > 예약선 침범 > 로컬 오류 > 평소.

        갱신 중이 오류보다 앞선다 — 오류 뒤에 새로고침을 눌렀을 때 눌린 줄 알 수 있게.
        로컬 오류는 폴링 결과와 무관하게 LOCAL_MSG_SECS 동안 남으므로 예약선 경고를
        가리지 않게 뒤에 둔다.
        """
        if self._refreshing:
            return i18n.t("refreshing"), False
        if self.err_key:
            return i18n.t(self.err_key), True
        warn = self._reserve_warn()
        if warn:
            n, pct = warn
            return i18n.t("status.reserve_warn", count=n, pct=pct), True
        if self.local_msg and time.monotonic() - self._local_msg_at < LOCAL_MSG_SECS:
            return i18n.t(self.local_msg), True
        return self._fresh_text(), False

    # ── 폴링 ────────────────────────────────────────────
    def _poll_loop(self):
        backoff = 0
        while not self._stop.is_set():
            # fetch 직전에 clear — wait() 반환과 clear 사이에 들어온 수동
            # 새로고침이 유실되지 않게 한다(이 fetch가 그 요청을 처리한다).
            self._refresh_now.clear()
            try:
                data = usage_api.fetch_usage(
                    allow_refresh=self.cfg["auto_refresh_token"])
                autos = usage_api.count_automations()
                self.q.put(("ok", (data, autos)))
                backoff = 0
                mult = 1
            except usage_api.RateLimitedError as e:
                backoff = min(backoff + 1, 3)
                mult = 2 ** backoff
                self.q.put(("err", e))
            except usage_api.UsageError as e:
                mult = 1
                self.q.put(("err", e))
            except Exception as e:  # 최종 안전망 — 폴링 스레드가 죽으면 위젯이
                mult = 1            # 낡은 값에서 영구히 멈춘다
                self.q.put(("err", e))
            # 대기 시간을 5초마다 cfg에서 다시 계산한다. 설정 패널에서 간격을 바꾸면
            # 이번 대기에도 바로 반영된다(60분 → 3분이면 3분이 지난 시점에 받는다).
            # 바로 받지는 않는다 — 저장할 때마다 요청하면 429가 날 수 있다.
            start = time.monotonic()
            while not self._stop.is_set():
                left = self.cfg["poll_interval"] * mult - (time.monotonic() - start)
                if left <= 0 or self._refresh_now.wait(timeout=min(left, 5)):
                    break

    def _drain_queue(self):
        try:
            changed = False
            while True:
                kind, payload = self.q.get_nowait()
                changed = True
                self._refreshing = False
                if kind == "ok":
                    data, autos = payload
                    self.last_ok = time.time()
                    self.automations = autos
                    self.limits = data
                    self.err_key = None
                    self._record_history(data)
                else:
                    self.err_key = ERROR_KEYS.get(type(payload), UNEXPECTED_ERROR_KEY)
        except queue.Empty:
            pass
        if changed:
            if not self._dragging():
                self._redraw()
            else:
                # 드래그 중에 창 크기·위치를 다시 박으면 끌기가 튄다(_tick과 같은 이유).
                # 놓을 때(_release) 그린다.
                self._redraw_pending = True
        self.root.after(200, self._drain_queue)

    def _tick(self):
        # 분 단위 카운트다운·시간 칸·"N분 전 갱신"을 따라가게 다시 그린다.
        # 드래그 중에는 건너뛴다(창 크기를 다시 박으면 끌기가 튄다).
        if not self._dragging():
            self._redraw()
        self.root.after(30_000, self._tick)

    def manual_refresh(self):
        self._refreshing = True
        self._redraw()
        self._refresh_now.set()
        self._start_spin()

    # ── 새로고침 회전 ───────────────────────────────────
    def _start_spin(self):
        self._spin_until = time.time() + 0.75      # 갱신이 빨라도 한 바퀴는 돈다
        if not self._spinning:
            self._spinning = True
            self._spin_step()

    def _spin_step(self):
        busy = self._refreshing or time.time() < self._spin_until
        self._spin = (self._spin + 24.0) % 360 if busy else 0.0
        # 카드 전체가 아니라 아이콘 선만 다시 그린다(30ms마다 전체를 그리면 깜빡인다).
        if not self.minimized and self._draft is None and hasattr(self, "_refresh_xy"):
            c = self.canvas
            col = c.itemcget("icon:refresh", "fill") if c.find_withtag("icon:refresh") \
                else self._palette()["muted"]
            c.delete("icon:refresh")
            self._icon_lines("refresh", *self._refresh_xy, col,
                             ("btn", "btn:refresh", "icon:refresh"), self._spin)
        if busy:
            self.root.after(30, self._spin_step)
        else:
            self._spinning = False

    # ── 주간 기록 ───────────────────────────────────────
    def _record_history(self, data):
        weekly = next((it for it in data if limit_kind(it["id"]) == "weekly_all"), None)
        if weekly is None:
            return
        if history.record(self.history, time.time(), weekly["used_pct"]):
            history.save(HISTORY_PATH, self.history)

    # ── 최소화(알약) 토글 ────────────────────────────────
    def toggle_minimize(self):
        self._draft = None              # 설정 중에 접으면 편집은 버린다
        self.minimized = not self.minimized
        self.cfg["minimized"] = self.minimized
        self._redraw()
        self._save_config()

    # ── 드래그/버튼 ─────────────────────────────────────
    def _drop_press(self):
        self._sx = self._sy = None
        self._pressed = None
        if self._redraw_pending:
            # 놓친 ButtonRelease는 _release에 오지 않는다 — 미뤄 둔 다시 그리기를
            # 여기서 건다(안 그러면 다음 _tick까지 최대 30초 늦는다).
            self.root.after_idle(lambda: self._stop.is_set() or self._redraw())

    def _dragging(self):
        """눌린 채 끄는 중인지. ButtonRelease를 놓쳤으면(메뉴·Alt+Tab·Win+D)
        _sx가 남아 있어도 버튼은 떼어져 있다 — 그때는 누른 상태를 버린다.
        안 그러면 다시 그리기가 영영 미뤄져 새 값이 화면에 안 나온다.

        버튼 상태 API는 원격 데스크톱 등에서 눌린 버튼을 떼어진 것으로 알려 줄 수
        있다. 그래서 마지막 누름·끌기 뒤 1초가 지났을 때만 버린다."""
        if self._sx is None:
            return False
        if not left_button_down() and time.monotonic() - self._last_motion > 1.0:
            self._drop_press()
            return False
        return True

    def _press(self, e):
        self._pressed = self._btn_at()
        self._sx, self._sy = e.x_root, e.y_root
        self._last_motion = time.monotonic()
        self._drag_ox = e.x_root - self.root.winfo_x()
        self._drag_oy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self._last_motion = time.monotonic()
        if self._sx is None or self._pressed:
            return              # 버튼 위에서 시작한 끌기는 창을 옮기지 않는다
        self.root.geometry(f"+{e.x_root - self._drag_ox}+{e.y_root - self._drag_oy}")

    def _release(self, e):
        if self._sx is None:   # _press 없이 온 ButtonRelease — 무시
            return
        moved = abs(e.x_root - self._sx) > 4 or abs(e.y_root - self._sy) > 4
        pressed, self._pressed = self._pressed, None
        self._sx = self._sy = None
        if pressed:
            if not moved and pressed == self._btn_at():
                self._invoke(pressed, e)
        elif moved:
            if self._redraw_pending:
                self._redraw()       # 위치를 저장하기 전에 — 다시 그리기가 창을 옮길 수 있다
            self._save_config()
        elif self._draft is not None:
            pass                     # 설정 패널의 빈 곳 클릭 — 알약 상태에서 열었어도 접지 않는다
        elif self.minimized:
            self.toggle_minimize()   # 알약 클릭(이동 아님) → 복원
        # 드래그 중 미뤄 둔 다시 그리기. 위 동작이 이미 그렸으면 _redraw가 플래그를
        # 내렸다. 닫기(quit)로 창이 없어졌으면 그리지 않는다.
        if self._redraw_pending and not self._stop.is_set():
            self._redraw()

    def _invoke(self, name, e):
        if name == "close":
            self.quit()
        elif name == "refresh":
            self.manual_refresh()
        elif name == "min":
            self.toggle_minimize()
        elif name == "menu":
            self.open_settings()
        elif name.startswith("set_"):
            self._settings_action(name)

    def _save_config(self):
        # 설정 패널을 화면 안으로 당긴 만큼은 빼고 저장한다. 그 이동은 패널을 닫으면
        # 되돌리는 임시 이동이라, 다음 실행의 카드 위치가 되면 안 된다.
        sx, sy = self._panel_shift
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x() - sx, self.root.winfo_y() - sy
        self.cfg["topmost"] = self.topmost_var.get()
        self.cfg["minimized"] = self.minimized
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())
        self._save_config()

    def _toggle_autostart(self):
        try:
            if self.autostart_var.get():
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                if not pythonw.exists():
                    pythonw = Path(sys.executable)
                # 파일 인코딩과 chcp 줄은 반드시 짝이 맞아야 한다. cmd는
                # 현재 코드페이지로 각 줄을 해석하므로, chcp 65001을 두고
                # cp949로 저장하면 한글 경로가 깨져 실행이 실패한다.
                # 이 조합(UTF-8 BOM 없이 + chcp 65001)은 코드페이지 949·65001
                # 양쪽 콘솔에서 실측 확인했다. 한쪽만 바꾸지 말 것.
                bat = (f'@echo off\nchcp 65001 >nul\n'
                       f'start "" "{pythonw}" "{APP_DIR / "main.py"}"\n')
                STARTUP_BAT.write_text(bat, encoding="utf-8")
            else:
                STARTUP_BAT.unlink(missing_ok=True)
        except (OSError, UnicodeError):  # 방어선 유지
            self.autostart_var.set(STARTUP_BAT.exists())
            # err_key가 아니라 local_msg — err_key는 '값이 낡았다'는 뜻이라
            # 카드를 흐리게 하고 위험 표시를 끈다(_stale).
            self.local_msg = "status.autostart_failed"
            self._local_msg_at = time.monotonic()
            self._redraw()
            return
        self.local_msg = None
        self._redraw()

    def _toggle_theme(self):
        self.cfg["theme"] = "dark" if self.dark_var.get() else "light"
        if self._draft is not None:
            # 설정 패널이 열려 있으면 편집 값도 따라간다. 안 그러면 저장할 때
            # 열 때 복사해 둔 옛 테마로 되돌아간다.
            self._draft["theme"] = self.cfg["theme"]
        self._redraw()
        self._save_config()

    def quit(self):
        self._save_config()
        self._stop.set()
        self._refresh_now.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


WINDOWS_ONLY_NOTICE = (
    "Claude Usage Widget runs on Windows only.\n"
    "It depends on the Tk -transparentcolor attribute, the msvcrt file lock, and\n"
    "the Windows Startup folder, none of which exist on this platform.\n"
)


if __name__ == "__main__":
    if sys.platform != "win32":
        # 가드가 없으면 -transparentcolor에서 TclError로 죽는다 — 안내 없이
        # 크래시하느니 이유를 말하고 나간다.
        sys.stderr.write(WINDOWS_ONLY_NOTICE)
        sys.exit(1)
    _lock = acquire_single_instance_lock()
    if _lock is None:
        sys.exit(0)   # 이미 실행 중 — 조용히 종료
    Widget(lock=_lock).run()
