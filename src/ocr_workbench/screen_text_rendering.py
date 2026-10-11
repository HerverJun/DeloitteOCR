"""Render explicit, supported model math syntax into the table's plain text.

No arithmetic, Unicode sign normalization, or guessed OCR correction occurs.
The recognized source, style runs and every proposed display change remain
available. Unknown syntax and numeric powers retain the entire original cell.
This helper neither approves a value nor changes a human review decision.
"""
from copy import deepcopy
import hashlib
import re

VERSION = 'screen-explicit-math-text-v1'
SYMBOLS = {
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε',
    'theta': 'θ', 'lambda': 'λ', 'mu': 'μ', 'pi': 'π', 'rho': 'ρ',
    'sigma': 'σ', 'tau': 'τ', 'phi': 'φ', 'omega': 'ω', 'Delta': 'Δ',
    'Sigma': 'Σ', 'Omega': 'Ω', 'pm': '±', 'times': '×', 'cdot': '·',
    'ast': '*', 'dagger': '†', 'ddagger': '‡', 'leq': '≤', 'geq': '≥',
    'neq': '≠', 'infty': '∞',
}
WRAPPERS = {'text', 'mathrm', 'mathbf', 'mathit', 'operatorname'}
ESCAPES = {'%': '%', '&': '&', '#': '#', '_': '_', '$': '$', '{': '{', '}': '}'}


class UnsupportedMath(ValueError):
    pass


class Parser:
    def __init__(self, text):
        self.text, self.index, self.marked = text, 0, False

    def sequence(self, depth=0, grouped=False):
        if depth > 8:
            raise UnsupportedMath('math_nesting_limit')
        runs = []
        while self.index < len(self.text):
            char = self.text[self.index]
            if char == '}':
                if not grouped:
                    raise UnsupportedMath('unbalanced_math_group')
                self.index += 1
                return runs
            if char in '{}%$`':
                raise UnsupportedMath('unsupported_math_syntax')
            if char == '\\':
                runs.extend(self.command(depth))
            elif char in '^_':
                self.index += 1
                part = self.argument(depth)
                value = ''.join(run['text'] for run in part)
                allowed = r'[A-Za-z*†‡]+' if char == '^' else r'[A-Za-z0-9]+'
                if not re.fullmatch(allowed, value):
                    raise UnsupportedMath('numeric_power_or_unsupported_script')
                self.marked = True
                runs.append({'text': value, 'style': 'superscript' if char == '^' else 'subscript',
                             'source_runs': part})
            else:
                self.index += 1
                runs.append({'text': char, 'style': 'baseline'})
        if grouped:
            raise UnsupportedMath('unbalanced_math_group')
        return runs

    def argument(self, depth):
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1
        if self.index == len(self.text):
            raise UnsupportedMath('missing_math_argument')
        if self.text[self.index] == '{':
            self.index += 1
            part = self.sequence(depth + 1, True)
            if not part:
                raise UnsupportedMath('empty_math_argument')
            return part
        if self.text[self.index] == '\\':
            return self.command(depth)
        char = self.text[self.index]
        if char in '^_}%$`':
            raise UnsupportedMath('invalid_math_argument')
        self.index += 1
        return [{'text': char, 'style': 'baseline'}]

    def command(self, depth):
        self.index += 1
        if self.index == len(self.text):
            raise UnsupportedMath('unfinished_math_command')
        char = self.text[self.index]
        if char in ESCAPES:
            self.index += 1
            self.marked = True
            return [{'text': ESCAPES[char], 'style': 'baseline', 'command': '\\' + char}]
        match = re.match(r'[A-Za-z]+', self.text[self.index:])
        if not match:
            raise UnsupportedMath('unsupported_math_command')
        name = match[0]
        self.index += len(name)
        if name in SYMBOLS:
            self.marked = True
            return [{'text': SYMBOLS[name], 'style': 'baseline', 'command': '\\' + name}]
        if name not in WRAPPERS:
            raise UnsupportedMath('unsupported_math_command:' + name)
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1
        if self.index == len(self.text) or self.text[self.index] != '{':
            raise UnsupportedMath('missing_math_wrapper_group')
        self.index += 1
        runs = self.sequence(depth + 1, True)
        if not runs:
            raise UnsupportedMath('empty_math_wrapper_group')
        self.marked = True
        return [{'text': ''.join(run['text'] for run in runs), 'style': name, 'source_runs': runs}]


