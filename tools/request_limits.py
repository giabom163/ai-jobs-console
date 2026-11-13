"""Shared bounds; a configured maximum is not permission to make requests."""

MAX_ROUND_REQUESTS = 200
DEFAULT_TASK_REQUESTS = 10
DEFAULT_AUTHORIZATION_REQUESTS = 30
MIN_HOST_INTERVAL = 11
MAX_INFLIGHT = 5


def request_count(value, name='request limit'):
    if type(value) is not int or not 1 <= value <= MAX_ROUND_REQUESTS:
        raise ValueError(f'{name} must be an integer in 1..{MAX_ROUND_REQUESTS}')
    return value
