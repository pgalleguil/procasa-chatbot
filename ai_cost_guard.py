"""Per-run DeepSeek budget guard. No network calls are made by this module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(value: Any) -> int:
    """Conservative token estimate used before a provider call."""
    text = str(value or "")
    return max(1, (len(text) + 3) // 4) if text else 0


@dataclass(slots=True)
class AIBudget:
    max_calls: int = 50
    max_input_tokens: int = 100_000
    max_output_tokens: int = 20_000
    max_estimated_cost: float = 5.0
    input_cost_per_million: float = 0.14
    output_cost_per_million: float = 0.28


@dataclass(slots=True)
class AICostGuard:
    budget: AIBudget = field(default_factory=AIBudget)
    calls_attempted: int = 0
    calls_executed: int = 0
    cache_hits: int = 0
    skipped_structural: int = 0
    skipped_identity: int = 0
    skipped_text_rule: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    budget_exceeded: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (max(0, input_tokens) / 1_000_000) * self.budget.input_cost_per_million
            + (max(0, output_tokens) / 1_000_000) * self.budget.output_cost_per_million,
            8,
        )

    def check_budget(self, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        projected_calls = self.calls_executed + 1
        projected_input = self.input_tokens + max(0, int(input_tokens))
        projected_output = self.output_tokens + max(0, int(output_tokens))
        projected_cost = self.estimated_cost + self.estimate_cost(input_tokens, output_tokens)
        reasons: list[str] = []
        if self.budget.max_calls >= 0 and projected_calls > self.budget.max_calls:
            reasons.append("MAX_AI_CALLS_PER_RUN")
        if self.budget.max_input_tokens >= 0 and projected_input > self.budget.max_input_tokens:
            reasons.append("MAX_INPUT_TOKENS_PER_RUN")
        if self.budget.max_output_tokens >= 0 and projected_output > self.budget.max_output_tokens:
            reasons.append("MAX_OUTPUT_TOKENS_PER_RUN")
        if self.budget.max_estimated_cost >= 0 and projected_cost > self.budget.max_estimated_cost:
            reasons.append("MAX_ESTIMATED_COST_PER_RUN")
        allowed = not reasons
        if not allowed:
            self.budget_exceeded = True
        decision = {
            "allowed": allowed,
            "reasons": reasons,
            "projected_calls": projected_calls,
            "projected_input_tokens": projected_input,
            "projected_output_tokens": projected_output,
            "projected_cost": round(projected_cost, 8),
        }
        self.events.append({"event": "budget_check", **decision})
        return decision

    def mark_attempted(self) -> None:
        self.calls_attempted += 1

    def mark_executed(self, input_tokens: int, output_tokens: int = 0) -> None:
        self.calls_executed += 1
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        self._recalculate_cost()

    def reconcile_attempt_usage(self, *, reserved_input_tokens: int, actual_input_tokens: int, actual_output_tokens: int) -> None:
        """Replace a preflight estimate with provider usage for one call."""
        self.input_tokens = max(0, self.input_tokens - max(0, int(reserved_input_tokens)) + max(0, int(actual_input_tokens)))
        self.output_tokens += max(0, int(actual_output_tokens))
        self._recalculate_cost()
        reasons = []
        if self.budget.max_input_tokens >= 0 and self.input_tokens > self.budget.max_input_tokens:
            reasons.append("MAX_INPUT_TOKENS_PER_RUN")
        if self.budget.max_output_tokens >= 0 and self.output_tokens > self.budget.max_output_tokens:
            reasons.append("MAX_OUTPUT_TOKENS_PER_RUN")
        if self.budget.max_estimated_cost >= 0 and self.estimated_cost > self.budget.max_estimated_cost:
            reasons.append("MAX_ESTIMATED_COST_PER_RUN")
        if reasons:
            self.budget_exceeded = True
        self.events.append({"event": "provider_usage_recorded", "input_tokens": max(0, int(actual_input_tokens)), "output_tokens": max(0, int(actual_output_tokens)), "budget_exceeded": self.budget_exceeded, "reasons": reasons})

    def _recalculate_cost(self) -> None:
        self.estimated_cost = self.estimate_cost(self.input_tokens, self.output_tokens)

    def report(self) -> dict[str, Any]:
        return {
            "AI_CALLS_ATTEMPTED": self.calls_attempted,
            "AI_CALLS_EXECUTED": self.calls_executed,
            "AI_CACHE_HITS": self.cache_hits,
            "AI_SKIPPED_STRUCTURAL": self.skipped_structural,
            "AI_SKIPPED_IDENTITY": self.skipped_identity,
            "AI_SKIPPED_TEXT_RULE": self.skipped_text_rule,
            "AI_INPUT_TOKENS": self.input_tokens,
            "AI_OUTPUT_TOKENS": self.output_tokens,
            "AI_ESTIMATED_COST": self.estimated_cost,
            "AI_BUDGET_EXCEEDED": self.budget_exceeded,
            "AI_CALL_RATE": (
                self.calls_executed / self.calls_attempted
                if self.calls_attempted else 0.0
            ),
        }


def budget_from_config(config: Any) -> AIBudget:
    return AIBudget(
        max_calls=int(getattr(config, "max_ai_calls_per_run", 50)),
        max_input_tokens=int(getattr(config, "max_input_tokens_per_run", 100000)),
        max_output_tokens=int(getattr(config, "max_output_tokens_per_run", 20000)),
        max_estimated_cost=float(getattr(config, "max_estimated_cost_per_run", 5.0)),
        input_cost_per_million=float(getattr(config, "estimated_input_cost_per_million", 0.14)),
        output_cost_per_million=float(getattr(config, "estimated_output_cost_per_million", 0.28)),
    )
