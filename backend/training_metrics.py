"""Parse numeric training metrics without truncating compact step counters."""
import math
import re

NUMBER = r'[+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
LOSS_RE = re.compile(r'\b(?:train_loss|loss)\s*[=:]?\s*(' + NUMBER + r')(?![\w.])', re.I)
STEP_RE = re.compile(r'\b(?:global_step|iterations?|steps?|epochs?)\s*[=:]?\s*(' + NUMBER + r')([KMBTQ]?)(?![\w.])', re.I)
MULTIPLIER = {'': 1, 'K': 10**3, 'M': 10**6, 'B': 10**9, 'T': 10**12, 'Q': 10**15}


def numeric_step(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a training step')
    match = re.fullmatch(r'(' + NUMBER + r')([KMBTQ]?)', str(value).strip(), re.I)
    if not match:
        raise ValueError('invalid training step')
    result = float(match[1].replace(',', '')) * MULTIPLIER[match[2].upper()]
    if not math.isfinite(result) or result < 0 or not result.is_integer():
        raise ValueError('invalid training step')
    return int(result)


def parse_metric(line):
    loss = LOSS_RE.search(line)
    if not loss:
        return None
    value = float(loss[1].replace(',', ''))
    if not math.isfinite(value):
        return None
    step = STEP_RE.search(line)
    return {'value': value, 'step': numeric_step(step[1] + step[2]) if step else None}
