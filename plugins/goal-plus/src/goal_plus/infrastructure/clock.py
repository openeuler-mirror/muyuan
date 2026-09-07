"""Existing UTC wire timestamps at the system clock boundary."""

import calendar
import time


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_timestamp_from_epoch(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def parse_utc_timestamp(timestamp: str) -> float:
    return float(calendar.timegm(time.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")))


class SystemClock:
    def monotonic(self) -> float:
        return time.perf_counter()

    def now(self) -> float:
        return time.time()

    def timestamp(self) -> str:
        return utc_timestamp()

    def timestamp_from_epoch(self, epoch: float) -> str:
        return utc_timestamp_from_epoch(epoch)
