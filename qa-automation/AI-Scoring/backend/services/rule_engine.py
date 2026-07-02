"""Pure formula rule engine — Wave 2 Phase 2a.

Table-driven scorer for the Ops-signed formula shape (backend/models/formula.py).
One engine, both teams: everything team-specific lives in the `Formula` —
rules, `evaluation_order`, and the per-formula `normalization.na` policy
(member_support_scoring_migration.md §4-§6, sales_scoring_migration.md §4-§6).

NO DB access, NO clock, NO randomness. `qa.compute_overall_score()` (Phase 2b)
is the DB-touching wrapper that loads the archived formula/rubric and calls
`evaluate_formula()`.

Semantics locked here (each traces to a spec line):

- **Normalization** (MS §4 / Sales §4): rating → linear curve; binary → Y/N
  map (per-section `binary_map` overrides the formula-level one). NA depends
  on `normalization.na`:
    * `full_credit` — NA scores frac 1.0 on its own slot; weights never move
      (Sales §4: "NA behaves exactly like a perfect score").
    * `redistribute_per_rules` — the section goes *inactive* (frac None,
      contributes 0) and its weight must be moved off by a rule. Any inactive
      section still holding weight at `weighted_sum` time raises
      `UnhandledNaWeightError` — MS §4: "never silently lost".
- **Evaluation order** (MS §6): tokens run exactly as listed. Weight-moving
  rules operate on *current* effective weights, so transfer→shift ordering
  compounds deliberately (caller_id_na_transfer feeds frequent_caller_shift).
  Static misconfigurations fail before any data is read
  (`FormulaOrderError`): a weight-moving rule scheduled after `weighted_sum`,
  a `score_scale` scheduled before it, or a duplicated `weighted_sum` token.
- **`when` conditions**: `equals` compares against the raw answer rendered as
  a string ("Y"/"N"/"NA"/"1".."5"); `frac_lte`/`frac_gte` compare against the
  section's *normalized* frac — raw ratings, pre-redistribution, so weight
  moves never change whether a call escalates (MS §6 step 7). Conditions on
  one clause AND together. An inactive (NA) section has no frac: numeric
  conditions are False, only `equals: "NA"` can match it. Signals default
  False when absent (frequent_caller is false-by-default until Command
  Center ships — Wave 3).
- **score_override** (MS §5 hard_zero): when fired, the final score IS
  `effect.set_score`. Evaluation continues so the trace, raw score, and flag
  events stay observable, but later `score_scale` steps do not touch the
  overridden value.
- **Answers are strict**: every formula section must be answered, unknown
  keys raise, NA is only legal where the section's score_type allows it.
  Key-mismatch bugs (the Wave2Plan §9 remapping risk) must fail loudly, not
  score wrong.

Answer encoding: ratings are ints (1-5), binary is "Y"/"N", not-applicable is
"NA". Sales' 0/1 labels are presentation only — callers map them to N/Y
before invoking (Sales §4: "identical to the shared binary_yn normalization").
"""

from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from backend.models.formula import (
    WEIGHTED_SUM,
    FlagRule,
    Formula,
    FormulaSection,
    NaRedistributionRule,
    Rule,
    RuleWhen,
    RuleWhenAll,
    RuleWhenAny,
    RuleWhenSection,
    RuleWhenSignal,
    ScoreOverrideRule,
    ScoreScaleRule,
    WeightRedistributionRule,
    WeightTransferRule,
)

SectionAnswer = Union[int, str]
"""int 1-5 for ratings; "Y"/"N" for binary; "NA" where the section allows it."""

_EPS = 1e-9

