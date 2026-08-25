"""Anthropic OAuth 사용량 API 클라이언트.

Claude Code가 저장해 둔 OAuth 토큰(~/.claude/.credentials.json)을 읽어
/usage 명령이 사용하는 것과 같은 사용량 엔드포인트를 호출한다.

accessToken이 만료(401)되면 같은 파일의 refreshToken으로 자동 재발급을
시도하고, 성공 시에만 credentials 파일을 원자적으로 다시 쓴다. 이 재발급
로직은 Claude Code CLI가 쓰는 것과 동일한 비공식 OAuth 엔드포인트를
사용하므로, 예고 없이 바뀌거나 막힐 수 있다 — 실패하면 예전처럼
TokenExpiredError로 폴백해 "Claude Code를 한 번 실행하세요" 안내로 돌아간다.

credentials 파일에 쓰는 경로는 이 재발급 하나뿐이며, fetch_usage(allow_refresh=False)로
통째로 끌 수 있다 (config.json의 auto_refresh_token). 남의 로그인 파일을 건드리는 것을
원치 않는 사용자를 위한 스위치다.
"""

import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import i18n

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SCHEDULED_TASKS_DIR = Path.home() / ".claude" / "scheduled-tasks"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# 이 User-Agent가 없으면 공격적 rate-limit 버킷에 걸려 지속적으로 429가 난다.
USER_AGENT = "claude-code/2.0.0"

# Claude Code CLI가 사용하는 OAuth 토큰 재발급 엔드포인트 (비공식).
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# expires_in의 상식적 상한(10년). 이를 넘는 값은 신뢰하지 않고 1시간으로 폴백한다.
_MAX_EXPIRES_IN = 10 * 365 * 24 * 3600

# limits 배열의 kind → i18n 키 (weekly_scoped는 모델명으로 동적 생성).
# 라벨 문자열이 아니라 키를 들고 있어야 language 설정이 실제로 먹는다.
KIND_KEYS = {
    "session": "limit.session",
    "weekly_all": "limit.weekly_all",
}

# 구식 최상위 윈도우 키 (limits 배열이 없을 때의 폴백).
# 값은 (i18n 키, 포맷 인자) — 모델별 주간은 weekly_scoped 문구를 재사용한다.
LEGACY_KEYS = {
    "five_hour": ("limit.session", {}),
    "seven_day": ("limit.weekly_all", {}),
    "seven_day_opus": ("limit.weekly_scoped", {"model": "Opus"}),
    "seven_day_sonnet": ("limit.weekly_scoped", {"model": "Sonnet"}),
}


class UsageError(Exception):
    """이 모듈이 던지는 모든 오류의 베이스."""


class TokenMissingError(UsageError):
    """credentials 파일이 없거나 accessToken을 찾지 못함."""


class TokenExpiredError(UsageError):
    """401 — 토큰 만료. Claude Code를 한 번 실행하면 갱신된다."""


class RateLimitedError(UsageError):
    """429 — 폴링 주기를 늘려야 함."""


class NetworkError(UsageError):
    """네트워크 불통 또는 예상 밖의 HTTP 오류."""


class ParseError(UsageError):
    """응답 형식이 예상과 다름 (엔드포인트 스펙 변경 가능성)."""


def _load_credentials() -> dict:
    """credentials 파일 전체를 읽어 claudeAiOauth 객체를 반환한다."""
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TokenMissingError("credentials file not found — log in with Claude Code")
    except (OSError, ValueError) as e:
        # ValueError = JSONDecodeError(깨진 JSON) + UnicodeDecodeError(비-UTF8).
        # 둘 다 사실상 로그인 정보를 못 읽는 것이므로 같은 안내로 보낸다.
        raise TokenMissingError(f"could not read credentials: {e}")

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise TokenMissingError("no accessToken — log in with Claude Code")
    return oauth


def _load_token() -> str:
    return _load_credentials()["accessToken"]


