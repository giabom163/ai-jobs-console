"""Business lifecycle is independent of source claims and HTTP accessibility."""
STATUSES = frozenset({'open', 'review', 'verification_pending', 'closed', 'excluded', 'merged', 'stale'})
PUBLIC_STATUSES = frozenset({'open', 'review'})
REVIEWABLE_STATUSES = frozenset({'open', 'review', 'verification_pending'})


def initial_state(source, tier):
    if tier == 'none':
        return 'excluded'
    return 'verification_pending' if source == 'official:meituan' else 'review'


def classification_state(current, tier):
    if current not in STATUSES:
        raise ValueError('unsupported lifecycle state')
    if tier == 'none' and current not in {'closed', 'merged'}:
        return 'excluded'
    return current
