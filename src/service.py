from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text, require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CALIBRATION_ENTITY, CALIBRATION_ROLES,
                    CREATE_ROLES, ENTITY, GATED_TRANSITIONS, RECORD_ROLES,
                    TERMINAL_STATES, TITLE, VIEW_ROLES, calibration_blockers,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        point_ref = require_text(payload.get("point_ref"), "point_ref", 100)
        device_id = require_text(payload.get("device_id"), "device_id", 100)
        calibration = self.repository.get_active_calibration(point_ref)
        blockers = calibration_blockers(calibration, device_id, utc_now())
        if blockers:
            raise ConflictError("；".join(blockers))
        factor = float(calibration["factor"])
        corrected = round(quantity * factor, 6)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, point_ref,
                                           device_id, factor, corrected,
                                           calibration["id"])
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "point_ref": point_ref, "device_id": device_id,
            "calibration_id": calibration["id"], "factor": factor,
            "corrected_quantity": corrected,
            "priority": priority_score(severity, corrected, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target in GATED_TRANSITIONS:
            calibration = (self.repository.get_active_calibration(item["point_ref"])
                           if item.get("point_ref") else None)
            blockers = blockers + calibration_blockers(
                calibration, item.get("device_id"), utc_now())
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "calibration_id": item.get("calibration_id"),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def register_calibration(self, payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, CALIBRATION_ROLES)
        actor = require_text(actor, "actor", 100)
        point_ref = require_text(payload.get("point_ref"), "point_ref", 100)
        device_id = require_text(payload.get("device_id"), "device_id", 100)
        calibrated_at = require_timestamp(payload.get("calibrated_at"), "calibrated_at")
        valid_until = require_timestamp(payload.get("valid_until"), "valid_until")
        if valid_until <= calibrated_at:
            raise ValueError("有效期必须晚于校准时间")
        factor = require_number(payload.get("factor"), "factor")
        if factor <= 0:
            raise ValueError("修正系数必须大于0")
        replace = bool(payload.get("replace", False))
        calibration = self.repository.create_calibration(
            point_ref, device_id, calibrated_at, factor, valid_until, replace, actor)
        self.repository.append_audit("calibration_register", CALIBRATION_ENTITY,
                                     calibration["id"], actor, {
            "point_ref": point_ref, "device_id": device_id, "factor": factor,
            "calibrated_at": calibrated_at, "valid_until": valid_until,
            "replace": replace,
        })
        return calibration

    def recalculate_point(self, point_ref: Any, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CALIBRATION_ROLES)
        actor = require_text(actor, "actor", 100)
        point_ref = require_text(point_ref, "point_ref", 100)
        device_id = payload.get("device_id")
        if device_id is not None:
            device_id = require_text(device_id, "device_id", 100)
        calibration = self.repository.get_active_calibration(point_ref)
        now = utc_now()
        base_blockers = calibration_blockers(calibration, None, now)
        if base_blockers:
            raise ConflictError("；".join(base_blockers))
        updated, blocked, skipped = [], [], []
        for item in self.repository.list_items_by_point(point_ref):
            if item["status"] in TERMINAL_STATES:
                skipped.append(item["id"])
                continue
            target_device = device_id if device_id is not None else item.get("device_id")
            reasons = calibration_blockers(calibration, target_device, now)
            if reasons:
                blocked.append({"item_id": item["id"], "reasons": reasons})
                continue
            corrected = round(item["quantity"] * float(calibration["factor"]), 6)
            self.repository.update_item_calibration(
                item["id"], target_device, float(calibration["factor"]), corrected,
                calibration["id"])
            self.repository.append_audit("recalculate", ENTITY, item["id"], actor, {
                "point_ref": point_ref, "calibration_id": calibration["id"],
                "device_id": target_device,
                "old_factor": item.get("factor"), "new_factor": calibration["factor"],
                "old_corrected": item.get("corrected_quantity"),
                "new_corrected": corrected,
            })
            updated.append(item["id"])
        return {
            "point_ref": point_ref, "calibration_id": calibration["id"],
            "factor": calibration["factor"], "device_id": calibration["device_id"],
            "updated": updated, "blocked": blocked, "skipped_terminal": skipped,
        }

    def list_calibrations(self, role: str, point_ref: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_calibrations(point_ref)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        effective = item.get("corrected_quantity")
        if effective is None:
            effective = item["quantity"]
        result["effective_quantity"] = effective
        result["priority"] = priority_score(
            item["severity"], effective, item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], effective, item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], effective, item["threshold"])
        calibration = (self.repository.get_active_calibration(item["point_ref"])
                       if item.get("point_ref") else None)
        result["calibration"] = calibration
        result["calibration_blockers"] = calibration_blockers(
            calibration, item.get("device_id"), utc_now())
        return result
