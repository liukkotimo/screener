"""
Screening profiles: YAML -> validated conditions.

    name: example
    description: optional text
    steps:
      - {attr: exchange, op: in, value: [HEL, STO, NYQ]}
      - {metric: market_cap_eur, op: ">", value: 1000000000}
      - label: big fall within 1 or 3 years      # optional label on any step
        any_of:
          - {metric: perf_1y, op: "<", value: -0.33}
          - {metric: perf_3y, op: "<", value: -0.33}

Column names are checked against whitelists before any SQL is built; values only ever become SQL parameters.
"""
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

# item columns a profile may filter on
ATTRIBUTES = ('ticker', 'name', 'type', 'exchange', 'exchange_name', 'country', 'currency', 'sector',
              'industry', 'source')

COMPARISONS = {'<': '<', '<=': '<=', '>': '>', '>=': '>=', '==': '=', '!=': '!='}
OPERATORS = (*COMPARISONS, 'between', 'in', 'not_in')
# metrics holding an ISO date text; <, >= etc. take a date string for these
DATE_METRICS = ('last_price_date', 'fiscal_year_end')


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Leaf:
    source: str   # 'attr' (item column) or 'metric' (item_metrics column)
    field: str
    op: str
    value: object

    def leaves(self):
        return [self]

    def to_dict(self):
        return {self.source: self.field, 'op': self.op, 'value': self.value}

    def describe(self):
        return f'{self.field} {self.op} {format_value(self.value)}'

    def sql(self) -> tuple[str, list]:
        """SQL expression that is 1, 0, or NULL when the column is NULL (SQL three-valued logic)."""
        col = f'{"i" if self.source == "attr" else "m"}.{self.field}'  # field is whitelisted
        if self.op in COMPARISONS:
            return f'({col} {COMPARISONS[self.op]} ?)', [self.value]
        if self.op == 'between':
            return f'({col} BETWEEN ? AND ?)', list(self.value)
        marks = ', '.join('?' * len(self.value))
        return f'({col} {"IN" if self.op == "in" else "NOT IN"} ({marks}))', list(self.value)


@dataclass(frozen=True)
class Group:
    kind: str     # 'any_of' (OR) or 'all_of' (AND)
    children: tuple

    def leaves(self):
        return [leaf for c in self.children for leaf in c.leaves()]

    def to_dict(self):
        return {self.kind: [c.to_dict() for c in self.children]}

    def describe(self):
        joiner = ' OR ' if self.kind == 'any_of' else ' AND '
        return '(' + joiner.join(c.describe() for c in self.children) + ')'

    def sql(self) -> tuple[str, list]:
        parts = [c.sql() for c in self.children]
        joiner = ' OR ' if self.kind == 'any_of' else ' AND '
        return '(' + joiner.join(p[0] for p in parts) + ')', [v for p in parts for v in p[1]]


@dataclass(frozen=True)
class Step:
    no: int
    condition: Leaf | Group
    label: str | None = None

    @property
    def definition_json(self) -> str:
        return json.dumps(self.condition.to_dict(), sort_keys=True)

    def describe(self) -> str:
        text = self.condition.describe()
        return f'{self.label}: {text}' if self.label else text


@dataclass(frozen=True)
class Profile:
    name: str
    steps: tuple
    yaml_text: str
    path: str | None = None
    description: str | None = None


def format_value(v) -> str:
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (list, tuple)):
        return '[' + ', '.join(format_value(x) for x in v) + ']'
    if isinstance(v, float):
        return f'{v:,.0f}' if abs(v) >= 1e6 else f'{v:.4g}'
    if isinstance(v, int):
        return f'{v:,}' if abs(v) >= 1e6 else str(v)
    return str(v)


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_condition(raw, where: str, metrics: set[str]):
    if not isinstance(raw, dict):
        raise ProfileError(f'{where}: expected a mapping, got {raw!r}')
    groups = [k for k in ('any_of', 'all_of') if k in raw]
    if groups:
        if len(groups) > 1 or set(raw) - {groups[0], 'label'}:
            raise ProfileError(f'{where}: a group step has exactly one of any_of/all_of (plus optional label)')
        children = raw[groups[0]]
        if not isinstance(children, list) or not children:
            raise ProfileError(f'{where}: {groups[0]} needs a non-empty list')
        return Group(groups[0], tuple(_parse_condition(c, f'{where}.{groups[0]}[{i + 1}]', metrics)
                                      for i, c in enumerate(children)))

    sources = [k for k in ('attr', 'metric') if k in raw]
    if len(sources) != 1:
        raise ProfileError(f'{where}: needs exactly one of attr / metric / any_of / all_of')
    extra = set(raw) - {sources[0], 'op', 'value', 'label'}
    if extra:
        raise ProfileError(f'{where}: unknown key(s) {sorted(extra)}')
    source, field, op, value = sources[0], raw[sources[0]], raw.get('op'), raw.get('value')

    known = ATTRIBUTES if source == 'attr' else sorted(metrics)
    if field not in known:
        raise ProfileError(f'{where}: unknown {source} {field!r}. Known: {", ".join(known)}')
    if op not in OPERATORS:
        raise ProfileError(f'{where}: unknown op {op!r}. Known: {", ".join(OPERATORS)}')
    if op == 'between':
        if not (isinstance(value, list) and len(value) == 2 and all(_is_number(v) for v in value)
                and value[0] <= value[1]):
            raise ProfileError(f'{where}: between needs [low, high] numbers with low <= high, got {value!r}')
    elif op in ('in', 'not_in'):
        if not (isinstance(value, list) and value and all(isinstance(v, (str, int, float)) for v in value)):
            raise ProfileError(f'{where}: {op} needs a non-empty list, got {value!r}')
    elif not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ProfileError(f'{where}: {op} needs a single number or text, got {value!r}')
    if source == 'metric' and op in ('<', '<=', '>', '>=') and not _is_number(value) and field not in DATE_METRICS:
        raise ProfileError(f'{where}: metric {field} {op} needs a number, got {value!r}')
    return Leaf(source, field, op, tuple(value) if isinstance(value, list) else value)


def parse_profile(text: str, metrics: set[str], path: str | None = None) -> Profile:
    """Parse and validate profile YAML. metrics = allowed item_metrics column names."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProfileError(f'invalid YAML: {e}') from e
    if not isinstance(raw, dict) or not raw.get('name') or not isinstance(raw.get('steps'), list) or not raw['steps']:
        raise ProfileError('profile needs a "name" and a non-empty "steps" list')
    extra = set(raw) - {'name', 'description', 'steps'}
    if extra:
        raise ProfileError(f'unknown top-level key(s) {sorted(extra)}')
    steps = tuple(Step(i, _parse_condition(s, f'step {i}', metrics), s.get('label') if isinstance(s, dict) else None)
                  for i, s in enumerate(raw['steps'], 1))
    return Profile(str(raw['name']), steps, text, path, raw.get('description'))


def load_profile(path: str | Path, metrics: set[str]) -> Profile:
    return parse_profile(Path(path).read_text(), metrics, str(path))
