#!/usr/bin/env python3
"""Deterministic evidence-deduction scoring shared by both pipeline stages."""

from __future__ import annotations

import math
import re


def normalize_figures(raw: object, allowed: list[str], requirements: list[str], evidence: list, visual: list) -> list[dict]:
    """Distinct original-page screenshot locators, not missing-element counts."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("A figure-counted deduction needs a non-empty figures list")
    page_pattern = re.compile(r"(?:\bp\.?\s*|第\s*)(\d+)", re.IGNORECASE)
    def pages(entries):
        return {int(n) for entry in entries for n in page_pattern.findall(str(entry))}

    evidence_pages, visual_pages = pages(evidence), pages(visual)
    seen = set()
    affected = set()
    result = []
    for figure in raw:
        if not isinstance(figure, dict):
            raise ValueError("Each figure must record id, requirements and reason")
        locator = str(figure.get("id", ""))
        match = re.fullmatch(r"p\.([1-9][0-9]*)#image([1-9][0-9]*)", locator)
        if not match or locator in seen:
            raise ValueError("Figure ids must be distinct canonical locators p.N#imageK")
        page = int(match[1])
        if page not in evidence_pages or page not in visual_pages:
            raise ValueError(f"Figure {locator!r} needs its own evidence and visually inspected page")
        missing = figure.get("requirements")
        if not isinstance(missing, list) or not missing or any(
            not isinstance(value, str) or value not in allowed for value in missing
        ):
            raise ValueError(f"Figure {locator!r} needs requirements from the printed rule")
        reason = str(figure.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"Figure {locator!r} needs a specific reason")
        missing = sorted(set(missing))
        affected.update(missing)
        seen.add(locator)
        result.append({"id": locator, "requirements": missing, "reason": reason})
    if affected != set(requirements):
        raise ValueError("Deduction requirements must equal the union of its figures' requirements")
    return sorted(result, key=lambda figure: tuple(map(int, re.findall(r"\d+", figure["id"]))))


def uses_deduction_policy(rubric_items: list[dict]) -> bool:
    return bool(rubric_items) and all(item.get("deduction_rules") for item in rubric_items)


def calculate_deduction_scores(decision: dict, rubric_items: list[dict]) -> tuple[list[float], list[dict]]:
    """Validate a deduction ledger and return program-calculated item scores.

    A fact id may appear only once across the report, which prevents one omission from being
    charged again under completeness, content and presentation. Deductions larger than two
    points require confirmed evidence; uncertain observations are therefore capped at the
    ordinary range configured by the rubric.
    """
    if not uses_deduction_policy(rubric_items):
        raise ValueError("The rubric does not define a complete deduction policy")
    deductions = decision.get("deductions", [])
    if not isinstance(deductions, list):
        raise ValueError("deductions/d must be a list")
    policy = rubric_items[0].get("report_deduction_policy", {})

    totals = [0.0 for _ in rubric_items]
    counts: list[dict[str, int]] = [{} for _ in rubric_items]
    group_totals: list[dict[str, float]] = [{} for _ in rubric_items]
    facts: set[str] = set()
    normalized: list[dict] = []
    minor_total = 0.0
    minor_items: set[int] = set()

    for position, raw in enumerate(deductions, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"deduction {position} must be an object")
        try:
            item_number = int(raw.get("item"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"deduction {position} needs a numeric item") from error
        if not 1 <= item_number <= len(rubric_items):
            raise ValueError(f"deduction {position} item is outside the rubric")
        item_index = item_number - 1
        item = rubric_items[item_index]
        rules = {str(rule["id"]): rule for rule in item.get("deduction_rules", [])}
        rule_id = str(raw.get("rule", "")).strip()
        if rule_id not in rules:
            raise ValueError(f"deduction {position} has invalid rule {rule_id!r} for item {item_number}")
        rule = rules[rule_id]
        if rule.get("only_if_no_other_deductions") and len(deductions) != 1:
            raise ValueError(
                f"deduction rule {rule_id!r} applies only if no other deductions exist "
                "anywhere in the report; omit this conditional rule when another rule applies"
            )
        fact = str(raw.get("fact", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        certainty = str(raw.get("certainty", "confirmed")).strip().lower()
        evidence = raw.get("evidence", raw.get("e", []))
        visual = raw.get("visual_evidence", raw.get("v", []))
        if not fact or not reason:
            raise ValueError(f"deduction {position} needs fact and reason")
        if policy.get("require_rule_fact_ids") and fact != rule_id:
            raise ValueError(f"deduction {position} fact must equal its canonical rule id")
        if fact in facts:
            raise ValueError(f"deduction fact {fact!r} is charged more than once")
        if certainty not in {"confirmed", "uncertain"}:
            raise ValueError(f"deduction {position} certainty must be confirmed or uncertain")
        if policy.get("require_confirmed_deductions") and (
            certainty != "confirmed" or raw.get("certainty") != "confirmed"
        ):
            raise ValueError(f"deduction {position} requires explicit confirmed evidence")
        if not isinstance(evidence, list) or not any(str(value).strip() for value in evidence):
            raise ValueError(f"deduction {position} needs at least one evidence page")
        if not isinstance(visual, list):
            raise ValueError(f"deduction {position} visual_evidence must be a list")
        points = float(rule["points"])
        if bool(rule.get("requires_visual_evidence", False)) and not any(
            str(value).strip() for value in visual
        ):
            raise ValueError(f"deduction {position} rule {rule_id!r} requires visual evidence")
        allowed_requirements = rule.get("requirement_ids", [])
        requirements = raw.get("requirements", [])
        if not isinstance(requirements, list) or any(
            not isinstance(value, str) or not value.strip() for value in requirements
        ):
            raise ValueError(f"deduction {position} requirements must be a list of non-empty strings")
        requirements = sorted(set(value.strip() for value in requirements))
        if (allowed_requirements and not requirements) or set(requirements) - set(allowed_requirements):
            raise ValueError(f"deduction {position} needs requirements from its printed requirement_ids")
        figures = []
        if rule.get("count_by") == "figure":
            figures = normalize_figures(raw.get("figures"), allowed_requirements, requirements, evidence, visual)
            # Only charged screenshots enter the normalized fact ledger and blind comparison.
            # Extra observations beyond the cap must not create a spurious same-score dispute.
            figures = figures[:int(rule.get("max_occurrences", 1))]
            requirements = sorted({value for figure in figures for value in figure["requirements"]})
            points *= len(figures)
        elif "figures" in raw:
            raise ValueError("figures may only appear on a figure-counted deduction")
        if points > 2 and certainty != "confirmed":
            raise ValueError(f"deduction {position} exceeds 2 points without confirmed evidence")

        for selected_rule_id in counts[item_index]:
            if selected_rule_id == rule_id:
                continue
            selected_rule = rules[selected_rule_id]
            if (
                selected_rule_id in rule.get("exclusive_with", [])
                or rule_id in selected_rule.get("exclusive_with", [])
            ):
                raise ValueError(
                    f"deduction rules {selected_rule_id!r} and {rule_id!r} are mutually exclusive "
                    f"for item {item_number}"
                )

        used = counts[item_index].get(rule_id, 0) + 1
        maximum = int(rule.get("max_occurrences", 1))
        if figures and used > 1:
            raise ValueError("A figure-counted rule must use one aggregated deduction entry")
        if used > maximum:
            raise ValueError(
                f"deduction rule {rule_id!r} exceeds its {maximum}-occurrence cap for item {item_number}"
            )
        counts[item_index][rule_id] = used
        group = str(rule.get("group", "")).strip()
        if group:
            next_total = group_totals[item_index].get(group, 0.0) + points
            cap = float((item.get("deduction_group_caps") or {}).get(group, math.inf))
            if next_total > cap + 1e-9:
                raise ValueError(
                    f"deduction group {group!r} exceeds its {cap:g}-point cap for item {item_number}"
                )
            group_totals[item_index][group] = next_total
            if group == "minor":
                minor_total += points
                minor_items.add(item_number)

        facts.add(fact)
        totals[item_index] += points
        normalized.append({
            "item": item_number,
            "rule": rule_id,
            "fact": fact,
            "reason": reason,
            "certainty": certainty,
            "points": int(points) if points.is_integer() else points,
            "evidence": evidence,
            "visual_evidence": visual,
            **({"requirements": requirements} if requirements else {}),
            **({"figures": figures} if figures else {}),
        })

    policy = rubric_items[0].get("report_deduction_policy", {}) if rubric_items else {}
    minimum_minor = float(policy.get("minimum_minor_deduction", 0) or 0)
    minimum_total = float(policy.get("minimum_report_deduction", 0) or 0)
    maximum_minor_items = int(policy.get("maximum_minor_items", 0) or 0)
    total_deduction = sum(totals)
    if minor_total + 1e-9 < minimum_minor:
        raise ValueError(
            f"deduction ledger needs at least {minimum_minor:g} ordinary points under the configured rubric"
        )
    if total_deduction + 1e-9 < minimum_total:
        raise ValueError(
            f"deduction ledger needs at least {minimum_total:g} total points; unsupported full or near-full scores are not allowed"
        )
    if maximum_minor_items and len(minor_items) > maximum_minor_items:
        raise ValueError(
            f"ordinary deductions may be concentrated in at most {maximum_minor_items} rubric items"
        )

    scores = [max(0.0, float(item["max_score"]) - totals[index]) for index, item in enumerate(rubric_items)]
    supplied = decision.get("scores")
    if supplied not in (None, []):
        if not isinstance(supplied, list) or len(supplied) != len(scores):
            raise ValueError(f"scores must contain {len(scores)} items")
        try:
            numeric = [float(value) for value in supplied]
        except (TypeError, ValueError) as error:
            raise ValueError("scores must be numeric") from error
        if any(abs(left - right) > 1e-9 for left, right in zip(numeric, scores)):
            raise ValueError("supplied scores do not equal the program-calculated deduction scores")
    return scores, normalized


def minimum_offset_for_average(
    totals: list[float], maximum: float, target: float, increment: float = 1.0
) -> float:
    """Return the smallest non-negative increment-aligned offset reaching the capped mean."""
    if not all(math.isfinite(float(value)) for value in [maximum, target, increment]):
        raise ValueError("average-floor settings must be finite")
    if maximum <= 0 or increment <= 0:
        raise ValueError("maximum and score increment must be positive")
    if not totals or target <= 0:
        return 0.0
    if not all(math.isfinite(float(total)) for total in totals):
        raise ValueError("raw totals for the average floor must be finite")
    if target > maximum:
        raise ValueError("minimum published average cannot exceed the rubric maximum")
    offset = 0.0
    limit = int(math.ceil(maximum / increment)) + 1
    for step in range(limit):
        offset = step * increment
        mean = sum(min(maximum, float(total) + offset) for total in totals) / len(totals)
        if mean + 1e-9 >= target:
            return offset
    raise ValueError("cannot reach the configured minimum average")


def score_distribution(totals: list[float], target: dict) -> dict:
    """Audit a strict > fraction target; never alter scores to meet it."""
    lower, upper = float(target["lower"]), float(target["upper"])
    fraction = float(target["minimum_fraction"])
    if not all(math.isfinite(float(value)) for value in [lower, upper, fraction, *totals]):
        raise ValueError("Distribution values must be finite")
    count = len(totals)
    inside = sum(lower <= float(value) <= upper for value in totals)
    required = math.floor(fraction * count) + 1 if count else 0
    return {
        "lower": lower, "upper": upper, "minimum_fraction": fraction,
        "comparison": ">", "count": count, "in_range": inside,
        "below_range": sum(float(value) < lower for value in totals),
        "above_range": sum(float(value) > upper for value in totals),
        "fraction": inside / count if count else None,
        "minimum_required_count": required,
        "target_met": inside >= required if count else None,
    }
