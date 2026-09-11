"""Unit tests for toinflux.rules (the control rule language).

Two things are being asserted here, and they pull in different directions. The language
has to be expressive enough for a real control rule, and small enough that there is
provably no way out of it - no attribute access, no strings, no imports, no calls beyond
a fixed table. The second half is why so many of these tests assert that something is
*refused*.
"""

import math
import sys
import pytest
from toinflux.exceptions import ConfigError
from toinflux.rules import (
    FUNCTION_ARITY,
    MAX_NESTING_DEPTH,
    MAX_RULE_LENGTH,
    Rule,
    RuleEvaluationError,
    RuleSyntaxError,
    parse_rule,
    tokenise,
)

NAMES = frozenset({"target", "dew", "outside", "grid_co2", "total", "divisor", "a", "b", "c"})


def evaluate(text, **bindings):
    """Parse and evaluate a rule in one step.

    Args:
        text (str): the rule
        **bindings: the values to evaluate it against

    Returns:
        float: the rule's value
    """
    return parse_rule(text, NAMES).evaluate(bindings)


class TestTheDocumentedExamples:
    """The rules the design note shows. If these stop working the documentation is
    wrong, which is worse than the code being wrong because nobody checks it."""

    def test_the_dew_point_floor(self):
        # "the greater of the user's target or dew point plus five degrees"
        assert evaluate("max(target, dew + 5)", target=18, dew=16) == 21
        assert evaluate("max(target, dew + 5)", target=18, dew=10) == 18

    def test_capping_the_ladder_on_grid_carbon(self):
        assert evaluate("if(grid_co2 > 300, 750, 2250)", grid_co2=400) == 750
        assert evaluate("if(grid_co2 > 300, 750, 2250)", grid_co2=100) == 2250

    def test_a_gate_on_the_outside_temperature(self):
        assert evaluate("outside < 15", outside=10) == 1.0
        assert evaluate("outside < 15", outside=20) == 0.0


class TestArithmetic:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1 + 2", 3),
            ("5 - 2", 3),
            ("2 * 3", 6),
            ("7 / 2", 3.5),
            ("1 + 2 * 3", 7),
            ("(1 + 2) * 3", 9),
            ("-3 + 1", -2),
            ("- -3", 3),
            ("2.5 * 2", 5),
            ("1e3 / 10", 100),
        ],
    )
    def test_precedence_and_literals(self, text, expected):
        assert evaluate(text) == expected

    def test_division_by_zero_is_a_runtime_failure_not_a_crash(self):
        # Not a ConfigError: the rule is well-formed and the configuration is fine, this
        # evaluation just cannot produce a value. The control's fail-safe handles it.
        with pytest.raises(RuleEvaluationError):
            evaluate("total / divisor", total=1, divisor=0)

    def test_an_evaluation_failure_is_not_a_config_error(self):
        # The distinction decides what a caller does: refuse to start, or fail safe and
        # carry on. Asserted directly because both inherit ToInfluxError.
        with pytest.raises(RuleEvaluationError) as exc:
            evaluate("total / divisor", total=1, divisor=0)
        assert not isinstance(exc.value, ConfigError)


class TestComparisons:
    @pytest.mark.parametrize(
        "text,expected",
        [("1 < 2", 1), ("2 < 1", 0), ("2 <= 2", 1), ("3 > 2", 1), ("2 >= 3", 0), ("1 == 1", 1), ("1 != 1", 0)],
    )
    def test_a_comparison_yields_one_or_zero(self, text, expected):
        # Truth is a number here, so there is no boolean type to coerce anywhere.
        assert evaluate(text) == expected

    def test_a_comparison_binds_looser_than_arithmetic(self):
        assert evaluate("1 + 1 == 2") == 1.0

    def test_chained_comparisons_are_refused_rather_than_guessed_at(self):
        # Python reads a < b < c as a chain; C reads it as (a < b) < c. Both are
        # defensible and they disagree, so a rule that could mean either is refused.
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("a < b < c", NAMES)
        assert "chain" in str(exc.value)
        assert "and" in str(exc.value)


