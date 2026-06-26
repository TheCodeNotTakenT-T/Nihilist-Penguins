import time
import random
import functools
from typing import Tuple, Type
import requests

from src.utils.logger import get_logger

logger = get_logger(__name__)


def retry_with_backoff(
    exceptions: Tuple[Type[Exception], ...] = (
        requests.exceptions.RequestException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ),
    max_attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 120.0,
    retry_on_status: Tuple[int, ...] = (429, 500, 502, 503, 504),
):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, requests.Response):
                        if result.status_code in retry_on_status:
                            delay = _compute_delay(attempt, base_delay, max_delay, result)
                            logger.warning(
                                f"[{func.__name__}] HTTP {result.status_code} "
                                f"on attempt {attempt}/{max_attempts}. Retrying in {delay:.1f}s..."
                            )
                            time.sleep(delay)
                            last_exception = requests.exceptions.HTTPError(
                                f"HTTP {result.status_code}"
                            )
                            continue
                    return result
                except exceptions as exc:
                    last_exception = exc
                    delay = _compute_delay(attempt, base_delay, max_delay)
                    logger.warning(
                        f"[{func.__name__}] {type(exc).__name__} on attempt "
                        f"{attempt}/{max_attempts}. Retrying in {delay:.1f}s..."
                    )
                    if attempt < max_attempts:
                        time.sleep(delay)
            logger.error(f"[{func.__name__}] All {max_attempts} attempts exhausted.")
            raise last_exception
        return wrapper
    return decorator


def _compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    response: requests.Response = None
) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    delay = min(base_delay ** attempt, max_delay)
    return delay + random.uniform(0, delay * 0.1)
