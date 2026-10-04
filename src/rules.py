import math
from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_station(actor, data, lookup):
    if not data.get("code"):
        raise ValidationError("station code is required")


def _validate_event(actor, data, lookup):
    reports = data.get("reports") or []
    if len(reports) < 2:
        raise ValidationError("event requires at least two station reports")
    if not data.get("title"):
        raise ValidationError("event title is required")


def _validate_associate(actor, entity, data, lookup):
    reports = entity["data"].get("reports") or []
    if len(reports) < 2:
        raise ValidationError("two reports are required for association")
    return {"associated_count": len(reports)}


def associate_reports(reports, max_delta=120, max_distance=3.0):
    if not reports:
        return []
    anchor = reports[0]
    result = [anchor]
    for report in reports[1:]:
        if abs(float(report.get("time_offset", 0))) <= max_delta and float(report.get("distance_km", 0)) <= max_distance:
            result.append(report)
    return result


def magnitude_median(amplitudes):
    values = sorted(float(value) for value in amplitudes)
    if not values:
        raise ValidationError("amplitudes are required")
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def _validate_sequence(actor, data, lookup):
    if not data.get("name"):
        raise ValidationError("sequence name is required")
    window = data.get("window") or {}
    if window.get("max_days") is None and window.get("max_distance_km") is None:
        raise ValidationError("sequence window requires max_days or max_distance_km")


def _parse_origin(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(float(lat1)))
        * math.cos(math.radians(float(lat2)))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def select_mainshock(events):
    """Mainshock = largest magnitude, ties broken by earliest origin time."""
    candidates = [event for event in events if event.get("data", {}).get("magnitude") is not None]
    if not candidates:
        return None

    def _key(event):
        data = event["data"]
        return (-float(data["magnitude"]), str(data.get("origin_time", "")))

    return min(candidates, key=_key)


def in_aftershock_window(event, mainshock, window):
    """True when event is within the sequence's space-time window of the mainshock."""
    if not window:
        return True
    max_days = window.get("max_days")
    max_distance = window.get("max_distance_km")
    if max_days is not None:
        origin = event.get("data", {}).get("origin_time")
        main_origin = mainshock.get("data", {}).get("origin_time")
        if origin and main_origin:
            delta_days = abs((_parse_origin(origin) - _parse_origin(main_origin)).total_seconds()) / 86400.0
            if delta_days > float(max_days):
                return False
    if max_distance is not None:
        lat1 = event.get("data", {}).get("lat")
        lon1 = event.get("data", {}).get("lon")
        lat2 = mainshock.get("data", {}).get("lat")
        lon2 = mainshock.get("data", {}).get("lon")
        if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
            return False
        if haversine_km(lat1, lon1, lat2, lon2) > float(max_distance):
            return False
    return True


def recalculate_plan(sequence, all_events):
    """Recompute the mainshock and member set for a sequence.

    The mainshock is the largest event in the cluster; the cluster is the set
    of events within the space-time window of the mainshock. Iterates until the
    mainshock is stable so a bigger event re-centers the window.
    Returns (mainshock_id, to_add, to_remove, final_member_ids).
    """
    data = sequence.get("data", {})
    window = data.get("window") or {}
    events_by_id = {event["id"]: event for event in all_events}
    current_members = set(data.get("member_ids") or [])

    anchor_id = data.get("mainshock_id")
    if anchor_id not in events_by_id:
        seed = [events_by_id[member] for member in current_members if member in events_by_id]
        if not seed:
            seed = list(events_by_id.values())
        anchor = select_mainshock(seed)
        anchor_id = anchor["id"] if anchor else None

    mainshock_id = anchor_id
    cluster = set()
    seen = set()
    for _ in range(20):
        if mainshock_id in seen:
            break
        seen.add(mainshock_id)
        anchor = events_by_id.get(mainshock_id)
        if anchor is None:
            break
        cluster = {
            event_id
            for event_id, event in events_by_id.items()
            if in_aftershock_window(event, anchor, window)
        }
        new_mainshock = select_mainshock([events_by_id[event_id] for event_id in cluster])
        new_id = new_mainshock["id"] if new_mainshock else None
        if new_id == mainshock_id:
            break
        mainshock_id = new_id

    final_members = cluster
    to_add = sorted(final_members - current_members)
    to_remove = sorted(current_members - final_members)
    return mainshock_id, to_add, to_remove, sorted(final_members)


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'sequence': _validate_sequence}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'sequences': 'sequence'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'sequence': 'active'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports'), 'sequence': ('name', 'window')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'sequence': ('admin', 'analyst')}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def select_mainshock(self, events):
        return select_mainshock(events)

    def in_aftershock_window(self, event, mainshock, window):
        return in_aftershock_window(event, mainshock, window)

    def recalculate_plan(self, sequence, all_events):
        return recalculate_plan(sequence, all_events)


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