def _save_credentials(oauth: dict) -> None:
    """claudeAiOauth 객체를 credentials 파일에 원자적으로 다시 쓴다.

    같은 디렉터리에 임시 파일을 만들어 os.replace로 교체하므로, 쓰는 도중
    Claude Code가 같은 파일을 읽어도 손상된 내용을 보지 않는다.
    """
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 없음/깨진 JSON/비-UTF8 → 새로 쓴다
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["claudeAiOauth"] = oauth

    fd, tmp_path = tempfile.mkstemp(
        dir=str(CREDENTIALS_PATH.parent), prefix=".credentials.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, CREDENTIALS_PATH)
    finally:
        # os.replace 성공 시 tmp_path는 이미 사라졌다 — 남아 있을 때만 정리.
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _expires_at_ms(value) -> int:
    """expires_in(초) → 만료 시각(ms). 값이 상식적인 범위를 벗어나면 1시간 후.

    json.loads는 기본 설정에서 NaN/Infinity를 그대로 float으로 받고(1e400 같은
    값도 inf가 된다), 1e308처럼 유한하지만 거대한 값은 뒤의 곱셈에서 inf가 되어
    int() 변환이 OverflowError로 터진다. 그래서 유한성만이 아니라 범위까지
    본다 — 0 ≤ seconds ≤ 10년을 벗어나면 어차피 쓸 수 없는 값이다.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: 자릿수 큰 정수
        seconds = 3600.0
    if not (math.isfinite(seconds) and 0 <= seconds <= _MAX_EXPIRES_IN):
        seconds = 3600.0  # NaN은 비교가 모두 False라 여기로 온다
    return int((time.time() + seconds) * 1000)


def _refresh_access_token(timeout: float = 15) -> str:
    """refreshToken으로 새 accessToken을 발급받아 파일에 저장하고 반환한다.

    refreshToken이 없거나 재발급 요청이 실패하면 TokenExpiredError를 던져
    기존 폴백 경로("Claude Code를 한 번 실행하세요")로 넘어가게 한다.
    비공식 엔드포인트이므로 응답을 신뢰하지 않는다 — 네트워크 오류, 비-JSON·
    비-UTF8 본문, dict가 아닌 본문, access_token 부재, 비정상 expires_in,
    저장 실패는 모두 TokenExpiredError로 바꾸거나 흡수한다.

    다만 "UsageError만 던진다"고 단정하지는 않는다: 예컨대 http.client의
    IncompleteRead처럼 urlopen/read()가 드물게 내는 예외는 위 except 튜플에
    걸리지 않는다. 최종 방어선은 main.py _poll_loop의 except Exception이다.
    """
    oauth = _load_credentials()
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise TokenExpiredError("token expired, no refreshToken — run Claude Code once")

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"content-type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # ValueError = JSONDecodeError + UnicodeDecodeError. 프록시·방화벽이
        # latin-1 오류 페이지를 뱉으면 후자가 난다 (JSONDecodeError가 아니다).
        raise TokenExpiredError(f"token refresh failed — run Claude Code once ({e})")

    if not isinstance(result, dict):
        raise TokenExpiredError("unexpected shape in token refresh response")

    new_access = result.get("access_token")
    if not new_access:
        raise TokenExpiredError("no access_token in token refresh response")

    new_oauth = {
        **oauth,
        "accessToken": new_access,
        "refreshToken": result.get("refresh_token", refresh_token),
        "expiresAt": _expires_at_ms(result.get("expires_in", 3600)),
    }
    # 저장 실패해도 재발급 자체는 이미 성공했으므로 새 accessToken을 버리지 않고
    # 그대로 반환해 이번 폴링은 성공시킨다. 던지는 쪽이 엄밀히 더 나쁘다: 파일엔
    # 구 accessToken + 구 refreshToken이 남고, 서버가 refreshToken을 회전시켰다면
    # 그 구 refreshToken은 이미 죽었다 — 다음 폴링은 어느 쪽을 택했든 401 →
    # 재발급 실패 → 수동 안내로 떨어진다. 즉 이건 자동 복구가 아니라, 잃을 것
    # 없이 이번 한 번을 더 건지는 선택이다.
    try:
        _save_credentials(new_oauth)
    except OSError:
        pass
    return new_access


def _parse_ts(value):
    """ISO 타임스탬프 또는 유닉스 초/밀리초 → aware datetime (UTC). 실패 시 None."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # 1e11초는 서기 5138년 — 이보다 크면 밀리초로 온 것으로 본다.
            if value > 1e11:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, OSError):
        return None


