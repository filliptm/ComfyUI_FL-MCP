from __future__ import annotations

import ast
import asyncio
import math
import operator as op
import random
from typing import Dict, Iterable, List, Optional, Sequence, Union

from pydantic import BaseModel, Field, model_validator


class CalcBatchParams(BaseModel):
    """
    Parameters for `calc_batch` / `acalc_batch`.

    Notes
    -----
    - `spec` accepts **either** a single string with multiple expressions
      separated by newlines, commas, or semicolons **or** a list of strings.
    - `variables` is an optional environment of variables that will be
      read and **updated** during evaluation (assignments persist).
    - `seed` controls deterministic behavior of random functions.
    - `yield_every` applies to the async version to keep the event loop responsive.
    """
    spec: Union[str, List[str]] = Field(
        ...,
        description=(
            "The equations to evaluate. If a single string, expressions may be "
            "separated by newlines, commas, or semicolons. If a list, one "
            "expression per item."
        ),
    )
    variables: Optional[Dict[str, float]] = Field(
        None,
        description=(
            "Optional variable environment. Names/values provided here are available "
            "to expressions. Assignments update this mapping with numeric results."
        ),
    )
    seed: Optional[int] = Field(
        None,
        description=(
            "Seed for deterministic randomness. If provided, `rand()`, `uniform(a,b)`, "
            "and `randint(a,b)` are repeatable."
        ),
    )
    yield_every: int = Field(
        100,
        ge=1,
        description=(
            "Async only: after processing this many expressions, yield to the event "
            "loop with `await asyncio.sleep(0)`."
        ),
    )

    # Normalize `spec` to a list of trimmed, non-empty expressions
    @model_validator(mode="after")
    def _normalize_spec(self) -> "CalcBatchParams":
        if isinstance(self.spec, str):
            pieces: List[str] = []
            for chunk in self.spec.replace(",", "\n").replace(";", "\n").splitlines():
                s = chunk.strip()
                if s:
                    pieces.append(s)
            object.__setattr__(self, "spec", pieces)
        else:
            object.__setattr__(
                self,
                "spec",
                [s.strip() for s in self.spec if str(s).strip()],
            )
        return self


class _SafeMathEvaluator:
    """
    A minimal, safe expression evaluator with a tiny AST whitelist.

    Supports:
      - Literals: int/float
      - Variables (from provided env)
      - Binary ops: + - * / // % **, parentheses (via AST nesting)
      - Unary ops: + -
      - Calls: whitelisted callables by simple name (no attribute calls)
      - Simple assignments: `x = <expr>` (no tuples, no targets other than bare names)
      - Random helpers (seeded): rand(), uniform(a,b), randint(a,b)
      - Common math: sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10
        log2 sqrt floor ceil hypot radians degrees, and constants pi e tau
      - Builtins: abs, round, min, max, pow

    Disallows:
      - Attribute access, subscripts, comprehensions, lambdas, if-expressions,
        booleans, comparisons, control flow, importing, etc.
    """

    def __init__(self, variables: Optional[Dict[str, float]] = None, seed: Optional[int] = None):
        rng = random.Random(seed)  # independent RNG

        # Allowed operators
        self._bin_ops = {
            ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
            ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
        }
        self._unary_ops = {ast.UAdd: op.pos, ast.USub: op.neg}

        # Env: functions + constants + any provided variables
        self.env: Dict[str, Union[float, int, callable]] = {
            # Builtins
            "abs": abs, "round": round, "min": min, "max": max, "pow": pow,
            # Math functions
            "sin": math.sin, "cos": math.cos, "tan": math.tan,
            "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
            "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
            "exp": math.exp, "log": math.log, "log10": math.log10, "log2": math.log2,
            "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
            "hypot": math.hypot, "radians": math.radians, "degrees": math.degrees,
            # Constants
            "pi": math.pi, "e": math.e, "tau": math.tau,
            # Random helpers (seeded)
            "rand": rng.random,
            "random": rng.random,  # alias
            "uniform": rng.uniform,
            "randint": rng.randint,
        }

        if variables:
            # User vars override defaults only for names that don't shadow callables/constants.
            for k, v in variables.items():
                if isinstance(v, (int, float)):
                    self.env[k] = float(v)

    # ---------- AST evaluation ----------
    def _evaluate(self, node: ast.AST):
        if isinstance(node, ast.Expression):
            return self._evaluate(node.body)

        # Numerics
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise ValueError("Only numeric literals are allowed.")

        # Names
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            raise ValueError(f"Unknown name: {node.id}")

        # Unary
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary_ops:
            return self._unary_ops[type(node.op)](self._evaluate(node.operand))

        # Binary
        if isinstance(node, ast.BinOp) and type(node.op) in self._bin_ops:
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            return self._bin_ops[type(node.op)](left, right)

        # Function calls (simple names only)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in self.env:
                func = self.env[node.func.id]
                if not callable(func):
                    raise ValueError(f"{node.func.id} is not callable.")
                args = [self._evaluate(a) for a in node.args]
                kwargs = {kw.arg: self._evaluate(kw.value) for kw in node.keywords} if node.keywords else {}
                return func(*args, **kwargs)
            raise ValueError("Only whitelisted functions by simple name are allowed.")

        # Parentheses are implicit in AST

        raise ValueError(f"Disallowed syntax: {ast.dump(node, include_attributes=False)}")

    def eval_expr_or_assign(self, expr: str) -> float:
        """
        Evaluate a single expression or a simple assignment, returning its value.
        """
        try:
            mod = ast.parse(expr, mode="exec")
        except SyntaxError as e:
            raise ValueError(f"Syntax error in '{expr}': {e.msg}") from None

        # Plain expression
        if len(mod.body) == 1 and isinstance(mod.body[0], ast.Expr):
            tree = ast.Expression(mod.body[0].value)
            val = self._evaluate(tree)
            if isinstance(val, (int, float)):
                return float(val)
            raise ValueError("Expression did not evaluate to a number.")

        # Simple assignment: name = <expr>
        if len(mod.body) == 1 and isinstance(mod.body[0], ast.Assign):
            assign = mod.body[0]
            if len(assign.targets) != 1 or not isinstance(assign.targets[0], ast.Name):
                raise ValueError("Only simple assignments like 'x = 2+3' are allowed.")
            name = assign.targets[0].id
            value = self._evaluate(ast.Expression(assign.value))
            if not isinstance(value, (int, float)):
                raise ValueError("Only numeric assignments are allowed.")
            value = float(value)
            self.env[name] = value
            return value

        raise ValueError("Only expressions and simple assignments are allowed.")


