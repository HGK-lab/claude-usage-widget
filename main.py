"""Claude 사용량 바탕화면 위젯.

테라코타 라인아트 카드에 5시간 세션/주간 한도의 '사용한 %'를 표시한다.
API 호출은 백그라운드 스레드, UI 갱신은 tkinter 메인 스레드(queue 경유).
"""

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path

import i18n
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
}

# ── 팔레트 (테라코타 라인아트) ──────────────────────────
# 배경은 단색 블록 하나(테라코타), 크림은 포인트 면적에만, 선은 단색 다크브라운.
CARD = "#c8785a"     # 테라코타 브라운 — 카드 배경(유일한 베이스 색)
CREAM = "#f5ede4"    # 크림 — 포인트 면(바 트랙, 글로브)에만
KEY = "#010203"      # 투명 키(화면에 거의 없는 색) — 어떤 위젯도 사용 금지
INK = "#2b1a12"      # 라인/스트로크/본문 — 다크브라운 단색
FG = INK
DIM = "#7b4432"      # 테라코타 위에서 한 단계 물러난 보조 텍스트
BAR_OK = INK         # 정상 구간은 잉크 단색 (선 위주 톤 유지)
LIMIT = "#8e2c1f"    # 제한선/경고 — 팔레트 안에 머무는 딥 레드브라운
ERR = "#5c3a1e"      # 오류색(경고와 구분) — 어두운 브라운

STROKE = 2           # 라인아트 균일 스트로크 두께

RADIUS = 14
PAD_OUT = 6          # 카드 가장자리 여백(둥근 코너 노출용, >= 0.3*RADIUS)
BAR_W = 188
BAR_H = 10

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


def usage_color(used_pct: float) -> str:
    """/usage와 같은 '사용한 %' 기준: 많이 썼을수록 경고색 (임계 70/90 유지)."""
    if used_pct < 70:
        return BAR_OK
    if used_pct < 90:
        return "#b3611a"   # 앰버 브라운 — 테라코타 위에서 구분되는 경고 톤
    return LIMIT


