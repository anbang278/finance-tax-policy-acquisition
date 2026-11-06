"""Business source scope is separate from machine scheduling settings."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ftr.config import SchedulerSettings

LEGACY_SOURCE_IDS = ("mof", "chinatax")


class ScheduleScope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source_ids: list[str] = Field(min_length=1)

    @field_validator("source_ids")
    @classmethod
    def structural_ids(cls, values):
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value) for value in values
        ):
            raise ValueError("计划来源须为非重复的登记ID")
        return values


class SchedulePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["2"] = "2"
    settings: SchedulerSettings
    scope: ScheduleScope


def legacy_scope() -> ScheduleScope:
    return ScheduleScope(source_ids=list(LEGACY_SOURCE_IDS))


def parse_plan_payload(
    payload: dict, existing_scope: ScheduleScope | None = None, *, validate_registered: bool = True
) -> SchedulePlan:
    """Old settings edits retain scope; only a new envelope can change it."""
    if set(payload) & {"schema_version", "settings", "scope"}:
        plan = SchedulePlan.model_validate(payload)
        if validate_registered:
            from ftr.sources import validate_source_ids

            validate_source_ids(plan.scope.source_ids)
        return plan
    return SchedulePlan(
        settings=SchedulerSettings.model_validate(payload),
        scope=existing_scope or legacy_scope(),
    )


def stored_scope(row) -> tuple[ScheduleScope, dict]:
    """Reading saved history never substitutes the current source catalog."""
    columns = set(row.keys()) if row else set()
    value = json.loads(row["scope_json"]) if "scope_json" in columns and row["scope_json"] else {}
    scope = ScheduleScope(source_ids=value["source_ids"]) if value else legacy_scope()
    return scope, value.get("source_snapshot", {})


def merge_pending(previous: dict | None, due: dict) -> dict:
    if not previous:
        return dict(due)
    return {
        **due,
        "date_from": min(previous["date_from"], due["date_from"]),
        "date_to": max(previous["date_to"], due["date_to"]),
        # A later plan/registry edit must not rewrite already authorized work.
        "source_snapshot": previous.get("source_snapshot") or due.get("source_snapshot", {}),
        "plan_revision": previous.get("plan_revision", due.get("plan_revision", 0)),
    }
