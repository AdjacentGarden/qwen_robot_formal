from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SkillRegistry:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.spec_dir = Path(config["paths"]["skill_spec_dir"])
        self.specs = self._load_specs()

    def _load_specs(self) -> dict[str, dict[str, Any]]:
        specs: dict[str, dict[str, Any]] = {}
        if not self.spec_dir.exists():
            return specs
        for path in sorted(self.spec_dir.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = spec.get("name") or path.stem
            specs[name] = spec
        return specs

    def get(self, skill_name: str) -> dict[str, Any] | None:
        return self.specs.get(skill_name)

    def names(self) -> list[str]:
        return sorted(self.specs)

    def disabled_names(self) -> set[str]:
        configured = set(self.config.get("execution", {}).get("disabled_skills", []))
        for name, spec in self.specs.items():
            tags = set(spec.get("domain_tags") or [])
            side_effects = set(spec.get("side_effects") or [])
            if "disabled" in tags or "disabled" in side_effects or spec.get("disabled_reason"):
                configured.add(name)
        return configured

    def should_speak_start_ack(self, skill_name: str) -> bool:
        spec = self.get(skill_name) or {}
        contract = spec.get("speech_contract")
        if not isinstance(contract, dict):
            return True
        return contract.get("speak_start_ack") is not False

    def compact_specs_for_prompt(self) -> list[dict[str, Any]]:
        """Return capability semantics for the model, never a phrase-matching table."""
        items = []
        for name in self.names():
            spec = self.specs[name]
            requirements = spec.get("implementation_requirements")
            compact_requirements = {}
            if isinstance(requirements, dict):
                compact_requirements = {
                    str(key): value
                    for key, value in list(requirements.items())[:8]
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            when_to_call = [
                str(item)[:120]
                for item in spec.get("when_to_call_zh") or []
                if str(item).strip()
            ][:3]
            query_actions = [
                str(item.get("action") or "")
                for item in spec.get("query_intents") or []
                if isinstance(item, dict) and str(item.get("action") or "")
            ]
            items.append(
                {
                    "name": name,
                    "description_zh": str(spec.get("description_zh", ""))[:320],
                    "domain_tags": list(spec.get("domain_tags") or [])[:6],
                    "when_to_call_zh": when_to_call,
                    "required_slots": spec.get("required_slots", []),
                    "optional_slots": list(spec.get("optional_slots") or [])[:10],
                    "allowed_actions": spec.get("allowed_actions", []),
                    "side_effects": list(spec.get("side_effects") or [])[:6],
                    "implementation_constraints": compact_requirements,
                    "query_actions": query_actions,
                }
            )
        return items

    def skills_for_domain(self, domain: str) -> list[dict[str, Any]]:
        result = []
        for spec in self.specs.values():
            if domain in (spec.get("domain_tags") or []):
                result.append(spec)
        return result

    def validate_step(self, skill_name: str) -> tuple[bool, str]:
        if skill_name not in self.specs:
            return False, f"unknown skill: {skill_name}"
        if skill_name in self.disabled_names():
            return False, f"disabled skill: {skill_name}"
        return True, ""

    def sanitize_model_arguments(self, skill_name: str, arguments: Any) -> tuple[dict[str, Any], list[str]]:
        """Keep only arguments declared by a skill's parameter contract."""
        supplied = dict(arguments) if isinstance(arguments, dict) else {}
        spec = self.get(skill_name) or {}
        parameters = spec.get("parameters")
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if not isinstance(properties, dict) or not properties:
            return supplied, []
        allowed = {str(name) for name in properties}
        sanitized = {key: value for key, value in supplied.items() if str(key) in allowed}
        removed = sorted(str(key) for key in supplied if str(key) not in allowed)
        return sanitized, removed
