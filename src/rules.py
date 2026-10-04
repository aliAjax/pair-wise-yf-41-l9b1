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


def _validate_sequence(actor, data, lookup):
    name = data.get("name")
    if not name:
        raise ValidationError("sequence name is required")
    anchor_ids = data.get("anchor_event_ids") or []
    if not anchor_ids:
        raise ValidationError("sequence requires at least one anchor event")
    if len(set(anchor_ids)) != len(anchor_ids):
        raise ValidationError("duplicate anchor events are not allowed")
    for event_id in anchor_ids:
        if not lookup("event", "id", event_id):
            raise ValidationError("anchor event does not exist: " + str(event_id))
    days = data.get("window_days")
    distance = data.get("window_distance_km")
    if days is not None and float(days) <= 0:
        raise ValidationError("window_days must be positive")
    if distance is not None and float(distance) <= 0:
        raise ValidationError("window_distance_km must be positive")


def _validate_sequence_attach(actor, entity, data, lookup):
    event_id = data.get("event_id")
    if not event_id:
        raise ValidationError("event_id is required")
    rows = lookup("event", "id", event_id) or []
    if not rows:
        raise ValidationError("event does not exist: " + str(event_id))
    return {"event_id": event_id}


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


CUSTOM_CREATE = {'station': _validate_station, 'event': _validate_event, 'sequence': _validate_sequence}
CUSTOM_TRANSITIONS = {('event', 'associate'): _validate_associate, ('sequence', 'attach'): _validate_sequence_attach}


class RuleEngine:
    ALIASES = {'stations': 'station', 'events': 'event', 'sequences': 'sequence', 'sequence_notices': 'sequence_notice'}
    INITIAL_STATUS = {'station': 'online', 'event': 'candidate', 'sequence': 'draft', 'sequence_notice': 'published'}
    TRANSITIONS = {'station': {'offline': (('online',), 'offline'), 'online': (('offline',), 'online')}, 'event': {'associate': (('candidate',), 'associated'), 'review': (('associated',), 'reviewed'), 'publish': (('reviewed',), 'published'), 'revise': (('published', 'revised'), 'revised'), 'withdraw': (('published', 'revised'), 'withdrawn')}, 'sequence': {'attach': (('draft', 'active', 'frozen'), None), 'recompute': (('draft', 'active', 'frozen'), None), 'publish': (('draft', 'active'), 'frozen'), 'unfreeze': (('frozen',), 'active')}}
    CREATE_REQUIRED = {'station': ('code', 'lat', 'lon'), 'event': ('title', 'origin_time', 'location', 'reports'), 'sequence': ('name', 'anchor_event_ids')}
    ACTION_REQUIRED = {('station', 'offline'): ('reason',), ('event', 'review'): ('reviewer', 'magnitude'), ('event', 'publish'): ('communication_id',), ('event', 'revise'): ('reason', 'magnitude'), ('event', 'withdraw'): ('reason',), ('sequence', 'attach'): ('event_id',), ('sequence', 'publish'): ('communication_id',)}
    CREATE_ROLES = {'station': ('admin', 'station'), 'event': ('admin', 'analyst'), 'sequence': ('admin', 'analyst'), 'sequence_notice': ('admin',)}
    ROLE_ACTIONS = {'offline': ('admin', 'station'), 'online': ('admin', 'station'), 'associate': ('admin', 'analyst'), 'review': ('admin', 'reviewer'), 'publish': ('admin', 'reviewer', 'analyst'), 'revise': ('admin', 'reviewer'), 'withdraw': ('admin', 'reviewer'), 'attach': ('admin', 'analyst'), 'recompute': ('admin', 'analyst', 'reviewer'), 'unfreeze': ('admin', 'reviewer')}

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
        if kind == "sequence_notice":
            raise InvalidTransition(
                "sequence_notice is created only by publishing/recomputing a sequence"
            )
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
        return next_status or entity["status"], patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
