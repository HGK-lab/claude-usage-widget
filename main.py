"""Claude 사용량 바탕화면 위젯.

5시간 세션/주간 한도의 '사용한 %'를 시간 칸 위의 얇은 사용량 막대로 표시한다.
칸은 흐른 시간, 막대는 쓴 양이라 막대가 칠한 칸보다 앞서면 빨리 쓰는 중이다.
색·글꼴·치수는 theme.py(DESIGN.md의 코드판)에 있다.
API 호출은 백그라운드 스레드, UI 갱신은 tkinter 메인 스레드(queue 경유).
"""

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

import i18n
import theme
import usage_api

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOCK_PATH = APP_DIR / ".instance.lock"
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

# 한도 종류별 창 길이와 시간 칸 수. 여기 없는 종류(모델별 주간 등)는 칸 없이
# 얇은 막대만 그린다 — 모델별 한도는 개수·이름이 동적이라 칸을 늘리지 않는다.
WINDOW_LEN = {"session": timedelta(hours=5), "weekly_all": timedelta(days=7)}
SLOT_COUNT = {"session": 5, "weekly_all": 7}
# 구식 응답(_parse_legacy)의 id → limits 배열의 kind
LEGACY_KIND = {"five_hour": "session", "seven_day": "weekly_all"}


def limit_kind(item_id: str) -> str:
    """usage_api가 만든 id("session:", "weekly_all:", "five_hour" …)에서 종류만."""
    kind = str(item_id).split(":", 1)[0]
    return LEGACY_KIND.get(kind, kind)


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


def daily_budget(pct: float, resets_at):
    """(남은 일수, 하루 %). 하루 미만 남았거나 다 썼으면 None."""
    if resets_at is None or pct >= 100:
        return None
    local = resets_at.astimezone()
    days = (local - datetime.now(local.tzinfo)).total_seconds() / 86400
    if days < 1:
        return None
    return int(days), int((100 - pct) / days)