# score_type → (kind, na_allowed). manual_yn is analyst-filled Y/N whose
# default state is NA; auto_value is a hardcoded Y — both normalize as binary.
_SCORE_TYPE_TRAITS: dict[str, tuple[str, bool]] = {
    "rating_1_5": ("rating", False),
    "rating_1_5_na": ("rating", True),
    "binary_yn": ("binary", False),
    "binary_yn_na": ("binary", True),
    "manual_yn": ("binary", True),
    "auto_value": ("binary", False),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RuleEngineError(ValueError):
    """Base for every deterministic engine failure."""


class FormulaOrderError(RuleEngineError):
    """evaluation_order is statically misconfigured (checked before any data)."""


class AnswerValidationError(RuleEngineError):
    """Answers don't match the formula's sections/score types."""


class UnhandledNaWeightError(RuleEngineError):
    """Under redistribute_per_rules, an NA section's weight was never moved
    off by a rule — MS §4 forbids silently losing it."""

    def __init__(self, formula_id: str, stranded: dict[str, float]) -> None:
        self.stranded = stranded
        super().__init__(
            f"formula {formula_id!r}: NA sections still hold weight at "
            f"weighted_sum time: {stranded} — no rule redistributed them"
        )


class RuleApplicationError(RuleEngineError):
    """A rule fired but its effect can't be applied deterministically."""


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

class EmittedEvent(BaseModel):
    """One flag-rule emission (MS §5 escalation_flag). Score-neutral."""
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    event: str
    route: Optional[str] = None


class RuleTrace(BaseModel):
    """Per-token evaluation record — the audit trail Phase 2c parity checks
    read when a golden fixture disagrees."""
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    rule_type: str
    fired: bool
    note: Optional[str] = None


class ScoreResult(BaseModel):
    """Everything evaluate_formula() decided, not just the number."""
    model_config = ConfigDict(extra="forbid")

    formula_id: str
    rubric_version: str
    final_score: float
    raw_score: float
    """Σ(weight × frac) before post-sum scaling and before any override."""
    overridden: bool = False
    effective_weights: dict[str, float]
    """Weights after all weight-moving rules — MS's derived "automated"
    weights land here (computed, not stored — MS §9)."""
    fracs: dict[str, Optional[float]]
    """Normalized frac per section; None = inactive (NA under redistribute)."""
    contributions: dict[str, float]
    na_sections: list[str] = Field(default_factory=list)
    events: list[EmittedEvent] = Field(default_factory=list)
    trace: list[RuleTrace] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def evaluate_formula(
    formula: Formula,
    answers: dict[str, SectionAnswer],
    signals: Optional[dict[str, bool]] = None,
) -> ScoreResult:
    """Score one evaluation against an Ops-signed formula. Pure function."""
    signals = signals or {}
    _check_static_order(formula)

    fracs, na_sections = _normalize_answers(formula, answers)
    weights = {s.key: s.weight for s in formula.sections}

    rules_by_id = {r.id: r for r in formula.rules}
    trace: list[RuleTrace] = []
    events: list[EmittedEvent] = []
    override_score: Optional[float] = None
    raw_score: Optional[float] = None
    running_score: Optional[float] = None

    for token in formula.evaluation_order:
        if token == WEIGHTED_SUM:
            _assert_na_weight_handled(formula, weights, fracs)
            raw_score = sum(
                weights[key] * frac
                for key, frac in fracs.items()
                if frac is not None
            )
            running_score = raw_score
            continue

        rule = rules_by_id[token]
        if not rule.enabled:
            trace.append(RuleTrace(rule_id=rule.id, rule_type=rule.type, fired=False, note="disabled"))
            continue

        fired = _when_matches(rule.when, answers, fracs, signals)
        if not fired:
            trace.append(RuleTrace(rule_id=rule.id, rule_type=rule.type, fired=False))
            continue

        note: Optional[str] = None
        if isinstance(rule, ScoreOverrideRule):
            override_score = rule.effect.set_score
            note = f"final score overridden to {override_score}"
        elif isinstance(rule, WeightTransferRule):
            note = _apply_weight_transfer(rule, weights)
        elif isinstance(rule, WeightRedistributionRule):
            note = _apply_weight_redistribution(rule, weights, fracs)
        elif isinstance(rule, NaRedistributionRule):
            note = _apply_na_redistribution(rule, weights, fracs)
        elif isinstance(rule, ScoreScaleRule):
            if override_score is not None:
                note = "skipped: score already overridden"
            else:
                assert running_score is not None  # static order check guarantees post-sum
                running_score *= rule.effect.multiply
                note = f"score × {rule.effect.multiply}"
        elif isinstance(rule, FlagRule):
            events.append(EmittedEvent(rule_id=rule.id, event=rule.effect.emit_event, route=rule.effect.route))
            note = f"emitted {rule.effect.emit_event!r}"

        trace.append(RuleTrace(rule_id=rule.id, rule_type=rule.type, fired=True, note=note))

    assert raw_score is not None and running_score is not None  # model requires the sentinel
    final = override_score if override_score is not None else running_score
    final = min(max(final, formula.scale.min), formula.scale.max)

    return ScoreResult(
        formula_id=formula.formula_id,
        rubric_version=formula.rubric_version,
        final_score=final,
        raw_score=raw_score,
        overridden=override_score is not None,
        effective_weights=weights,
        fracs=fracs,
        contributions={
            key: (weights[key] * frac if frac is not None else 0.0)
            for key, frac in fracs.items()
        },
        na_sections=na_sections,
        events=events,
        trace=trace,
    )


# ---------------------------------------------------------------------------
# Static order checks — fail before reading any answer
# ---------------------------------------------------------------------------

_PRE_SUM_ONLY = ("weight_transfer", "weight_redistribution", "na_redistribution")
_POST_SUM_ONLY = ("score_scale",)


def _check_static_order(formula: Formula) -> None:
    order = formula.evaluation_order
    if order.count(WEIGHTED_SUM) != 1:
        raise FormulaOrderError(
            f"formula {formula.formula_id!r}: evaluation_order must contain "
            f"exactly one {WEIGHTED_SUM!r} token"
        )
    sum_at = order.index(WEIGHTED_SUM)
    rules_by_id = {r.id: r for r in formula.rules}
    for pos, token in enumerate(order):
        if token == WEIGHTED_SUM:
            continue
        rtype = rules_by_id[token].type
        if rtype in _PRE_SUM_ONLY and pos > sum_at:
            raise FormulaOrderError(
                f"formula {formula.formula_id!r}: {rtype} rule {token!r} is "
                f"scheduled after {WEIGHTED_SUM!r} — weight moves would be lost"
            )
        if rtype in _POST_SUM_ONLY and pos < sum_at:
            raise FormulaOrderError(
                f"formula {formula.formula_id!r}: {rtype} rule {token!r} is "
                f"scheduled before {WEIGHTED_SUM!r} — there is no score to scale yet"
            )


# ---------------------------------------------------------------------------
# Normalization (MS §4 / Sales §4)
# ---------------------------------------------------------------------------

def _normalize_answers(
    formula: Formula, answers: dict[str, SectionAnswer]
) -> tuple[dict[str, Optional[float]], list[str]]:
    section_keys = {s.key for s in formula.sections}
    unknown = sorted(set(answers) - section_keys)
    if unknown:
        raise AnswerValidationError(
            f"formula {formula.formula_id!r}: answers for unknown sections "
            f"{unknown} — section-key mismatch (Wave2Plan §9)?"
        )
    missing = sorted(section_keys - set(answers))
    if missing:
        raise AnswerValidationError(
            f"formula {formula.formula_id!r}: missing answers for {missing}"
        )

    fracs: dict[str, Optional[float]] = {}
    na_sections: list[str] = []
    for section in formula.sections:
        answer = answers[section.key]
        kind, na_allowed = _SCORE_TYPE_TRAITS[section.score_type]

        if answer == "NA":
            if not na_allowed:
                raise AnswerValidationError(
                    f"section {section.key!r}: NA not allowed for "
                    f"score_type={section.score_type!r}"
                )
            na_sections.append(section.key)
            if formula.normalization.na == "full_credit":
                fracs[section.key] = 1.0  # Sales §4: full points on-slot
            else:
                fracs[section.key] = None  # inactive; a rule must move its weight
            continue

        if kind == "rating":
            fracs[section.key] = _rating_frac(formula, section, answer)
        else:
            fracs[section.key] = _binary_frac(formula, section, answer)

    return fracs, na_sections


def _rating_frac(formula: Formula, section: FormulaSection, answer: SectionAnswer) -> float:
    curve = formula.normalization.rating_1_5
    if not isinstance(answer, int) or isinstance(answer, bool) or not (
        curve.input_min <= answer <= curve.input_max
    ):
        raise AnswerValidationError(
            f"section {section.key!r}: expected int rating "
            f"{curve.input_min}-{curve.input_max}, got {answer!r}"
        )
    out_lo, out_hi = curve.output
    span = curve.input_max - curve.input_min
    return out_lo + (answer - curve.input_min) / span * (out_hi - out_lo)


def _binary_frac(formula: Formula, section: FormulaSection, answer: SectionAnswer) -> float:
    binary_map = section.binary_map or formula.normalization.binary_yn
    if answer == "Y":
        return binary_map.Y
    if answer == "N":
        return binary_map.N
    raise AnswerValidationError(
        f"section {section.key!r}: expected 'Y'/'N'"
        f"{'/NA' if _SCORE_TYPE_TRAITS[section.score_type][1] else ''}, got {answer!r}"
    )


# ---------------------------------------------------------------------------
# `when` evaluation
# ---------------------------------------------------------------------------

def _when_matches(
    when: RuleWhen,
    answers: dict[str, SectionAnswer],
    fracs: dict[str, Optional[float]],
    signals: dict[str, bool],
) -> bool:
    if isinstance(when, RuleWhenSignal):
        return signals.get(when.signal, False)
    if isinstance(when, RuleWhenAny):
        return any(_when_matches(c, answers, fracs, signals) for c in when.any)
    if isinstance(when, RuleWhenAll):
        return all(_when_matches(c, answers, fracs, signals) for c in when.all)

    # RuleWhenSection — AND every condition present on the clause.
    conditions = [when.equals is not None, when.frac_lte is not None, when.frac_gte is not None]
    if not any(conditions):
        raise RuleApplicationError(
            f"when-clause on section {when.section!r} has no condition "
            f"(one of equals/frac_lte/frac_gte required)"
        )
    if when.section not in fracs:
        raise RuleApplicationError(
            f"when-clause references unknown section {when.section!r}"
        )
    if when.equals is not None and str(answers[when.section]) != when.equals:
        return False
    frac = fracs[when.section]
    if when.frac_lte is not None and (frac is None or frac > when.frac_lte):
        return False
    if when.frac_gte is not None and (frac is None or frac < when.frac_gte):
        return False
    return True


# ---------------------------------------------------------------------------
# Weight-moving effects (all pre-sum; operate on current effective weights)
# ---------------------------------------------------------------------------

def _apply_weight_transfer(rule: WeightTransferRule, weights: dict[str, float]) -> str:
    effect = rule.effect
    source = effect.from_
    moved = weights[source] if effect.amount == "all" else weights[source] * (effect.fraction or 0.0)
    weights[source] -= moved

    if isinstance(effect.to, str):
        weights[effect.to] += moved
        return f"moved {moved:g} from {source!r} to {effect.to!r}"

    share_sum = sum(effect.to.values())
    if abs(share_sum - 1.0) > 1e-6:
        raise RuleApplicationError(
            f"rule {rule.id!r}: transfer target shares sum to {share_sum}, expected 1.0"
        )
    for target, share in effect.to.items():
        weights[target] += moved * share
    return f"moved {moved:g} from {source!r} split across {sorted(effect.to)}"


def _redistribution_targets(
    selector: Union[str, list[str]],
    exclude: set[str],
    weights: dict[str, float],
    fracs: dict[str, Optional[float]],
    rule_id: str,
) -> list[str]:
    """Resolve a to/targets selector to *active* sections (frac is not None).
    "active_sections" and "remaining" are aliases: every scored section except
    the sources — MS §3 footnote: the spread is recomputed over the sections
    that remain active."""
    if isinstance(selector, str):
        pool = [k for k in weights if k not in exclude and fracs.get(k) is not None]
    else:
        pool = [k for k in selector if k not in exclude and fracs.get(k) is not None]
    if not pool:
        raise RuleApplicationError(
            f"rule {rule_id!r}: no active target sections to redistribute to"
        )
    return pool


def _apply_weight_redistribution(
    rule: WeightRedistributionRule,
    weights: dict[str, float],
    fracs: dict[str, Optional[float]],
) -> str:
    effect = rule.effect
    pool_weight = weights[effect.from_]
    if pool_weight <= _EPS:
        return f"no-op: {effect.from_!r} holds no weight"
    targets = _redistribution_targets(effect.to, {effect.from_}, weights, fracs, rule.id)

    if effect.method == "equal_additive":
        per_target = pool_weight / len(targets)
        for key in targets:
            weights[key] += per_target
        detail = f"+{per_target:g} each to {len(targets)} active sections"
    else:  # proportional
        base = sum(weights[k] for k in targets)
        if base <= _EPS:
            raise RuleApplicationError(
                f"rule {rule.id!r}: proportional redistribution but targets hold no weight"
            )
        for key in targets:
            weights[key] += pool_weight * weights[key] / base
        detail = f"proportional across {len(targets)} active sections"

    weights[effect.from_] = 0.0
    return f"spread {pool_weight:g} from {effect.from_!r}: {detail}"


def _apply_na_redistribution(
    rule: NaRedistributionRule,
    weights: dict[str, float],
    fracs: dict[str, Optional[float]],
) -> str:
    """SQLMigration §3.8 proportional NA spread. Sources are the when-clause
    sections that are actually inactive (NA) and still hold weight."""
    sources = [
        key for key in sorted(_sections_in_when(rule.when))
        if fracs.get(key) is None and weights.get(key, 0.0) > _EPS
    ]
    if not sources:
        return "no-op: no NA sections with weight in when-clause"

    targets = _redistribution_targets(rule.targets, set(sources), weights, fracs, rule.id)
    base = sum(weights[k] for k in targets)
    if base <= _EPS:
        raise RuleApplicationError(
            f"rule {rule.id!r}: proportional redistribution but targets hold no weight"
        )
    total = sum(weights[k] for k in sources)
    for key in targets:
        weights[key] += total * weights[key] / base
    for key in sources:
        weights[key] = 0.0
    return f"spread {total:g} from {sources} proportionally across {len(targets)} sections"


def _sections_in_when(when: RuleWhen) -> set[str]:
    if isinstance(when, RuleWhenSection):
        return {when.section}
    if isinstance(when, (RuleWhenAny, RuleWhenAll)):
        clauses = when.any if isinstance(when, RuleWhenAny) else when.all
        return {c.section for c in clauses if isinstance(c, RuleWhenSection)}
    return set()


# ---------------------------------------------------------------------------
# NA safety net — MS §4: NA weight is "never silently lost"
# ---------------------------------------------------------------------------

def _assert_na_weight_handled(
    formula: Formula,
    weights: dict[str, float],
    fracs: dict[str, Optional[float]],
) -> None:
    stranded = {
        key: weights[key]
        for key, frac in fracs.items()
        if frac is None and weights[key] > _EPS
    }
    if stranded:
        raise UnhandledNaWeightError(formula.formula_id, stranded)


__all__ = [
    "SectionAnswer",
    "evaluate_formula",
    "ScoreResult",
    "EmittedEvent",
    "RuleTrace",
    "RuleEngineError",
    "FormulaOrderError",
    "AnswerValidationError",
    "UnhandledNaWeightError",
    "RuleApplicationError",
]