class TestBooleanOperators:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1 and 1", 1),
            ("1 and 0", 0),
            ("0 or 1", 1),
            ("0 or 0", 0),
            ("not 0", 1),
            ("not 1", 0),
            ("not 0 and 1", 1),
        ],
    )
    def test_truth_tables(self, text, expected):
        assert evaluate(text) == expected

    def test_or_binds_looser_than_and(self):
        # 1 or (0 and 0) is 1; (1 or 0) and 0 is 0. Python's order, which is what anyone
        # writing one of these expects.
        assert evaluate("1 or 0 and 0") == 1.0

    def test_not_binds_tighter_than_and(self):
        assert evaluate("not 0 and 0") == 0.0

    def test_comparisons_bind_tighter_than_and(self):
        assert evaluate("outside < 15 and grid_co2 < 300", outside=10, grid_co2=200) == 1.0
        assert evaluate("outside < 15 and grid_co2 < 300", outside=10, grid_co2=400) == 0.0

    def test_and_returns_a_truth_value_not_an_operand(self):
        # Python would return 7 here. A rule is arithmetic, so a non-truth value leaking
        # out of a gate and into a sum is exactly the confusion to avoid.
        assert evaluate("1 and 7") == 1.0

    def test_or_returns_a_truth_value_not_an_operand(self):
        assert evaluate("7 or 0") == 1.0

    def test_and_short_circuits_so_a_rule_can_guard_its_own_arithmetic(self):
        # The whole point of short-circuiting here: the operator wrote a guard, and it
        # has to actually guard.
        assert evaluate("divisor != 0 and total / divisor > 5", total=100, divisor=0) == 0.0

    def test_or_short_circuits_too(self):
        assert evaluate("divisor == 0 or total / divisor > 5", total=100, divisor=0) == 1.0


