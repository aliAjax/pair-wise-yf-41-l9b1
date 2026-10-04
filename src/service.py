from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if kind == "event":
            self._auto_recalc_for_event(entity)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "event" and action == "revise":
            self._auto_recalc_for_event(updated)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    def _load_kind(self, entity_id, kind):
        entity = self.repository.get_entity(entity_id)
        if not entity or entity["kind"] != kind:
            raise NotFoundError("%s not found: %s" % (kind, entity_id))
        return entity

    def _check_version(self, entity, expected_version):
        if expected_version is not None and int(expected_version) != entity["version"]:
            mainshock = entity["data"].get("mainshock_id") if entity["kind"] == "sequence" else None
            raise ConflictError(
                "version conflict: expected %s, found %s; current mainshock: %s"
                % (expected_version, entity["version"], mainshock)
            )

    def _auto_recalc_for_event(self, event):
        """Recalculate sequences whose window contains a newly created/revised event."""
        try:
            sequences = [s for s in self.repository.list_entities(kind="sequence")]
            events = self.repository.list_entities(kind="event")
            by_id = {item["id"]: item for item in events}
            for sequence in sequences:
                data = sequence["data"]
                window = data.get("window") or {}
                mainshock = by_id.get(data.get("mainshock_id"))
                if mainshock and self.rules.in_aftershock_window(event, mainshock, window):
                    fresh = self.repository.get_entity(sequence["id"])
                    self.recalculate_sequence(
                        Actor("system", "admin"), fresh["id"], expected_version=fresh["version"]
                    )
        except Exception:
            pass

    def create_sequence(self, actor, data, idempotency_key=None):
        return self.create(actor, "sequence", data, idempotency_key)

    def recalculate_sequence(self, actor, sequence_id, expected_version=None):
        sequence = self._load_kind(sequence_id, "sequence")
        self._check_version(sequence, expected_version)
        all_events = self.repository.list_entities(kind="event")
        mainshock_id, to_add, to_remove, final_members = self.rules.recalculate_plan(
            sequence, all_events
        )

        data = dict(sequence["data"])
        batch = data.get("batch")
        if isinstance(batch, dict) and batch.get("status") == "in_progress":
            batch_id = batch.get("batch_id") or str(uuid4())
            processed = dict(batch.get("processed") or {})
        else:
            batch_id = str(uuid4())
            processed = {}

        updated = sequence
        steps = [(event_id, "added") for event_id in to_add]
        steps += [(event_id, "removed") for event_id in to_remove]
        for event_id, action in steps:
            if event_id in processed:
                continue
            current_members = list(data.get("member_ids") or [])
            if action == "added":
                new_members = sorted(set(current_members) | {event_id})
                event_patch = {"sequence_id": sequence_id}
            else:
                new_members = [m for m in current_members if m != event_id]
                event_patch = {"sequence_id": None}
            new_data = dict(data)
            new_data["member_ids"] = new_members
            new_data["batch"] = {
                "batch_id": batch_id,
                "status": "in_progress",
                "processed": {**processed, event_id: action},
            }
            updated = self.repository.apply_sequence_step(
                sequence_id, updated["version"], new_data, event_id, event_patch
            )
            data = dict(updated["data"])
            processed = dict(data["batch"]["processed"])

        final_data = dict(data)
        final_data["mainshock_id"] = mainshock_id
        final_data["member_ids"] = final_members
        final_data["batch"] = {
            "batch_id": batch_id,
            "status": "done",
            "processed": processed,
        }
        updated = self.repository.update_entity(
            sequence_id, updated["version"], sequence["status"], final_data
        )
        self.audit.record(
            sequence_id,
            actor,
            "recalculate",
            sequence["status"],
            updated["status"],
            {
                "mainshock_id": mainshock_id,
                "added": [eid for eid, action in steps if action == "added"],
                "removed": [eid for eid, action in steps if action == "removed"],
                "batch_id": batch_id,
            },
        )
        return updated

    def publish_sequence_announcement(self, actor, sequence_id, note, expected_version=None):
        sequence = self._load_kind(sequence_id, "sequence")
        self._check_version(sequence, expected_version)
        data = dict(sequence["data"])
        announcements = list(data.get("announcements") or [])
        version = len(announcements) + 1
        snapshot = {
            "version": version,
            "mainshock_id": data.get("mainshock_id"),
            "member_ids": list(data.get("member_ids") or []),
            "note": note,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        announcements.append(snapshot)
        data["announcements"] = announcements
        updated = self.repository.update_entity(
            sequence_id, sequence["version"], sequence["status"], data
        )
        self.audit.record(
            sequence_id,
            actor,
            "publish_announcement",
            sequence["status"],
            updated["status"],
            {
                "version": version,
                "mainshock_id": snapshot["mainshock_id"],
                "member_count": len(snapshot["member_ids"]),
            },
        )
        return updated

    def backfill_sequence(self, actor, sequence_id):
        sequence = self._load_kind(sequence_id, "sequence")
        data = dict(sequence["data"])
        window = data.get("window") or {}
        events = self.repository.list_entities(kind="event")
        mainshock = next((e for e in events if e["id"] == data.get("mainshock_id")), None)
        if not mainshock:
            raise ValidationError("sequence has no mainshock")
        current = set(data.get("member_ids") or [])
        assigned = []
        for event in events:
            if event["data"].get("sequence_id"):
                continue
            if event["id"] in current:
                continue
            if self.rules.in_aftershock_window(event, mainshock, window):
                self.repository.update_entity(
                    event["id"],
                    event["version"],
                    event["status"],
                    {**event["data"], "sequence_id": sequence_id},
                )
                current.add(event["id"])
                assigned.append(event["id"])
        if assigned:
            new_data = dict(data)
            new_data["member_ids"] = sorted(current)
            self.repository.update_entity(
                sequence_id, sequence["version"], sequence["status"], new_data
            )
        self.audit.record(
            sequence_id,
            actor,
            "backfill",
            sequence["status"],
            sequence["status"],
            {"assigned": assigned},
        )
        return {"sequence_id": sequence_id, "assigned": assigned}

    def backfill_sequences(self, actor):
        sequences = [s for s in self.repository.list_entities(kind="sequence")]
        events = self.repository.list_entities(kind="event")
        details = []
        assigned_total = 0
        for sequence in sequences:
            data = dict(sequence["data"])
            window = data.get("window") or {}
            mainshock = next((e for e in events if e["id"] == data.get("mainshock_id")), None)
            if not mainshock:
                details.append({"sequence_id": sequence["id"], "assigned": []})
                continue
            current = set(data.get("member_ids") or [])
            assigned = []
            for event in events:
                if event["data"].get("sequence_id"):
                    continue
                if event["id"] in current:
                    continue
                if self.rules.in_aftershock_window(event, mainshock, window):
                    self.repository.update_entity(
                        event["id"],
                        event["version"],
                        event["status"],
                        {**event["data"], "sequence_id": sequence["id"]},
                    )
                    current.add(event["id"])
                    assigned.append(event["id"])
            if assigned:
                new_data = dict(data)
                new_data["member_ids"] = sorted(current)
                self.repository.update_entity(
                    sequence["id"], sequence["version"], sequence["status"], new_data
                )
            assigned_total += len(assigned)
            details.append({"sequence_id": sequence["id"], "assigned": assigned})
        self.audit.record("*", actor, "backfill", None, "done", {"assigned": assigned_total})
        return {"assigned": assigned_total, "sequences": details}

    def sequence_action(self, actor, sequence_id, action, data=None, expected_version=None):
        if action == "recalculate":
            return self.recalculate_sequence(actor, sequence_id, expected_version)
        if action == "publish_announcement":
            return self.publish_sequence_announcement(
                actor, sequence_id, (data or {}).get("note"), expected_version
            )
        if action == "backfill":
            return self.backfill_sequence(actor, sequence_id)
        raise ValidationError("unknown sequence action: " + action)