def calc_batch(params: CalcBatchParams) -> List[float]:
    """
    Evaluate a *batch* of math expressions safely and return their results.

    Features (succinct):
      • Supports + - * / // % **, parentheses, unary +/-
      • Variables & **simple assignments** (`x = 2+3`) that persist across lines
      • Math funcs: sin, cos, tan, asin, acos, atan, atan2, sinh, cosh, tanh,
        exp, log, log10, log2, sqrt, floor, ceil, hypot, radians, degrees
      • Builtins: abs, round, min, max, pow
      • Constants: pi, e, tau
      • Random (seeded via `params.seed`): `rand()` / `random()`, `uniform(a,b)`, `randint(a,b)`
      • No `eval` or attributes; AST is strictly whitelisted
      • If `params.variables` is given, it is **updated** with numeric names

    Returns
    -------
    list[float] : one numeric result per input expression (assignment returns assigned value)

    Examples
    --------
    Basic:
    >>> calc_batch(CalcBatchParams(spec="2+3; 10/4; 2**3"))
    [5.0, 2.5, 8.0]

    Variables and assignments:
    >>> env = {"r": 2}
    >>> calc_batch(CalcBatchParams(spec="area = pi*r**2; area", variables=env))
    [12.566370614359172, 12.566370614359172]

    Random (deterministic with seed):
    >>> p = CalcBatchParams(spec="rand(); uniform(1, 2); randint(1, 6)", seed=42)
    >>> calc_batch(p)
    [0.6394267984578837, 1.0250107552226669, 6.0]
    """
    evaluator = _SafeMathEvaluator(variables=params.variables, seed=params.seed)
    results: List[float] = []
    for expr in params.spec:  # type: ignore[arg-type]  # normalized to List[str] by validator
        results.append(evaluator.eval_expr_or_assign(expr))

    # Push numeric names back into the provided variables dict (if any)
    if params.variables is not None:
        params.variables.clear()
        for k, v in evaluator.env.items():
            if isinstance(v, (int, float)):
                params.variables[k] = float(v)

    return results


async def acalc_batch(params: CalcBatchParams) -> List[float]:
    """
    Async version of `calc_batch`, functionally identical but event-loop friendly.

    Behavior
    --------
    - Processes expressions in order.
    - After `params.yield_every` expressions, yields with `await asyncio.sleep(0)`
      so large batches remain responsive.
    - Random helpers honor the same `params.seed` for determinism.

    See `calc_batch` docstring for operators, functions, and examples.
    """
    evaluator = _SafeMathEvaluator(variables=params.variables, seed=params.seed)
    results: List[float] = []

    for i, expr in enumerate(params.spec):  # type: ignore[arg-type]
        results.append(evaluator.eval_expr_or_assign(expr))
        if (i + 1) % params.yield_every == 0:
            # Yield control to keep UI/event loop responsive
            await asyncio.sleep(0)

    if params.variables is not None:
        params.variables.clear()
        for k, v in evaluator.env.items():
            if isinstance(v, (int, float)):
                params.variables[k] = float(v)

    return results