def _coerce_int(cfg, key, lo, hi=None):
    """cfg[key]를 int로 강제하고 범위에 클램프. 실패 시 DEFAULT_CONFIG 값."""
    try:
        value = int(cfg[key])
    except (KeyError, TypeError, ValueError):
        value = DEFAULT_CONFIG[key]
    value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    cfg[key] = value


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    # 손으로 고친 config.json이 위젯을 깨거나(불변조건 5)
    # 폴링 하한 180초(불변조건 4)를 어기지 않도록 검증한다.
    _coerce_int(cfg, "poll_interval", 180)
    _coerce_int(cfg, "automation_reserve_pct", 0, 100)
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
        self.err_key = None             # 상태줄에 띄울 오류 i18n 키
        self._refreshing = False
        self._sx = self._sy = None      # 드래그 시작 좌표 (None = 드래그 중 아님)
        self._drag_ox = self._drag_oy = 0
        self._pressed = None            # 눌린 버튼 이름 (떼는 순간 같은 버튼이면 실행)

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
        c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        c.bind("<Motion>", self._hover)
        c.bind("<Leave>", lambda e: self._set_hover(None))
        self._hovered = None

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

    def _visible(self):
        hidden = self.cfg["hidden_labels"]
        return [it for it in (self.limits or [])
                if not any(h in it["label"].lower() for h in hidden)]

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
        if self.minimized:
            w, h = self._draw_pill(P)
        else:
            w, h = self._draw_card(P)
        c.configure(width=w, height=h)
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

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
            self._icon(name, bx, cy, P)
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

    def _icon(self, name, cx, cy, P):
        """아이콘 버튼. 투명한 히트 영역 + 선 아이콘, 둘 다 btn:<name> 태그."""
        c = self.canvas
        tags = ("btn", f"btn:{name}")
        itags = tags + (f"icon:{name}",)
        c.create_rectangle(cx - 12, cy - 12, cx + 12, cy + 12, fill=P["card"],
                           outline="", tags=tags)
        col = P["muted"]
        kw = dict(fill=col, width=1.6, capstyle="round", tags=itags)
        if name == "close":
            c.create_line(cx - 4, cy - 4, cx + 4, cy + 4, **kw)
            c.create_line(cx + 4, cy - 4, cx - 4, cy + 4, **kw)
        elif name == "min":
            c.create_line(cx - 5, cy, cx + 5, cy, **kw)
        elif name == "refresh":
            # 원호는 create_arc 대신 smooth 선으로 근사한다. arc는 색이 fill이 아니라
            # outline이라 icon:<name> 태그 하나로 호버 색을 한 번에 바꿀 수 없다.
            pts = []
            for deg in range(40, 330, 25):
                rad = math.radians(deg)
                pts += [cx + 5 * math.cos(rad), cy - 5 * math.sin(rad)]
            c.create_line(*pts, smooth=True, **kw)
            ex, ey = cx + 5 * math.cos(math.radians(40)), cy - 5 * math.sin(math.radians(40))
            c.create_line(ex, ey - 4, ex, ey, ex - 4, ey, **kw)
        elif name == "menu":
            # 설정 패널은 아직 없다 — 같은 자리에서 우클릭 메뉴를 연다.
            for dy in (-4, 0, 4):
                c.create_line(cx - 5, cy + dy, cx + 5, cy + dy, **kw)

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
        progress = time_progress(kind, item["resets_at"])
        if progress is not None:
            y = self._timeline(x0, x1, y, SLOT_COUNT[kind], progress,
                               slot_labels(kind, item["resets_at"]),
                               pct, bar, reserve, P)
        else:
            _round_rect(c, x0, y, x1, y + theme.USE_H, 2, fill=P["slot"], outline="")
            self._use_bar(x0, x1, y, pct, bar, P)
            y += theme.USE_H

        left, right = format_reset(item["resets_at"])
        if left or right:
            y += 4
            c.create_text(x0, y, text=left, anchor="nw", font=f["small"], fill=P["muted"])
            c.create_text(x1, y, text=right, anchor="ne", font=f["small"], fill=P["muted"])
            y += self._lh("small")

        if kind == "weekly_all":
            budget = daily_budget(pct, item["resets_at"])
            if budget:
                days, per = budget
                y = self._label_box(x0, x1, y + 8, i18n.t("budget.left", days=days),
                                    i18n.t("budget.per_day", pct=per), P)
        return y

    def _timeline(self, x0, x1, y, n, progress, labels, pct, bar, reserve, P):
        """시간 칸 n개 + 겹친 사용량 막대 + 예약선. 반환: 다음 y."""
        c = self.canvas
        H = theme.SLOT_H_WEEK if labels else theme.SLOT_H
        y += 3                              # 현재 칸 테두리 자리
        gap = theme.SLOT_GAP
        sw = (x1 - x0 - gap * (n - 1)) / n
        idx = min(int(progress * n), n - 1)
        part = progress * n - idx
        for i in range(n):
            sx = x0 + i * (sw + gap)
            _round_rect(c, sx, y, sx + sw, y + H, theme.SLOT_R,
                        fill=P["slot_past"] if i < idx else P["slot"], outline="")
            if i == idx:
                if part > 0.02:
                    _round_rect(c, sx, y, sx + sw * part, y + H, theme.SLOT_R,
                                fill=P["slot_past"], outline="")
                _round_rect(c, sx - 2, y - 2, sx + sw + 2, y + H + 2, theme.SLOT_R + 2,
                            fill="", outline=P["ink2"], width=1.5)
            if labels:
                # 지난 칸은 바탕이 진해서 ink로 — 어두운 테마에서 ink2는 대비가 모자란다
                c.create_text(sx + sw / 2, y + (H - 10) / 2 + 1, text=labels[i],
                              font=self.f["tiny"],
                              fill=P["ink"] if i < idx else P["ink2"])
        by = y + H - theme.USE_BOTTOM - theme.USE_H
        self._use_bar(x0, x1, by, pct, bar, P, ring=True)
        if reserve > 0:
            rx = x0 + (x1 - x0) * (100 - reserve) / 100
            c.create_line(rx, y - 3, rx, y + H + 3, fill=P["reserve"], width=2)
        return y + H + 3

    def _use_bar(self, x0, x1, y, pct, color, P, ring=False):
        c = self.canvas
        w = (x1 - x0) * max(0.0, min(pct, 100)) / 100
        if w < 1:
            return
        h = theme.USE_H
        if ring:   # 칸 위에서도 막대가 떠 보이게 카드색 테두리를 두른다
            _round_rect(c, x0 - 1.5, y - 1.5, x0 + w + 1.5, y + h + 1.5, 3.5,
                        fill=P["card"], outline="")
        _round_rect(c, x0, y, x0 + w, y + h, 2, fill=color, outline="")

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
        """접힌 알약: 세션 칸 5개 축소판 + 세션 % + 주간 %."""
        c, f = self.canvas, self.f
        H = theme.PILL_H
        visible = self._visible()
        session = next((it for it in visible if limit_kind(it["id"]) == "session"), None)
        weekly = next((it for it in visible if limit_kind(it["id"]) == "weekly_all"), None)
        main = session or max(visible, key=lambda it: it["used_pct"], default=None)

        # 미니 칸 상자
        bx0, by0 = 6, 6
        n, sw, gap = 5, 7, 2
        bx1 = bx0 + 5 + n * sw + (n - 1) * gap + 5
        _round_rect(c, bx0, by0, bx1, H - 6, 12, fill=P["surface"], outline="")
        progress = time_progress("session", session["resets_at"]) if session else None
        idx = min(int(progress * n), n - 1) if progress is not None else -1
        sy0, sy1 = by0 + 5, H - 11
        for i in range(n):
            sx = bx0 + 5 + i * (sw + gap)
            c.create_rectangle(sx, sy0, sx + sw, sy1, outline="",
                               fill=P["slot_past"] if i < idx else P["slot"])
        if main:
            pct = main["used_pct"]
            bar = P["high"] if pct >= 70 else P["accent"]
            self._use_bar(bx0 + 5, bx1 - 5, sy1 - 6, pct, bar, P, ring=True)

        tx = bx1 + 8
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
        W = int(right + 14)
        bg = _round_rect(c, 1, 1, W - 1, H - 1, H / 2, fill=P["card"],
                         outline=P["outline"], width=1)
        c.tag_lower(bg)
        return W, H

    def _place_window(self):
        self.root.update_idletasks()
        x, y = self.cfg["x"], self.cfg["y"]
        if x is None or y is None:
            x = self.root.winfo_screenwidth() - 320
            y = 60
        x = min(max(0, x), self.root.winfo_screenwidth() - 100)
        y = min(max(0, y), self.root.winfo_screenheight() - 60)
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
        """(문구, 강조 여부). 오류 > 갱신 중 > 예약선 침범 > 평소."""
        if self.err_key:
            return i18n.t(self.err_key), True
        if self._refreshing:
            return i18n.t("refreshing"), False
        warn = self._reserve_warn()
        if warn:
            n, pct = warn
            return i18n.t("status.reserve_warn", count=n, pct=pct), True
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
                delay = self.cfg["poll_interval"]
            except usage_api.RateLimitedError as e:
                backoff = min(backoff + 1, 3)
                delay = self.cfg["poll_interval"] * (2 ** backoff)
                self.q.put(("err", e))
            except usage_api.UsageError as e:
                delay = self.cfg["poll_interval"]
                self.q.put(("err", e))
            except Exception as e:  # 최종 안전망 — 폴링 스레드가 죽으면 위젯이
                delay = self.cfg["poll_interval"]  # 낡은 값에서 영구히 멈춘다
                self.q.put(("err", e))
            self._refresh_now.wait(timeout=delay)

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
                else:
                    self.err_key = ERROR_KEYS.get(type(payload), UNEXPECTED_ERROR_KEY)
        except queue.Empty:
            pass
        if changed:
            self._redraw()
        self.root.after(200, self._drain_queue)

    def _tick(self):
        # 분 단위 카운트다운·시간 칸·"N분 전 갱신"을 따라가게 다시 그린다.
        # 드래그 중에는 건너뛴다(창 크기를 다시 박으면 끌기가 튄다).
        if self._sx is None:
            self._redraw()
        self.root.after(30_000, self._tick)

    def manual_refresh(self):
        self._refreshing = True
        self._redraw()
        self._refresh_now.set()

    # ── 최소화(알약) 토글 ────────────────────────────────
    def toggle_minimize(self):
        self.minimized = not self.minimized
        self.cfg["minimized"] = self.minimized
        self._redraw()
        self._save_config()

    # ── 드래그/버튼 ─────────────────────────────────────
    def _press(self, e):
        self._pressed = self._btn_at()
        self._sx, self._sy = e.x_root, e.y_root
        self._drag_ox = e.x_root - self.root.winfo_x()
        self._drag_oy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
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
            self._save_config()
        elif self.minimized:
            self.toggle_minimize()   # 알약 클릭(이동 아님) → 복원

    def _invoke(self, name, e):
        if name == "close":
            self.quit()
        elif name == "refresh":
            self.manual_refresh()
        elif name == "min":
            self.toggle_minimize()
        elif name == "menu":
            self.menu.tk_popup(e.x_root, e.y_root)

    def _save_config(self):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
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
            self.err_key = "status.autostart_failed"
            self._redraw()

    def _toggle_theme(self):
        self.cfg["theme"] = "dark" if self.dark_var.get() else "light"
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
