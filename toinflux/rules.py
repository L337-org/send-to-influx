"""A small arithmetic expression language for control rules.

A control decides what to aim for from the estate's own data: hold the conservatory at
the greater of the user's target or dew point plus five, cap the heaters when grid carbon
is high, run only while it is cold outside. Those are expressions, and they arrive from a
configuration file that an MCP client can also write.

So they are **parsed, never evaluated as code**. The grammar is arithmetic over numbers
and nothing else: no strings, no attribute access, no indexing, no assignment, no calls
except a fixed table of five. There is no path from a rule to an object, an import or a
filesystem, because the language has no way to express one.

``simpleeval`` and ``asteval`` were the obvious alternatives and were rejected: both
evaluate a subset of the Python AST, which is a far larger surface to defend than a
numbers-only grammar needs, and both carry their own history of hardening around
operators and resource exhaustion.

Identifiers resolve against a binding table declared by the control, checked when the
rule is parsed rather than when it runs, so an undeclared name is a configuration error
an operator sees at ``--check-config`` rather than a surprise at three in the morning.

Truth is a number: a comparison yields 1.0 or 0.0, ``if`` treats any non-zero value as
true, and a rule used as a gate is true when it evaluates non-zero. Keeping one type
means there is no boolean/number coercion to get wrong - including for ``and``/``or``,
which return 1.0 or 0.0 rather than one of their operands as Python's do.

Precedence runs ``or`` < ``and`` < ``not`` < comparison < ``+ -`` < ``* /`` < unary
minus, which is Python's order, because anyone writing one of these has that order in
their fingers already. ``and``, ``or`` and ``not`` short-circuit, so a rule can guard
its own arithmetic: ``divisor != 0 and total / divisor > 5`` never divides by zero.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import re
from dataclasses import dataclass
from toinflux.exceptions import ConfigError, ToInfluxError


class RuleSyntaxError(ConfigError):
    """A rule could not be parsed, or names something that was never declared.

    A ``ConfigError`` because that is what it is: waiting does not fix it, and the
    operator has to edit the control before it can run. Carries the offset into the rule
    text so a caller can say where, not merely that.

    Built through :func:`_syntax_error` rather than a custom ``__init__``, so the
    exception carries exactly one argument and survives ``copy`` and ``pickle`` the way
    every other exception in the project does.

    Attributes:
        offset (int): character position in the rule text the problem was found at, or
            -1 when it was raised without one
    """

    offset = -1


def _syntax_error(message, offset):
    """Build a syntax error that knows where it happened.

    Args:
        message (str): what was wrong, without the rule text or the control's name
        offset (int): character position in the rule text

    Returns:
        RuleSyntaxError: ready to raise
    """
    error = RuleSyntaxError(f"{message} at offset {offset}")
    error.offset = offset
    return error


class RuleEvaluationError(ToInfluxError):
    """A parsed rule could not produce a value this cycle.

    Deliberately not a ``ConfigError``: the rule is well-formed and the configuration is
    fine. Something about *this evaluation* failed - a division by zero once an input hit
    a particular value. The control's fail-safe handles it, which is a different response
    from refusing to start, so it is a different type.
    """


# The whole function table. Adding one is a deliberate widening of what a rule can do and
# belongs in the design record, not in passing.
#
# Each entry is (minimum arguments, maximum arguments or None for unbounded).
FUNCTION_ARITY = {
    "min": (2, None),
    "max": (2, None),
    "abs": (1, 1),
    "clamp": (3, 3),
    "if": (3, 3),
}

# Reserved: these are operators, so a control cannot declare an input called `and` and
# then wonder why its rule will not parse.
KEYWORDS = frozenset({"and", "or", "not"})

COMPARISONS = {
    "<": lambda left, right: left < right,
    "<=": lambda left, right: left <= right,
    ">": lambda left, right: left > right,
    ">=": lambda left, right: left >= right,
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
}

# Longest first, so "<=" is never read as "<" followed by a stray "=".
_OPERATORS = ("<=", ">=", "==", "!=", "<", ">", "+", "-", "*", "/")

_NUMBER_RE = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class _Token:
    """One lexical unit of a rule.

    Attributes:
        kind (str): one of "number", "name", "operator", "(", ")", ","
        text (str): the matched text
        offset (int): where it started in the rule
    """

    kind: str
    text: str
    offset: int


def tokenise(text):
    """Split a rule into tokens.

    Args:
        text (str): the rule as written in the control document

    Returns:
        list: the tokens, in order

    Raises:
        RuleSyntaxError: the text contains a character the language has no meaning for
    """
    tokens = []
    position = 0
    while position < len(text):
        character = text[position]
        if character.isspace():
            position += 1
            continue
        if character in "(),":
            tokens.append(_Token(character, character, position))
            position += 1
            continue
        number = _NUMBER_RE.match(text, position)
        if number:
            tokens.append(_Token("number", number.group(), position))
            position = number.end()
            continue
        name = _NAME_RE.match(text, position)
        if name:
            tokens.append(_Token("name", name.group(), position))
            position = name.end()
            continue
        operator = next((candidate for candidate in _OPERATORS if text.startswith(candidate, position)), None)
        if operator:
            tokens.append(_Token("operator", operator, position))
            position += len(operator)
            continue
        # Anything else - a quote, a bracket, a dot, an ampersand. Named rather than
        # skipped, because every one of them is somebody expecting a language this is
        # deliberately not.
        raise _syntax_error(f"unexpected character {character!r}", position)
    return tokens


@dataclass(frozen=True)
class _Number:
    """A literal value.

    Attributes:
        value (float): the number as written
    """

    value: float

    def evaluate(self, bindings):
        """Return the literal.

        Args:
            bindings (dict): unused; a literal depends on nothing

        Returns:
            float: the value
        """
        return self.value

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: always empty
        """
        return set()