class TestFunctions:
    def test_min_and_max_take_any_number_of_arguments(self):
        assert evaluate("min(3, 1, 2)") == 1
        assert evaluate("max(3, 1, 2)") == 3

    def test_abs_clamp_and_if(self):
        assert evaluate("abs(0 - 4)") == 4
        assert evaluate("clamp(30, 5, 25)") == 25
        assert evaluate("clamp(1, 5, 25)") == 5
        assert evaluate("clamp(10, 5, 25)") == 10
        assert evaluate("if(1, 2, 3)") == 2
        assert evaluate("if(0, 2, 3)") == 3

    def test_if_evaluates_only_the_branch_it_chooses(self):
        # Same reasoning as short-circuiting: a rule using if() as a guard must not
        # evaluate the branch it was written to avoid.
        assert evaluate("if(divisor == 0, 0, total / divisor)", total=1, divisor=0) == 0

    def test_clamp_with_inverted_bounds_is_refused_at_evaluation(self):
        # Silently returning the lower bound for every input looks like a working clamp
        # and is not, which is worse than failing.
        with pytest.raises(RuleEvaluationError):
            evaluate("clamp(10, 25, 5)")

    @pytest.mark.parametrize("text", ["max(1)", "abs(1, 2)", "clamp(1, 2)", "if(1, 2)", "abs()"])
    def test_the_wrong_number_of_arguments_is_refused(self, text):
        with pytest.raises(RuleSyntaxError):
            parse_rule(text, NAMES)

    def test_an_unknown_function_lists_the_ones_that_exist(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("sqrt(4)", NAMES)
        message = str(exc.value)
        assert "sqrt" in message
        for available in FUNCTION_ARITY:
            assert available in message


class TestNames:
    def test_a_declared_name_resolves(self):
        assert evaluate("target", target=18) == 18

    def test_an_undeclared_name_is_refused_when_the_rule_is_parsed(self):
        # Caught at --check-config rather than at three in the morning when a control
        # first tries to run.
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("humidity", NAMES)
        assert "humidity" in str(exc.value)

    def test_the_refusal_says_what_was_declared(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("humidity", {"target", "dew"})
        assert "target" in str(exc.value) and "dew" in str(exc.value)

    def test_a_rule_with_nothing_declared_says_so_rather_than_showing_an_empty_list(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("target", set())
        assert "nothing" in str(exc.value)

    def test_a_rule_reports_which_names_it_reads(self):
        # The control loop uses this to know which inputs it must fetch before evaluating.
        rule = parse_rule("max(target, dew + 5)", NAMES)
        assert rule.referenced == frozenset({"target", "dew"})

    def test_a_name_the_binding_lacks_at_runtime_fails_safe_rather_than_silently(self):
        rule = parse_rule("target + 1", NAMES)
        with pytest.raises(RuleEvaluationError) as exc:
            rule.evaluate({})
        assert "target" in str(exc.value)

    @pytest.mark.parametrize("value", ["hello", None, [1], {"a": 1}, object()])
    def test_a_binding_that_is_not_a_number_fails_safe_rather_than_crashing(self, value):
        # Bindings come from InfluxDB, which stores strings and nulls as happily as
        # numbers. Unchecked, a string field reached float() and escaped as a raw
        # ValueError, taking the control loop down instead of failing it safe.
        rule = parse_rule("target + 1", NAMES)
        with pytest.raises(RuleEvaluationError) as exc:
            rule.evaluate({"target": value})
        assert "target" in str(exc.value)
        assert type(value).__name__ in str(exc.value)

    def test_a_boolean_binding_is_accepted_as_one_or_zero(self):
        # Deliberately different from a bool in a stage's `level:`, which is refused.
        # There a bool is an operator typo; here it is data - InfluxDB has a boolean
        # field type, and a control reading one means exactly 1 or 0 by it.
        rule = parse_rule("target", NAMES)
        assert rule.evaluate({"target": True}) == 1.0
        assert rule.evaluate({"target": False}) == 0.0

    @pytest.mark.parametrize("keyword", ["and", "or", "not"])
    def test_an_operator_cannot_be_used_as_a_name(self, keyword):
        with pytest.raises(RuleSyntaxError):
            parse_rule(keyword, NAMES | {keyword})


class TestTheLanguageHasNoWayOut:
    """The security half. Each of these is a thing a Python-based evaluator would accept
    and this grammar has no production for, so they fail at the tokeniser or the parser
    rather than being blocked by a deny-list that has to be kept complete."""

    @pytest.mark.parametrize(
        "text",
        [
            "__import__('os')",
            "target.__class__",
            "target.real",
            "[1, 2]",
            "target[0]",
            "'a string'",
            '"a string"',
            "{1: 2}",
            "lambda: 1",
            "target = 1",
            "target; target",
            "1 ** 2",
            "1 | 2",
            "1 & 2",
            "target if 1 else 2",
            "f'{target}'",
            "#comment",
            "1 @ 2",
            "target\\ntarget",
        ],
    )
    def test_anything_outside_the_grammar_is_refused(self, text):
        with pytest.raises(RuleSyntaxError):
            parse_rule(text, NAMES)

    def test_declared_names_given_as_a_bare_string_are_refused(self):
        # A string satisfies every iterable contract and iterates one character at a
        # time, so this would silently declare 't', 'a', 'r', 'g' and 'e' as names and
        # accept a rule the control never meant to allow.
        with pytest.raises(ConfigError) as exc:
            parse_rule("t + a", "target")
        assert "string" in str(exc.value)

    @pytest.mark.parametrize("value", [None, 42, object()])
    def test_declared_names_that_are_not_a_collection_are_reported_not_crashed(self, value):
        # Reachable from real configuration rather than only from a coding slip: a control
        # document with no `inputs:` section yields None here, and every other input
        # problem in this function becomes a typed error rather than a raw TypeError.
        with pytest.raises(ConfigError) as exc:
            parse_rule("1", value)
        assert type(value).__name__ in str(exc.value)

    def test_declared_names_may_be_any_iterable(self):
        # Normalised on the way in, so a one-shot iterable is not consumed by the first
        # membership test and then absent from the error message.
        rule = parse_rule("target", (name for name in ["target", "dew"]))
        assert rule.referenced == frozenset({"target"})

    def test_a_one_shot_iterable_still_lists_the_declared_names_on_failure(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("humidity", (name for name in ["target", "dew"]))
        assert "target" in str(exc.value) and "dew" in str(exc.value)

    def test_a_rule_must_be_text(self):
        with pytest.raises(RuleSyntaxError):
            parse_rule({"not": "a rule"}, NAMES)

    def test_a_syntax_error_is_a_config_error(self):
        # It stops the control from starting and waiting will not fix it, which is what
        # ConfigError means everywhere else in the project.
        with pytest.raises(ConfigError):
            parse_rule("1 +", NAMES)


class TestErrorsPointAtTheProblem:
    def test_an_error_carries_the_offset(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("1 + $", NAMES)
        assert exc.value.offset == 4

    def test_the_offset_is_in_the_message_too(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("1 + $", NAMES)
        assert "offset 4" in str(exc.value)

    def test_an_empty_rule_says_so(self):
        with pytest.raises(RuleSyntaxError, match="empty"):
            parse_rule("   ", NAMES)

    def test_trailing_text_is_refused_rather_than_ignored(self):
        # Using the first half of a rule the operator did not write is the worst of the
        # available options.
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("1 + 1 2", NAMES)
        assert "after a complete expression" in str(exc.value)

    def test_an_unclosed_bracket_is_reported(self):
        with pytest.raises(RuleSyntaxError):
            parse_rule("max(1, 2", NAMES)

    def test_an_unexpected_character_is_named(self):
        with pytest.raises(RuleSyntaxError) as exc:
            parse_rule("1 ? 2", NAMES)
        assert "?" in str(exc.value)


class TestTokeniser:
    def test_whitespace_is_not_significant(self):
        assert evaluate("  1   +\t2  ") == 3

    def test_two_character_operators_are_not_split(self):
        # "<=" must never be read as "<" followed by a stray "=".
        assert [token.text for token in tokenise("a <= b")] == ["a", "<=", "b"]

    def test_offsets_point_into_the_original_text(self):
        tokens = tokenise("1 + 22")
        assert [token.offset for token in tokens] == [0, 2, 4]


class TestParsingHappensOnce:
    def test_a_parsed_rule_is_reusable(self):
        # A control loop runs this every cycle for months; re-parsing each time would do
        # needless work and turn a config error into a runtime one.
        rule = parse_rule("target * 2", NAMES)
        assert rule.evaluate({"target": 2}) == 4
        assert rule.evaluate({"target": 3}) == 6

    def test_a_rule_keeps_the_text_as_written(self):
        rule = parse_rule("max(target, dew + 5)", NAMES)
        assert isinstance(rule, Rule)
        assert rule.source == "max(target, dew + 5)"

    def test_evaluation_always_produces_a_float(self):
        # One numeric type out, so a caller never has to care whether a rule happened to
        # be all-integer this cycle.
        value = evaluate("1 + 1")
        assert isinstance(value, float)
        assert not math.isnan(value)


class TestBounds:
    """A rule is external input, so its cost has to be bounded before it is parsed.

    Recursive descent recurses: nesting depth is stack depth. Unbounded, a few thousand
    opening brackets exhaust the interpreter and raise RecursionError - which is not a
    RuleSyntaxError, so it escapes as a crash in whatever was parsing rather than being
    reported as the bad rule it is.
    """

    def test_a_rule_at_the_nesting_limit_still_parses(self):
        # This is the test that keeps MAX_NESTING_DEPTH honest. The limit was set from a
        # measurement - roughly ten Python frames per level against the default
        # recursion limit - so if a future interpreter spends more frames per level, this
        # fails in CI rather than a control process falling over in the field.
        text = "(" * MAX_NESTING_DEPTH + "1" + ")" * MAX_NESTING_DEPTH
        assert parse_rule(text, NAMES).evaluate({}) == 1.0

    def test_one_level_past_the_limit_is_refused(self):
        depth = MAX_NESTING_DEPTH + 1
        with pytest.raises(RuleSyntaxError, match="nests more than"):
            parse_rule("(" * depth + "1" + ")" * depth, NAMES)

    @pytest.mark.parametrize(
        "shape,build",
        [
            ("brackets", lambda n: "(" * n + "1" + ")" * n),
            ("unary minus", lambda n: "-" * n + "1"),
            ("call arguments", lambda n: "abs(" * n + "1" + ")" * n),
            ("not", lambda n: "not " * n + "1"),
        ],
    )
    def test_every_recursive_route_is_bounded_by_the_depth_guard(self, shape, build):
        """Each of these recurses by a different route, and each route needs its own
        guard: brackets and call arguments through expression(), repeated minus through
        unary(), repeated not through negation().

        Sized just past the depth limit rather than arbitrarily large, and the message is
        asserted rather than only the exception type. The first version of this test used
        five hundred repetitions, which put two of the four cases over MAX_RULE_LENGTH -
        so they were refused by the *length* guard while claiming to prove the *depth*
        guard, and passed without ever exercising it. Both guards raise the same type,
        which is exactly why the type alone proves nothing here.
        """
        text = build(MAX_NESTING_DEPTH + 1)
        assert len(text) <= MAX_RULE_LENGTH, "this case is being caught by the length guard instead"
        with pytest.raises(RuleSyntaxError, match="nests more than"):
            parse_rule(text, NAMES)

    def test_a_wide_shallow_rule_is_not_mistaken_for_a_deep_one(self):
        # The limit measures nesting, not size. Every argument of a call is its own
        # expression, so a counter that only ever went up would refuse this - 60 siblings
        # at a nesting depth of two - and an operator would be told their perfectly flat
        # rule was too deeply nested.
        arguments = ", ".join(["1"] * 60)
        assert parse_rule(f"min({arguments})", NAMES).evaluate({}) == 1.0

    def test_a_long_flat_sum_is_not_mistaken_for_a_deep_one(self):
        # Same property by the other route: repeated binary operators at one level.
        assert parse_rule(" + ".join(["1"] * 60), NAMES).evaluate({}) == 60.0

    def test_an_overlong_rule_is_refused_before_it_is_parsed(self):
        with pytest.raises(RuleSyntaxError, match="at most"):
            parse_rule("1 + " * MAX_RULE_LENGTH + "1", NAMES)

    def test_a_rule_of_ordinary_length_is_unaffected(self):
        assert parse_rule("max(target, dew + 5)", NAMES).evaluate({"target": 18, "dew": 16}) == 21


def test_a_recursion_error_is_translated_rather_than_escaping(monkeypatch):
    """The backstop behind the depth limit.

    MAX_NESTING_DEPTH should make this unreachable, so it is provoked directly by
    lowering the interpreter's own limit. The cost of the bound being one interpreter
    release too generous is a raw RecursionError reaching a control process, and the text
    comes from a file an MCP client can write - so a report about a bad rule is the right
    outcome even when the first line of defence has been out-thought.
    """
    monkeypatch.setattr(sys, "setrecursionlimit", sys.setrecursionlimit)
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(60)
    try:
        with pytest.raises(RuleSyntaxError, match="too deeply"):
            parse_rule("(" * (MAX_NESTING_DEPTH - 1) + "1" + ")" * (MAX_NESTING_DEPTH - 1), NAMES)
    finally:
        sys.setrecursionlimit(original)
