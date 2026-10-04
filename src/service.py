from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine
from .sequences import compute_membership


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
        if kind == "sequence":
            # 两名编目员提交同一条序列：重名直接按冲突处理，并回传当前主震
            for existing_seq in self.repository.list_entities(kind="sequence"):
                if existing_seq["data"].get("name") == payload.get("name"):
                    raise ConflictError(
                        "sequence already exists: %s" % payload.get("name"),
                        details={
                            "existing_sequence_id": existing_seq["id"],
                            "current_version": existing_seq["version"],
                            "mainshock_event_id": existing_seq["data"].get("mainshock_event_id"),
                        },
                    )
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if kind == "sequence":
            # 建列即按主震窗口算出初始成员（成员表写入，版本不重复抬升）
            entity, _ = self._recompute_persist(
                actor, entity, trigger="create", expected_version=1, bump_version=False
            )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])

        if kind == "sequence" and action in ("attach", "recompute", "publish"):
            # 序列的成员写动作都汇聚到重算逻辑，保证主震一变成员立刻对账
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
            working = dict(entity["data"])
            if action == "attach":
                event_id = patch["event_id"]
                anchors = working.get("anchor_event_ids") or []
                if event_id in anchors:
                    raise ValidationError("event already attached to sequence: " + str(event_id))
                working["anchor_event_ids"] = anchors + [event_id]
            elif action == "recompute":
                for key in ("window_days", "window_distance_km"):
                    if key in patch:
                        working[key] = patch[key]
            elif action == "publish":
                working["communication_id"] = patch["communication_id"]
            expected = int(expected_version) if expected_version is not None else entity["version"]
            updated, _ = self._recompute_persist(
                actor,
                entity,
                override_data=working,
                expected_version=expected,
                next_status=next_status,
                communication_id=working.get("communication_id") if action == "publish" else None,
                trigger=action,
            )
            return updated

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
        return updated

    # ----- 余震序列 -----
    def _notice_count(self, sequence_id):
        return len(
            [
                notice
                for notice in self.repository.list_entities(kind="sequence_notice")
                if notice["data"].get("sequence_id") == sequence_id
            ]
        )

    def _recompute_persist(self, actor, sequence, *, override_data=None, expected_version=None,
                           next_status=None, communication_id=None, trigger="recompute",
                           bump_version=True):
        """按当前锚点重算主震和成员并落库。

        - 主震 = 锚点中震级最大、时间最早者；
        - 成员对全目录开窗：掉出窗口的删除，新落进窗口的收入；
        - 已发布公告的序列保留原公告，重算结果以新公告版本留存。
        仓储层对成员表做差集写入，已是目标状态的成员不再改动。
        """
        sequence_id = sequence["id"]
        data = dict(override_data if override_data is not None else sequence["data"])
        events = self.repository.list_entities(kind="event")
        anchors = data.get("anchor_event_ids") or []
        mainshock_id, window, members = compute_membership(data, events, anchors)
        if mainshock_id is None:
            raise ValidationError("no eligible mainshock among anchor events")

        previous_mainshock = data.get("mainshock_event_id")
        data["mainshock_event_id"] = mainshock_id
        data["effective_window"] = window
        data["member_count"] = len(members)

        target_status = next_status or sequence["status"]
        published = self._notice_count(sequence_id)
        frozen_now = target_status == "frozen"
        new_version_on_frozen = sequence["status"] == "frozen" and target_status == "frozen" and trigger != "create"
        notice_entity = None
        notice_version = published
        if frozen_now or new_version_on_frozen:
            notice_version = published + 1
            data["announcement_version"] = notice_version
            notice_entity = {
                "id": "%s-notice-v%d" % (sequence_id, notice_version),
                "status": "published",
                "data": {
                    "sequence_id": sequence_id,
                    "sequence_name": data.get("name"),
                    "version": notice_version,
                    "communication_id": communication_id or data.get("communication_id"),
                    "mainshock_event_id": mainshock_id,
                    "window": window,
                    "member_event_ids": members,
                    "member_count": len(members),
                    "trigger": trigger,
                },
                "created_by": actor.user_id,
            }

        audit_entries = [{
            "entity_id": sequence_id,
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": trigger if trigger in ("create",) else "recompute",
            "from_status": sequence["status"],
            "to_status": target_status,
            "detail": {
                "mainshock_event_id": mainshock_id,
                "previous_mainshock_event_id": previous_mainshock,
                "mainshock_changed": previous_mainshock is not None and previous_mainshock != mainshock_id,
                "window": window,
                "announcement_version": notice_version or None,
                "trigger": trigger,
            },
        }]
        if notice_entity is not None:
            audit_entries.append({
                "entity_id": notice_entity["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "create",
                "from_status": None,
                "to_status": "published",
                "detail": {"kind": "sequence_notice", "sequence_id": sequence_id,
                           "version": notice_version},
            })
        prior_members = set(self.repository.list_sequence_members(sequence_id))
        added = sorted(set(members) - prior_members)
        removed = sorted(prior_members - set(members))
        for event_id in added:
            audit_entries.append({
                "entity_id": event_id, "actor_id": actor.user_id, "actor_role": actor.role,
                "action": "sequence_member_added", "from_status": "", "to_status": "member",
                "detail": {"sequence_id": sequence_id, "mainshock_event_id": mainshock_id},
            })
        for event_id in removed:
            audit_entries.append({
                "entity_id": event_id, "actor_id": actor.user_id, "actor_role": actor.role,
                "action": "sequence_member_removed", "from_status": "member", "to_status": "",
                "detail": {"sequence_id": sequence_id, "mainshock_event_id": mainshock_id},
            })

        try:
            updated, changes = self.repository.recompute_sequence(
                sequence_id,
                expected_version,
                target_status,
                data,
                members,
                notice_entity,
                audit_entries,
                bump_version=bump_version,
            )
        except ConflictError as exc:
            # 后到的提交者要看到冲突，以及此刻的主震
            current = self.repository.get_entity(sequence_id)
            raise ConflictError(
                str(exc),
                details={
                    "current_version": current["version"],
                    "mainshock_event_id": current["data"].get("mainshock_event_id"),
                    "member_count": current["data"].get("member_count"),
                    "announcement_version": current["data"].get("announcement_version"),
                },
            ) from exc

        return updated, changes

    def recompute_sequence(self, actor, sequence_id, expected_version=None, patch=None):
        sequence = self.repository.get_entity(sequence_id)
        if not sequence or self.rules.normalize_kind(sequence["kind"]) != "sequence":
            raise NotFoundError("sequence not found: " + sequence_id)
        # 走一遍状态机，拿到角色/状态校验，next_status 保持现状
        provided = dict(patch or {})
        if provided:
            self.rules.validate_transition(
                actor, sequence, "recompute", provided, self._lookup
            )
        else:
            self.rules.validate_transition(
                actor, sequence, "recompute", {}, self._lookup
            )
        override_data = dict(sequence["data"])
        for key in ("window_days", "window_distance_km"):
            if key in provided:
                override_data[key] = provided[key]
        updated, changes = self._recompute_persist(
            actor, sequence, override_data=override_data,
            expected_version=expected_version,
            trigger="recompute",
        )
        return updated, changes

    # ----- 整批重算：失败留批次、只重试未完成项 -----
    def recompute_batch(self, actor, sequence_ids=None):
        self._ensure_batch_role(actor)
        if sequence_ids is None:
            sequence_ids = [
                entity["id"]
                for entity in self.repository.list_entities(kind="sequence")
            ]
        batch_id = str(uuid4())
        self.repository.create_recompute_batch(batch_id, sequence_ids, actor.user_id)
        return self._run_batch(actor, batch_id)

    def retry_recompute_batch(self, actor, batch_id):
        self._ensure_batch_role(actor)
        batch = self.repository.get_recompute_batch(batch_id)
        if not batch:
            raise NotFoundError("recompute batch not found: " + batch_id)
        return self._run_batch(actor, batch_id, include_failed=True)

    @staticmethod
    def _ensure_batch_role(actor):
        if actor.role not in ("admin", "analyst", "reviewer"):
            raise PermissionDenied("role %s may not recompute sequences" % actor.role)

    def _run_batch(self, actor, batch_id, include_failed=False):
        for sequence_id in self.repository.list_pending_batch_items(
            batch_id, only_pending=not include_failed
        ):
            sequence = self.repository.get_entity(sequence_id)
            if not sequence:
                self.repository.mark_batch_item(batch_id, sequence_id, "failed", "sequence not found")
                continue
            try:
                self._recompute_persist(actor, sequence, trigger="batch_recompute")
            except Exception as exc:  # 单个失败不拖垮整批，留给后续重试
                self.repository.mark_batch_item(batch_id, sequence_id, "failed", str(exc))
            else:
                self.repository.mark_batch_item(batch_id, sequence_id, "done")
        return self.repository.get_recompute_batch(batch_id)

    def get_recompute_batch(self, batch_id):
        batch = self.repository.get_recompute_batch(batch_id)
        if not batch:
            raise NotFoundError("recompute batch not found: " + batch_id)
        return batch

    # ----- 旧数据升级回填：按主震窗口贪心聚簇 -----
    def backfill_sequences(self, actor, window_days=None, window_distance_km=None):
        if actor.role != "admin":
            raise PermissionDenied("only admin may backfill sequence membership")
        from .sequences import (
            event_coordinates,
            event_magnitude,
            event_origin,
        )

        events = self.repository.list_entities(kind="event")
        assigned = set()
        for event in events:
            if self.repository.list_member_sequences(event["id"]):
                assigned.add(event["id"])
        eligible = []
        skipped = []
        for event in events:
            if event["id"] in assigned:
                continue
            if event_origin(event) is None or event_coordinates(event) is None or event_magnitude(event) is None:
                skipped.append(event["id"])
                continue
            eligible.append(event)
        eligible.sort(key=lambda item: (event_origin(item), item["id"]))

        overrides = {}
        if window_days is not None:
            overrides["window_days"] = float(window_days)
        if window_distance_km is not None:
            overrides["window_distance_km"] = float(window_distance_km)

        clusters = []
        for event in eligible:
            for cluster in clusters:
                probe_data = dict(overrides)
                _, _, members = compute_membership(
                    probe_data, cluster + [event], [item["id"] for item in cluster] + [event["id"]]
                )
                if event["id"] in members:
                    cluster.append(event)
                    break
            else:
                clusters.append([event])

        created = []
        for index, cluster in enumerate(clusters, start=1):
            if len(cluster) < 2:
                continue
            sequence_id = str(uuid4())
            data = {
                "name": "backfill-%s-%d" % (cluster[0]["id"], index),
                "anchor_event_ids": [event["id"] for event in cluster],
                "backfilled": True,
            }
            data.update(overrides)
            entity = self.repository.create_entity(
                sequence_id, "sequence", "draft", data, actor.user_id
            )
            self.audit.record(sequence_id, actor, "create", None, "draft",
                              {"kind": "sequence", "backfill": True})
            entity, _ = self._recompute_persist(
                actor, entity, trigger="backfill", expected_version=1, bump_version=False
            )
            created.append(entity["id"])

        return {
            "created_sequence_ids": created,
            "cluster_count": len(clusters),
            "assigned_event_count": len([event for event in eligible if any(event["id"] in c for c in clusters if len(c) >= 2)]),
            "skipped_event_ids": skipped,
        }

    def list_notices(self, sequence_id=None):
        notices = self.repository.list_entities(kind="sequence_notice")
        if sequence_id:
            notices = [
                notice
                for notice in notices
                if notice["data"].get("sequence_id") == sequence_id
            ]
        return notices

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) == "sequence":
            entity = dict(entity)
            entity["data"] = dict(entity["data"])
            entity["data"]["member_event_ids"] = self.repository.list_sequence_members(entity["id"])
            entity["data"]["announcement_versions"] = [
                notice["data"].get("version")
                for notice in self.repository.list_entities(kind="sequence_notice")
                if notice["data"].get("sequence_id") == entity["id"]
            ]
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