def _clamp_pct(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _parse_limits(data: dict) -> list:
    """/usage 명령이 쓰는 것과 같은 limits 배열을 파싱한다 (1차 소스)."""
    items = data.get("limits")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        used = _clamp_pct(item.get("percent"))
        if used is None:
            continue
        kind = str(item.get("kind") or "limit")
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        scope_name = model.get("display_name")
        if kind == "weekly_scoped" and scope_name:
            label = i18n.t("limit.weekly_scoped", model=scope_name)
        elif kind in KIND_KEYS:
            label = i18n.t(KIND_KEYS[kind])
        else:
            label = kind  # 미지의 kind도 버리지 않음
        out.append({
            "id": f"{kind}:{scope_name or ''}",
            "label": label,
            "used_pct": used,
            "resets_at": _parse_ts(item.get("resets_at")),
        })
    return out


def _parse_legacy(data: dict) -> list:
    """limits 배열이 없을 때 구식 최상위 윈도우 키로 폴백."""
    out = []
    for key, (label_key, label_fmt) in LEGACY_KEYS.items():
        w = data.get(key)
        if not isinstance(w, dict):
            continue
        used = _clamp_pct(w.get("utilization"))
        if used is None:
            continue
        out.append({
            "id": key,
            "label": i18n.t(label_key, **label_fmt),
            "used_pct": used,
            "resets_at": _parse_ts(w.get("resets_at")),
        })
    return out


def _call_usage_api(token: str, timeout: float):
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_usage(timeout: float = 15, allow_refresh: bool = True) -> list:
    """사용량 한도 목록을 조회해 /usage와 같은 기준(사용한 %)으로 반환한다.

    반환 형식: [{"id": str, "label": str, "used_pct": float,
                 "resets_at": datetime|None}, ...] (표시 순서대로)

    accessToken이 만료되어 401이 오면, 먼저 credentials 파일을 다시 읽어
    Claude Code가 그 사이에 스스로 갱신했는지 확인한다. 갱신됐으면 그 토큰으로
    재요청만 한다(재발급하면 refreshToken이 회전해 CLI가 로그아웃된다).
    여전히 같은 토큰이면 그때만 refreshToken으로 재발급한 뒤 재요청한다.
    재발급도 실패하면 TokenExpiredError를 그대로 던진다.

    allow_refresh=False면 재발급 단계를 건너뛰고 바로 TokenExpiredError를 던진다.
    credentials 파일을 쓰는 유일한 경로가 재발급이므로, 이 인자가 곧 "이 위젯이
    내 로그인 파일에 쓰게 할 것인가" 스위치다. 401 직후 파일을 다시 읽어보는
    단계는 읽기 전용이라 allow_refresh와 무관하게 항상 수행한다.
    """
    token = _load_token()  # 매번 다시 읽어 Claude Code가 갱신한 토큰을 반영
    try:
        raw = _call_usage_api(token, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Claude Code가 이미 갱신했는지 먼저 확인 — 그렇다면 재발급 금지.
            try:
                current = _load_token()
            except TokenMissingError:
                current = None
            if current and current != token:
                new_token = current
            elif allow_refresh:
                new_token = _refresh_access_token(timeout=timeout)  # 실패 시 TokenExpiredError
            else:
                raise TokenExpiredError(
                    "token expired, auto refresh disabled — run Claude Code once")
            try:
                raw = _call_usage_api(new_token, timeout)
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    raise TokenExpiredError("refreshed token was rejected — run Claude Code once")
                if e2.code == 429:
                    raise RateLimitedError("rate limited (429) — widening the poll interval")
                raise NetworkError(f"HTTP {e2.code}")
            except (urllib.error.URLError, TimeoutError, OSError) as e2:
                raise NetworkError(str(e2))
        elif e.code == 429:
            raise RateLimitedError("rate limited (429) — widening the poll interval")
        else:
            raise NetworkError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetworkError(str(e))

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ParseError("response is not JSON")

    limits = _parse_limits(data) or _parse_legacy(data)
    if not limits:
        raise ParseError("no usage limits in response — format may have changed")
    return limits


def count_automations() -> int:
    """~/.claude/scheduled-tasks/ 아래 SKILL.md를 가진 하위 폴더 수.

    폴더가 없거나 읽기 실패 시 0 (크래시 금지, 쓰기 없음 — 읽기 전용).
    UI 계층이 파일시스템을 직접 뒤지지 않도록 데이터 계층에 둔다.
    """
    try:
        return sum(
            1 for d in SCHEDULED_TASKS_DIR.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        )
    except OSError:
        return 0


if __name__ == "__main__":
    # 단독 실행 시 간단 동작 확인 (토큰은 출력하지 않음).
    # 인자로 언어 코드를 주면 라벨 번역까지 함께 확인할 수 있다: python usage_api.py ko
    if len(sys.argv) > 1:
        i18n.set_language(sys.argv[1])
    for item in fetch_usage():
        reset = item["resets_at"].astimezone().strftime("%m/%d %H:%M") if item["resets_at"] else "?"
        print(f"{item['label']:<16} used {item['used_pct']:5.1f}%  resets {reset}")
    print(f"automations (scheduled-tasks): {count_automations()}")