def format_reset(dt: "datetime | None") -> str:
    if dt is None:
        return ""
    local = dt.astimezone()
    now = datetime.now(local.tzinfo)
    delta = local - now
    ampm = i18n.t("ampm.am") if local.hour < 12 else i18n.t("ampm.pm")
    hour12 = local.hour % 12 or 12
    if delta.total_seconds() < 24 * 3600:
        mins = max(0, int(delta.total_seconds() // 60))
        return i18n.t(
            "reset.soon",
            time=i18n.t("time.hm", ampm=ampm, hour=hour12, minute=local.minute),
            hours=mins // 60, minutes=mins % 60)
    return i18n.t(
        "reset.later",
        month=local.month, day=local.day,
        weekday=i18n.weekdays()[local.weekday()],
        time=i18n.t("time.h", ampm=ampm, hour=hour12))


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
    """부드러운 둥근 사각형 폴리곤."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def _draw_globe(canvas, x, y, r):
    """라인아트 글로브: 크림 원 + 잉크 외곽선, 경선/위선, 양옆 브래킷.

    스크린샷 모티프({ 글로브 })를 그대로 옮긴 것. 아이콘 안에서 크림 단색 면은
    이 원 하나뿐이다 — 아이콘에 크림 면을 더 늘리지 말 것(크림은 포인트 면적
    전용). 좌표를 정수로 딱 떨어지지 않게 둬서 손그림의 미세한 불규칙성을 남긴다.
    """
    canvas.create_oval(x - r, y - r, x + r, y + r,
                       fill=CREAM, outline=INK, width=STROKE)
    # 경선 2개(좌우로 부푼 타원) + 위선 2개(수평선) — 완벽한 대칭은 피한다.
    canvas.create_oval(x - r * 0.46, y - r, x + r * 0.46, y + r,
                       outline=INK, width=1)
    canvas.create_line(x - r * 0.97, y - r * 0.36, x + r * 0.97, y - r * 0.33,
                       fill=INK, width=1)
    canvas.create_line(x - r * 0.97, y + r * 0.33, x + r * 0.97, y + r * 0.36,
                       fill=INK, width=1)
    canvas.create_line(x, y - r, x, y + r, fill=INK, width=1)
    # 브래킷 { } — 원 바깥 양옆
    for side in (-1, 1):
        bx = x + side * (r + 4)
        canvas.create_line(
            bx - side * 2, y - r * 0.95, bx + side * 1, y - r * 0.5,
            bx - side * 1, y, bx + side * 1, y + r * 0.5,
            bx - side * 2, y + r * 0.95,
            fill=INK, width=STROKE, smooth=True, capstyle="round")


class Widget:
    def __init__(self, lock=None):
        self._lock = lock   # GC로 닫히지 않게 프로세스 생존 동안 붙든다
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._refresh_now = threading.Event()
        self.last_ok = None
        self.automations = 0
        self.minimized = bool(self.cfg["minimized"])
        self._pill_pct = 0.0
        self._reserve_warn = None       # (개수, 예약%) or None
        self._status_mode = "ok"        # ok | err | warn
        self._sx = self._sy = None      # 드래그 시작 좌표 (None = 드래그 중 아님)
        self._drag_ox = self._drag_oy = 0

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self.root.configure(bg=KEY)
        self.root.wm_attributes("-transparentcolor", KEY)
        self.root.title(i18n.t("app.title"))

        fam = self._resolve_family()
        self.font = (fam, 10)
        self.font_small = (fam, 8)
        self.font_title = (fam, 10, "bold")
        self.font_pct = (fam, 11, "bold")

        self._build_ui()
        self._apply_view()
        self._place_window()
        self.root.deiconify()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.after(200, self._drain_queue)
        self.root.after(30_000, self._tick_age)

    # ── 폰트 폴백 ───────────────────────────────────────
    def _resolve_family(self):
        # 라운드 산세리프 우선 — 없으면 기존 폴백 순서 유지.
        fams = set(tkfont.families())
        for name in ("Quicksand", "Nunito", "Varela Round", "Inter", "Segoe UI"):
            if name in fams:
                return name
        return "TkDefaultFont"

    # ── UI 구성 ─────────────────────────────────────────
    def _build_ui(self):
        self.bg = tk.Canvas(self.root, bg=KEY, highlightthickness=0)
        self.bg.place(x=0, y=0, relwidth=1, relheight=1)

        self.content = tk.Frame(self.root, bg=CARD)
        self.content.pack(padx=PAD_OUT, pady=PAD_OUT)

        self._build_full(self.content)
        self._build_pill(self.content)

        # 우클릭 메뉴 (토플레벨 바인딩 → 모든 자식 이벤트 수신)
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg["topmost"]))
        self.autostart_var = tk.BooleanVar(value=STARTUP_BAT.exists())
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=i18n.t("menu.refresh"), command=self.manual_refresh)
        menu.add_command(label=i18n.t("menu.minimize"), command=self.toggle_minimize)
        menu.add_checkbutton(label=i18n.t("menu.topmost"), variable=self.topmost_var,
                             command=self._toggle_topmost)
        menu.add_checkbutton(label=i18n.t("menu.autostart"), variable=self.autostart_var,
                             command=self._toggle_autostart)
        menu.add_separator()
        menu.add_command(label=i18n.t("menu.quit"), command=self.quit)
        self.root.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    def _dot(self, parent, d=10, color=INK):
        """라인아트 점: 채운 원이 아니라 외곽선 원 + 작은 중심점(반복 모티프)."""
        c = tk.Canvas(parent, width=d, height=d, bg=CARD, highlightthickness=0)
        c.create_oval(1, 1, d - 1, d - 1, outline=color, width=1)
        c.create_oval(d / 2 - 1.5, d / 2 - 1.5, d / 2 + 1.5, d / 2 + 1.5,
                      fill=color, outline=color)
        return c

    def _globe(self, parent, r=7):
        w = int(2 * (r + 7))
        h = int(2 * r + 4)
        c = tk.Canvas(parent, width=w, height=h, bg=CARD, highlightthickness=0)
        _draw_globe(c, w / 2, h / 2, r)
        return c

    def _build_full(self, parent):
        self.full = tk.Frame(parent, bg=CARD, padx=10, pady=8)

        title_bar = tk.Frame(self.full, bg=CARD)
        title_bar.pack(fill="x")
        self._globe(title_bar).pack(side="left", padx=(0, 6))
        title = tk.Label(title_bar, text=i18n.t("app.title"), font=self.font_title,
                         bg=CARD, fg=FG)
        title.pack(side="left")

        # 우→좌 순으로 pack → 화면상 좌→우: —  ⟳  ✕
        close = tk.Label(title_bar, text="✕", font=self.font, bg=CARD, fg=DIM,
                         cursor="hand2")
        close.pack(side="right", padx=(6, 0))
        close.bind("<Button-1>", lambda e: self.quit())
        refresh = tk.Label(title_bar, text="⟳", font=self.font, bg=CARD, fg=DIM,
                           cursor="hand2")
        refresh.pack(side="right", padx=(6, 0))
        refresh.bind("<Button-1>", lambda e: self.manual_refresh())
        mini = tk.Label(title_bar, text="—", font=self.font, bg=CARD, fg=DIM,
                        cursor="hand2")
        mini.pack(side="right", padx=(6, 0))
        mini.bind("<Button-1>", lambda e: self.toggle_minimize())

        self.rows_frame = tk.Frame(self.full, bg=CARD)
        self.rows_frame.pack(fill="x", pady=(8, 2))
        self.loading = tk.Label(self.rows_frame, text=i18n.t("loading"),
                                font=self.font, bg=CARD, fg=DIM)
        self.loading.pack(anchor="w")

        status_bar = tk.Frame(self.full, bg=CARD)
        status_bar.pack(fill="x", pady=(6, 0))
        self.status_dot = self._dot(status_bar, 8)
        self.status_dot.pack(side="left", padx=(0, 5))
        self.status = tk.Label(status_bar, text="", font=self.font_small,
                               bg=CARD, fg=DIM, anchor="w")
        self.status.pack(side="left")

        self._bind_drag(self.full, title_bar, title, self.rows_frame,
                        status_bar, self.status)

    def _build_pill(self, parent):
        self.pill = tk.Frame(parent, bg=CARD, padx=12, pady=7)
        self._globe(self.pill, r=6).pack(side="left", padx=(0, 7))
        self.pill_lbl = tk.Label(self.pill, text=i18n.t("pill.init"),
                                 font=self.font_title, bg=CARD, fg=FG)
        self.pill_lbl.pack(side="left")
        self._bind_drag(self.pill, self.pill_lbl)

    # ── 뷰 전환/사이즈/배경 ─────────────────────────────
    def _apply_view(self):
        if self.minimized:
            self.full.pack_forget()
            self.pill.pack()
        else:
            self.pill.pack_forget()
            self.full.pack()
        self._resize()

    def _resize(self):
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = self.root.winfo_reqheight()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.bg.delete("all")
        _round_rect(self.bg, 2, 2, w - 2, h - 2, RADIUS, fill=CARD,
                    outline=INK, width=STROKE)

    def _place_window(self):
        self.root.update_idletasks()
        x, y = self.cfg["x"], self.cfg["y"]
        if x is None or y is None:
            x = self.root.winfo_screenwidth() - 300
            y = 60
        x = min(max(0, x), self.root.winfo_screenwidth() - 100)
        y = min(max(0, y), self.root.winfo_screenheight() - 60)
        self.root.geometry(f"+{x}+{y}")

    # ── 렌더 ────────────────────────────────────────────
    def _draw_bar(self, bar, pct, color, reserve):
        """크림 트랙 + 잉크 외곽선 위에 사용량 채움. reserve>0이면 제한선 표시."""
        r = BAR_H / 2
        inset = STROKE / 2
        _round_rect(bar, inset, inset, BAR_W - inset, BAR_H - inset, r,
                    fill=CREAM, outline=INK, width=STROKE)
        fill_w = (BAR_W - 2 * STROKE) * pct / 100
        if fill_w > 1:
            bar.create_rectangle(STROKE, STROKE, STROKE + fill_w,
                                 BAR_H - STROKE, fill=color, width=0)
        if reserve > 0:
            # 제한선은 점선 대신 짧은 잉크 선 — 라인아트 톤 유지
            x = STROKE + (BAR_W - 2 * STROKE) * (100 - reserve) / 100
            bar.create_line(x, 0, x, BAR_H, fill=LIMIT, width=STROKE)

    def _render(self, limits):
        for child in self.rows_frame.winfo_children():
            child.destroy()

        hidden = self.cfg["hidden_labels"]
        reserve = min(self.automations * self.cfg["automation_reserve_pct"], 80)
        session_pct = None
        max_pct = 0.0

        for item in limits:
            label = item["label"]
            if any(h in label.lower() for h in hidden):
                continue
            pct = item["used_pct"]
            max_pct = max(max_pct, pct)
            is_session = item["id"].startswith("session:") or item["id"] == "five_hour"
            if is_session:
                session_pct = pct
            warn_here = is_session and reserve > 0 and pct >= (100 - reserve)
            color = usage_color(pct)
            pct_color = LIMIT if warn_here else color

            row = tk.Frame(self.rows_frame, bg=CARD)
            row.pack(fill="x", pady=(8, 0))

            top = tk.Frame(row, bg=CARD)
            top.pack(fill="x")
            lab = tk.Label(top, text=label, font=self.font, bg=CARD, fg=FG,
                           anchor="w")
            lab.pack(side="left")
            reset_text = format_reset(item["resets_at"])
            rlab = tk.Label(top, text=reset_text, font=self.font_small, bg=CARD,
                            fg=DIM, anchor="e")
            rlab.pack(side="right")

            bot = tk.Frame(row, bg=CARD)
            bot.pack(fill="x", pady=(3, 0))
            bar = tk.Canvas(bot, width=BAR_W, height=BAR_H, bg=CARD,
                            highlightthickness=0)
            bar.pack(side="left")
            self._draw_bar(bar, pct, color,
                           reserve if (is_session and reserve > 0) else 0)
            plab = tk.Label(bot, text=f"{pct:.0f}%", font=self.font_pct, bg=CARD,
                            fg=pct_color, width=5, anchor="e")
            plab.pack(side="right")

            self._bind_drag(row, top, lab, rlab, bot, bar, plab)

        # 알약 % 값
        self._pill_pct = session_pct if session_pct is not None else max_pct
        self.pill_lbl.config(
            text=i18n.t("pill.session", pct=self._pill_pct) if session_pct is not None
            else i18n.t("pill.max", pct=self._pill_pct))

        # 자동화 예약분 경고
        if session_pct is not None and reserve > 0 and session_pct >= (100 - reserve):
            self._reserve_warn = (self.automations, reserve)
        else:
            self._reserve_warn = None

        # 행 수·라벨 길이는 응답마다 달라진다. root.geometry()로 크기를 한 번
        # 박아둔 창은 자식이 커져도 따라 늘지 않으므로 렌더할 때마다 다시 맞춘다
        # (풀 뷰에서 행이 잘리던 원인).
        self._resize()

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

    def _refresh_status(self):
        if self._status_mode == "err":
            return
        if self._reserve_warn:
            n, pct = self._reserve_warn
            self._status_mode = "warn"
            self.status.config(text=i18n.t("status.reserve_warn", count=n, pct=pct),
                               fg=LIMIT)
        else:
            self._status_mode = "ok"
            self.status.config(text=self._fresh_text(), fg=DIM)

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
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    data, autos = payload
                    self.last_ok = time.time()
                    self.automations = autos
                    self._status_mode = "ok"
                    self._render(data)
                    self._refresh_status()
                else:
                    self._status_mode = "err"
                    msg = i18n.t(ERROR_KEYS.get(type(payload), UNEXPECTED_ERROR_KEY))
                    self.status.config(text=msg, fg=ERR)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_queue)

    def _tick_age(self):
        if self.last_ok and self._status_mode != "err":
            self._refresh_status()
        self.root.after(30_000, self._tick_age)

    def manual_refresh(self):
        self._status_mode = "ok"
        self.status.config(text=i18n.t("refreshing"), fg=DIM)
        self._refresh_now.set()

    # ── 최소화(알약) 토글 ────────────────────────────────
    def toggle_minimize(self):
        self.minimized = not self.minimized
        self.cfg["minimized"] = self.minimized
        self._apply_view()
        self._save_config()

    # ── 드래그/설정 ─────────────────────────────────────
    def _bind_drag(self, *widgets):
        for w in widgets:
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, e):
        self._sx, self._sy = e.x_root, e.y_root
        self._drag_ox = e.x_root - self.root.winfo_x()
        self._drag_oy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._drag_ox}+{e.y_root - self._drag_oy}")

    def _drag_end(self, e):
        if self._sx is None:   # _drag_start 없이 온 ButtonRelease — 무시
            return
        moved = abs(e.x_root - self._sx) > 4 or abs(e.y_root - self._sy) > 4
        if moved:
            self._save_config()
        elif self.minimized:
            self.toggle_minimize()   # 알약 클릭(이동 아님) → 복원

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
            self._status_mode = "err"
            self.status.config(text=i18n.t("status.autostart_failed"), fg=ERR)

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