def render_cell_text(value):
    """A supported explicit math fragment supplies display syntax, not truth."""
    answer = {'before': value, 'after': value, 'changed': False, 'runs': [], 'reason': None}
    if not isinstance(value, str) or '$' not in value:
        return answer
    # Code, display equations, escaped currency and incomplete delimiters keep
    # their original literal meaning. No best-effort partial cell conversion.
    if len(value) > 32767 or '`' in value or '$$' in value or '\\$' in value:
        answer['reason'] = 'literal_code_currency_or_unsupported_delimiter'
        return answer
    if value.count('$') % 2 or value.count('$') > 128:
        answer['reason'] = 'unbalanced_or_excessive_math_fragments'
        return answer
    runs, cursor, marked = [], 0, False
    try:
        for match in re.finditer(r'\$([^$]*)\$', value):
            if match.start() > cursor:
                runs.append({'text': value[cursor:match.start()], 'style': 'baseline'})
            parser = Parser(match[1].strip())
            fragment = parser.sequence()
            # A balanced dollar pair alone could be currency. Require explicit
            # supported commands/scripts or a comparison expression.
            if not parser.marked and not re.fullmatch(r'[<>≤≥]\s*\d+(?:\.\d+)?', match[1].strip()):
                raise UnsupportedMath('ambiguous_dollar_literal')
            runs.extend(fragment)
            cursor, marked = match.end(), True
        runs.append({'text': value[cursor:], 'style': 'baseline'})
        rendered = ''.join(run['text'] for run in runs)
        digits = lambda text: ''.join(char for char in text if char.isdecimal())
        if digits(value) != digits(rendered):
            raise UnsupportedMath('numeric_digits_changed')
    except UnsupportedMath as error:
        answer['reason'] = str(error)
        return answer
    if marked and rendered != value:
        answer.update(after=rendered, changed=True, runs=runs,
                      reason='explicit_supported_math_display_syntax')
    return answer


def render_screen_table_text(raw):
    """Return a proposal copy; original model text and review flags survive."""
    result = deepcopy(raw)
    changes, rejected = [], []
    for ti, table in enumerate(result.get('tables', [])):
        for cell in table.get('cells', []):
            proposal = render_cell_text(cell.get('text'))
            position = {'table': ti, **{key: cell.get(key, 1) for key in ('row', 'column', 'row_span', 'column_span')}}
            if proposal['changed']:
                change = {**position, **proposal, 'policy_version': VERSION,
                          'recognized_text_sha256': hashlib.sha256(proposal['before'].encode()).hexdigest(),
                          'human_review': 'not_performed', 'automatic_delivery_authorized': False}
                cell['text'] = proposal['after']
                cell.setdefault('provenance', []).append(deepcopy(change))
                changes.append(change)
            elif proposal['reason']:
                rejected.append({**position, 'reason': proposal['reason']})
    result.setdefault('screen', {})['table_text_rendering'] = {
        'version': VERSION, 'changes': changes, 'rejected': rejected,
        'original_model_text_unchanged': result.get('text') == raw.get('text'),
        'image_version': raw.get('project_image_version'),
        'input_sha256': raw.get('screen', {}).get('input_sha256'),
        'semantic_ocr_corrections': 0, 'review_decision_changed': False,
        'plain_text_representation': 'Scripts retain their characters, as in existing HTML table text; style runs and original syntax remain in provenance.'}
    return result
