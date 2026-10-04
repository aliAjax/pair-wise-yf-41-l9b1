"""余震序列的纯领域计算：主震选择、时空窗口、成员判定。

规则：
- 主震 = 候选锚点中震级最大、震级并列时发震时间最早的事件。
- 成员 = 全目录中落在主震时空窗口内的事件（时间取主震之后，余震口径）。
- 窗口可由序列显式覆盖；缺省按震级查 Gardner-Knopoff 式简化表。
本模块不接触数据库，方便单测和批量重算复用。
"""

from datetime import datetime, timedelta

from .domain import ValidationError

# 简化的震级 -> (时间窗口天数, 空间窗口公里)，量级参考 Gardner-Knopoff 对照表
_DEFAULT_WINDOW = (
    (2.5, 6.0, 20.0),
    (4.0, 42.0, 30.0),
    (5.0, 83.0, 40.0),
    (6.0, 155.0, 53.0),
    (7.0, 269.0, 61.0),
    (8.0, 510.0, 100.0),
    (8.5, 780.0, 110.0),
)


def parse_time(value):
    """解析 ISO 时间；Z 结尾按 UTC，无时区也按 UTC。"""
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("invalid origin_time: %s" % value) from exc
    if moment.tzinfo is None:
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("numeric value expected, got %r" % (value,)) from exc


def event_origin(event):
    return parse_time((event or {}).get("data", event or {}).get("origin_time"))


def event_magnitude(event):
    data = (event or {}).get("data", event or {})
    return _as_float(data.get("magnitude"))


def event_coordinates(event):
    data = (event or {}).get("data", event or {})
    lat = _as_float(data.get("lat"))
    lon = _as_float(data.get("lon"))
    if lat is None or lon is None:
        return None
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValidationError("event %s has out-of-range coordinates" % data.get("id"))
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    hav = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2.0 * asin(sqrt(hav))


def default_window(magnitude):
    """按主震震级给缺省窗口，返回 (days, distance_km)。"""
    magnitude = float(magnitude)
    days, distance = _DEFAULT_WINDOW[0][1], _DEFAULT_WINDOW[0][2]
    for threshold, day_value, distance_value in _DEFAULT_WINDOW:
        if magnitude >= threshold:
            days, distance = day_value, distance_value
    return days, distance


def resolve_window(sequence_data, mainshock_magnitude):
    """序列显式覆盖优先，否则按主震震级查缺省表。"""
    days = _as_float(sequence_data.get("window_days"))
    distance = _as_float(sequence_data.get("window_distance_km"))
    if days is None or distance is None:
        default_days, default_distance = default_window(mainshock_magnitude)
        days = days if days is not None else default_days
        distance = distance if distance is not None else default_distance
    if days <= 0 or distance <= 0:
        raise ValidationError("sequence window must be positive")
    return {"days": days, "distance_km": distance}


def select_mainshock(anchor_events):
    """震级最大且时间最早；缺坐标/时间/震级的锚点不能当主震。"""
    candidates = []
    for event in anchor_events:
        magnitude = event_magnitude(event)
        origin = event_origin(event)
        coords = event_coordinates(event)
        if magnitude is None or origin is None or coords is None:
            continue
        candidates.append((magnitude, origin, event["id"]))
    if not candidates:
        return None
    # 震级降序、时间升序
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates[0][2]


def compute_membership(sequence_data, events, anchor_ids):
    """给定序列配置、目录事件和锚点 id，返回主震、窗口、成员。

    主震只在锚点里挑；成员对全目录开窗判定，这样主震一换，
    新落进窗口的事件能被收进来，掉出窗口的被移出去。
    """
    by_id = {event["id"]: event for event in events if event.get("kind", "event") == "event"}
    anchor_events = [by_id[event_id] for event_id in anchor_ids if event_id in by_id]
    mainshock_id = select_mainshock(anchor_events)
    if mainshock_id is None:
        return None, None, []
    mainshock = by_id[mainshock_id]
    magnitude = event_magnitude(mainshock)
    window = resolve_window(sequence_data, magnitude)
    origin = event_origin(mainshock)
    lat, lon = event_coordinates(mainshock)
    horizon = origin + timedelta(days=window["days"])

    members = []
    for event in events:
        if event.get("kind", "event") != "event":
            continue
        event_origin_time = event_origin(event)
        coords = event_coordinates(event)
        if event_origin_time is None or coords is None:
            continue
        if event_origin_time < origin or event_origin_time > horizon:
            continue
        distance = haversine_km(lat, lon, coords[0], coords[1])
        if distance <= window["distance_km"]:
            members.append(event["id"])
    members.sort(key=lambda event_id: event_origin(by_id[event_id]))
    return mainshock_id, window, members