@dataclass(frozen=True)
class _Name:
    """A reference to a declared input or parameter.

    Attributes:
        identifier (str): the name as written
        offset (int): where it appeared, for a message
    """

    identifier: str
    offset: int

    def evaluate(self, bindings):
        """Look the identifier up in the bindings.

        Args:
            bindings (dict): identifier to number, supplied by the caller each cycle

        Returns:
            float: the bound value

        Raises:
            RuleEvaluationError: the binding is absent, or is not a number
        """
        if self.identifier not in bindings:
            # Parsing already rejected undeclared names, so reaching here means the
            # caller supplied an incomplete table - a stale input, most likely. A
            # runtime failure rather than a configuration one, so the control fails safe.
            raise RuleEvaluationError(f"no value available for {self.identifier!r}")
        value = bindings[self.identifier]
        # Bindings come from InfluxDB, which stores strings and nulls as happily as it
        # stores numbers, so a field whose type nobody checked would otherwise reach
        # float() and escape as a raw ValueError - crashing the control loop instead of
        # failing it safe. A bool is accepted deliberately: InfluxDB has a boolean field
        # type, a control reading one means exactly 1 or 0 by it, and there is nothing
        # ambiguous to guess at. That differs from a bool in a stage's `level:`, which is
        # an operator typo rather than data, and is refused there.
        if not isinstance(value, (int, float, bool)):
            raise RuleEvaluationError(
                f"{self.identifier!r} is {type(value).__name__}, not a number, so the rule cannot be evaluated"
            )
        return float(value)

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: this node's identifier
        """
        return {self.identifier}


@dataclass(frozen=True)
class _Unary:
    """A negation.

    Attributes:
        operand (object): the node being negated
    """

    operand: object

    def evaluate(self, bindings):
        """Negate the operand.

        Args:
            bindings (dict): identifier to number

        Returns:
            float: the negated value
        """
        return -self.operand.evaluate(bindings)

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: the operand's identifiers
        """
        return self.operand.names()


@dataclass(frozen=True)
class _Binary:
    """An arithmetic or comparison operation.

    Attributes:
        operator (str): the operator text
        left (object): the left operand
        right (object): the right operand
        offset (int): where the operator appeared, for a message
    """

    operator: str
    left: object
    right: object
    offset: int

    def evaluate(self, bindings):
        """Apply the operator to both operands.

        Args:
            bindings (dict): identifier to number

        Returns:
            float: the result; a comparison yields 1.0 or 0.0

        Raises:
            RuleEvaluationError: the operation cannot produce a value, i.e. a division
                by zero
        """
        left = self.left.evaluate(bindings)
        right = self.right.evaluate(bindings)
        if self.operator in COMPARISONS:
            return 1.0 if COMPARISONS[self.operator](left, right) else 0.0
        if self.operator == "+":
            return left + right
        if self.operator == "-":
            return left - right
        if self.operator == "*":
            return left * right
        if right == 0:
            raise RuleEvaluationError(f"division by zero at offset {self.offset}")
        return left / right

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: the identifiers of both operands
        """
        return self.left.names() | self.right.names()


@dataclass(frozen=True)
class _Not:
    """A logical negation.

    Attributes:
        operand (object): the node whose truth is inverted
    """

    operand: object

    def evaluate(self, bindings):
        """Return the inverse truth of the operand.

        Args:
            bindings (dict): identifier to number

        Returns:
            float: 1.0 when the operand is zero, 0.0 otherwise
        """
        return 0.0 if self.operand.evaluate(bindings) else 1.0

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: the operand's identifiers
        """
        return self.operand.names()


