"""주간 사용량 기록과 요일별 사용량 계산.

API는 주간 누적 %만 준다. 요일별 사용량을 그리려면 위젯이 폴링할 때마다 누적 %를
기록해 두고 하루 단위로 차이를 내야 한다. 이 모듈은 그 계산만 맡는 leaf 모듈이다
(어떤 프로젝트 모듈도 import 하지 않는다). 파일 읽기·쓰기는 실패해도 던지지 않는다
— 기록이 없다고 위젯이 안 뜨면 안 된다(불변조건 5).
"""

import json
import os
from pathlib import Path

DAY = 86400
KEEP = 8 * DAY          # 주간 창(7일)보다 하루 넉넉히 남긴다
HEARTBEAT = 3600        # 값이 그대로여도 1시간에 한 번은 남긴다(그날 0%를 알 수 있게)


def load(path: Path) -> list:
    """[(epoch초, 누적%), ...] 시간순. 파일이 없거나 깨졌으면 빈 목록."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = [(float(t), float(p)) for t, p in raw]
    except (OSError, ValueError, TypeError):
        return []
    return sorted(rows)


def save(path: Path, samples: list) -> None:
    """임시 파일 + os.replace로 원자적으로 쓴다. 실패는 조용히 넘긴다."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps([[round(t), p] for t, p in samples]), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record(samples: list, ts: float, pct: float) -> bool:
    """표본을 덧붙이고 오래된 것을 지운다. 목록이 바뀌었으면 True."""
    changed = False
    if not samples or samples[-1][1] != pct or ts - samples[-1][0] >= HEARTBEAT:
        samples.append((ts, pct))
        changed = True
    cutoff = ts - KEEP
    while samples and samples[0][0] < cutoff:
        samples.pop(0)
        changed = True
    return changed


def daily_usage(samples: list, start: float, now: float, days: int = 7) -> list:
    """창 시작(start)부터 하루씩 나눈 칸마다 그날 쓴 %. 모르는 날은 None.

    하루 = 창 시작 시각부터 24시간(자정 기준이 아니다 — 리셋 요일·시각을 따른다).
    그날 쓴 양 = 그날 마지막 표본 − 바로 전날의 마지막 표본.
    - 창의 첫날은 기준을 0%로 둔다(리셋 직후는 0%에서 시작하므로 정확하다).
    - 바로 전날 표본이 없으면(위젯이 꺼져 있던 날 다음) 그날 첫 표본을 기준으로
      삼는다. 더 앞선 날의 표본을 쓰면 꺼져 있던 며칠치가 이날 하루에 몰려
      예산을 넘은 막대로 보인다. 대신 이날은 실제보다 적게 나올 수 있다.
    - 그날 표본이 하나도 없으면(위젯이 꺼져 있었으면) None.
    """
    window = [(t, p) for t, p in samples if start <= t <= now]
    out = []
    for i in range(days):
        b0, b1 = start + i * DAY, start + (i + 1) * DAY
        if b0 > now:
            out.append(None)
            continue
        inside = [p for t, p in window if b0 <= t < b1]
        if not inside:
            out.append(None)
            continue
        prev_day = [p for t, p in window if b0 - DAY <= t < b0]
        if prev_day:
            base = prev_day[-1]
        elif i == 0:
            base = 0.0
        else:
            base = inside[0]
        out.append(max(0.0, inside[-1] - base))
    return out