@dataclass(frozen=True)
class _Logical:
    """A short-circuiting ``and`` or ``or``.

    Attributes:
        operator (str): "and" or "or"
        left (object): the left operand
        right (object): the right operand
    """

    operator: str
    left: object
    right: object

    def evaluate(self, bindings):
        """Combine both sides, evaluating the right only when it can change the answer.

        Short-circuiting is what lets a rule guard its own arithmetic: in
        ``divisor != 0 and total / divisor > 5`` the division is never reached when the
        divisor is zero, so the guard the operator wrote actually works.

        Args:
            bindings (dict): identifier to number

        Returns:
            float: 1.0 or 0.0, never one of the operands - a rule is arithmetic, and
                Python's value-returning semantics would leak a non-truth value into a
                sum
        """
        left = self.left.evaluate(bindings)
        if self.operator == "and":
            if not left:
                return 0.0
            return 1.0 if self.right.evaluate(bindings) else 0.0
        if left:
            return 1.0
        return 1.0 if self.right.evaluate(bindings) else 0.0

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: the identifiers of both operands
        """
        return self.left.names() | self.right.names()


@dataclass(frozen=True)
class _Call:
    """One of the five permitted functions.

    Attributes:
        function (str): the function name
        arguments (tuple): the argument nodes
        offset (int): where the call appeared, for a message
    """

    function: str
    arguments: tuple
    offset: int

    def evaluate(self, bindings):
        """Apply the function to its arguments.

        ``if`` evaluates its condition first and only the branch it selects, so a rule
        like ``if(divisor == 0, 0, total / divisor)`` is usable as a guard rather than
        raising from the branch it was written to avoid.

        Args:
            bindings (dict): identifier to number

        Returns:
            float: the function's result

        Raises:
            RuleEvaluationError: a clamp was given bounds the wrong way round
        """
        if self.function == "if":
            condition = self.arguments[0].evaluate(bindings)
            chosen = self.arguments[1] if condition else self.arguments[2]
            return chosen.evaluate(bindings)
        values = [argument.evaluate(bindings) for argument in self.arguments]
        if self.function == "min":
            return min(values)
        if self.function == "max":
            return max(values)
        if self.function == "abs":
            return abs(values[0])
        value, lower, upper = values
        # Bounds the wrong way round would otherwise return the lower bound for every
        # input, which looks like a working clamp and is not.
        if lower > upper:
            raise RuleEvaluationError(f"clamp lower bound {lower} is above its upper bound {upper}")
        return max(lower, min(value, upper))

    def names(self):
        """Return the identifiers this node depends on.

        Returns:
            set: the identifiers of every argument
        """
        return set().union(*(argument.names() for argument in self.arguments)) if self.arguments else set()


class _Parser:
    """Recursive descent over a rule's tokens.

    One method per precedence level, lowest first, each consuming the level above it.
    The order is Python's - ``or`` < ``and`` < ``not`` < comparison < ``+ -`` < ``* /`` <
    unary minus - because anyone writing a rule already has that order in their fingers,
    and a language that looked like Python but bound differently would be worse than one
    that looked nothing like it.
    """

    def __init__(self, text, allowed_names):
        """Prepare to parse one rule.

        Args:
            text (str): the rule as written
            allowed_names (collections.abc.Container): identifiers the control declared
        """
        self.text = text
        self.tokens = tokenise(text)
        self.allowed_names = allowed_names
        self.position = 0

    def peek(self):
        """Return the next token without consuming it.

        Returns:
            _Token or None: the next token, or None at the end of the rule
        """
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self):
        """Consume and return the next token.

        Returns:
            _Token: the token consumed

        Raises:
            RuleSyntaxError: the rule ended where something was expected
        """
        token = self.peek()
        if token is None:
            raise _syntax_error("the rule ends unexpectedly", len(self.text))
        self.position += 1
        return token

    def _at_keyword(self, keyword):
        """Whether the next token is a particular keyword.

        Args:
            keyword (str): the keyword to test for

        Returns:
            bool: True when the next token is that keyword
        """
        token = self.peek()
        return token is not None and token.kind == "name" and token.text == keyword

    def _at_operator(self, operators):
        """Whether the next token is one of a set of operators.

        Args:
            operators (tuple): the operator texts to test for

        Returns:
            bool: True when the next token is one of them
        """
        token = self.peek()
        return token is not None and token.kind == "operator" and token.text in operators

    def parse(self):
        """Parse the whole rule and insist nothing is left over.

        Returns:
            object: the root node

        Raises:
            RuleSyntaxError: the rule is empty, malformed, or has trailing text
        """
        if not self.tokens:
            raise _syntax_error("the rule is empty", 0)
        node = self.expression()
        leftover = self.peek()
        if leftover is not None:
            # Reported rather than ignored: trailing text means the rule says something
            # other than the operator thought, and silently using the first half is the
            # worst of the three options.
            raise _syntax_error(f"unexpected {leftover.text!r} after a complete expression", leftover.offset)
        return node

    def expression(self):
        """Parse an ``or`` chain, the lowest precedence level.

        Returns:
            object: the node
        """
        node = self.conjunction()
        while self._at_keyword("or"):
            self.take()
            node = _Logical("or", node, self.conjunction())
        return node

    def conjunction(self):
        """Parse an ``and`` chain.

        Returns:
            object: the node
        """
        node = self.negation()
        while self._at_keyword("and"):
            self.take()
            node = _Logical("and", node, self.negation())
        return node

    def negation(self):
        """Parse ``not``, which binds tighter than ``and`` and looser than a comparison.

        Returns:
            object: the node
        """
        if self._at_keyword("not"):
            self.take()
            return _Not(self.negation())
        return self.comparison()

    def comparison(self):
        """Parse an optional comparison between two sums.

        Deliberately non-associative: ``a < b < c`` is refused rather than read as
        Python's chain or as C's ``(a < b) < c``. Both readings are defensible and they
        disagree, so a rule that could mean either is a rule nobody should have to guess
        about.

        Returns:
            object: the node

        Raises:
            RuleSyntaxError: two comparisons appear in a row
        """
        node = self.sum()
        if self._at_operator(tuple(COMPARISONS)):
            token = self.take()
            node = _Binary(token.text, node, self.sum(), token.offset)
            if self._at_operator(tuple(COMPARISONS)):
                following = self.peek()
                raise _syntax_error(
                    f"cannot chain comparisons - write 'a < b and b < c' instead of 'a < b {following.text} c'",
                    following.offset,
                )
        return node

    def sum(self):
        """Parse addition and subtraction.

        Returns:
            object: the node
        """
        node = self.product()
        while self._at_operator(("+", "-")):
            token = self.take()
            node = _Binary(token.text, node, self.product(), token.offset)
        return node

    def product(self):
        """Parse multiplication and division.

        Returns:
            object: the node
        """
        node = self.unary()
        while self._at_operator(("*", "/")):
            token = self.take()
            node = _Binary(token.text, node, self.unary(), token.offset)
        return node

    def unary(self):
        """Parse a leading minus.

        Returns:
            object: the node
        """
        if self._at_operator(("-",)):
            self.take()
            return _Unary(self.unary())
        return self.primary()

    def primary(self):
        """Parse a number, a name, a call, or a parenthesised expression.

        Returns:
            object: the node

        Raises:
            RuleSyntaxError: the token cannot start an expression
        """
        token = self.take()
        if token.kind == "number":
            return _Number(float(token.text))
        if token.kind == "(":
            node = self.expression()
            closing = self.take()
            if closing.kind != ")":
                raise _syntax_error(f"expected ')' but found {closing.text!r}", closing.offset)
            return node
        if token.kind == "name":
            return self._name_or_call(token)
        raise _syntax_error(f"{token.text!r} cannot start an expression", token.offset)

    def _name_or_call(self, token):
        """Resolve a name token to a binding reference or a function call.

        Args:
            token (_Token): the name token already consumed

        Returns:
            object: the node

        Raises:
            RuleSyntaxError: the name is a keyword in the wrong place, an unknown
                function, or an identifier the control never declared
        """
        if token.text in KEYWORDS:
            raise _syntax_error(f"{token.text!r} is an operator and cannot be used here", token.offset)
        if self.peek() is not None and self.peek().kind == "(":
            return self._call(token)
        if token.text not in self.allowed_names:
            # Caught here rather than at evaluation, so an operator sees it at
            # --check-config rather than when a control first tries to run.
            raise _syntax_error(
                f"unknown name {token.text!r} (declared: {', '.join(sorted(self.allowed_names)) or 'nothing'})",
                token.offset,
            )
        return _Name(token.text, token.offset)

    def _call(self, token):
        """Parse a function call and check its arity.

        Args:
            token (_Token): the function's name token

        Returns:
            _Call: the node

        Raises:
            RuleSyntaxError: the function is not one of the permitted set, or was given
                the wrong number of arguments
        """
        if token.text not in FUNCTION_ARITY:
            raise _syntax_error(
                f"unknown function {token.text!r} (available: {', '.join(sorted(FUNCTION_ARITY))})",
                token.offset,
            )
        self.take()
        arguments = []
        if not (self.peek() is not None and self.peek().kind == ")"):
            arguments.append(self.expression())
            while self.peek() is not None and self.peek().kind == ",":
                self.take()
                arguments.append(self.expression())
        closing = self.take()
        if closing.kind != ")":
            raise _syntax_error(f"expected ')' or ',' but found {closing.text!r}", closing.offset)

        minimum, maximum = FUNCTION_ARITY[token.text]
        if len(arguments) < minimum or (maximum is not None and len(arguments) > maximum):
            wanted = f"{minimum}" if maximum == minimum else f"{minimum} or more" if maximum is None else "..."
            raise _syntax_error(
                f"{token.text}() takes {wanted} argument(s), got {len(arguments)}",
                token.offset,
            )
        return _Call(token.text, tuple(arguments), token.offset)


@dataclass(frozen=True)
class Rule:
    """A parsed rule, ready to evaluate as often as a control cycles.

    Parsing happens once at load. A control loop runs every cycle for months, and
    re-parsing the same text each time would turn a configuration error into a runtime
    one and do needless work besides.

    Attributes:
        source (str): the rule exactly as the operator wrote it, for messages
        root (object): the parsed expression tree
        referenced (frozenset): the identifiers this rule actually reads
    """

    source: str
    root: object
    referenced: frozenset

    def evaluate(self, bindings):
        """Compute the rule's value for one set of inputs.

        Args:
            bindings (dict): identifier to number, covering at least
                :attr:`referenced`

        Returns:
            float: the rule's value; a rule used as a gate is true when this is non-zero

        Raises:
            RuleEvaluationError: a binding was missing, or the arithmetic could not
                produce a value
        """
        return float(self.root.evaluate(bindings))


def parse_rule(text, allowed_names=()):
    """Parse one rule, resolving its names against what the control declared.

    ``allowed_names`` is normalised to a frozenset here rather than used as given: the
    parser both tests membership and lists it in an error message, so a one-shot iterable
    would be consumed by the first failure and report nothing afterwards.

    A bare string is refused rather than accepted. It satisfies every iterable contract
    and iterates one character at a time, so ``allowed_names="target"`` would silently
    declare ``t``, ``a``, ``r``, ``g`` and ``e`` as valid names and accept a rule the
    control never meant to allow.

    Args:
        text (str): the rule as written in the control document
        allowed_names (collections.abc.Iterable): identifiers the control declared as
            inputs or parameters

    Returns:
        Rule: the parsed rule

    Raises:
        RuleSyntaxError: the rule does not parse, or names something undeclared
        ConfigError: ``allowed_names`` was given as a bare string
    """
    if not isinstance(text, str):
        raise _syntax_error(f"a rule must be text, got {type(text).__name__}", 0)
    if isinstance(allowed_names, str):
        raise ConfigError(
            f"allowed_names must be a collection of identifiers, not the string {allowed_names!r} - "
            f"a string would declare each of its characters as a separate name"
        )
    root = _Parser(text, frozenset(allowed_names)).parse()
    return Rule(source=text, root=root, referenced=frozenset(root.names()))
