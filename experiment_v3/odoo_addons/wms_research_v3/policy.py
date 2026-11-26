"""Pure policy on delivered observations. No fixture or model clock access."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class Address:
    name: str
    group: int
    quarantine: bool
    distance: float

def enabled(mask, number):
    if type(mask) is not int or not 0 <= mask <= 127 or not 1 <= number <= 7:
        raise ValueError("invalid mask or module")
    return bool(mask & (1 << (number - 1)))

def observed_count(now, snapshot, delivered_balance, capacity):
    if snapshot is not None:
        stamp, count = snapshot
        if 0 <= now - stamp <= 60 and type(count) is int and 0 <= count <= capacity:
            return count, "sensor"
    return max(0, delivered_balance), "delivered_movements"

def choose_address(addresses, group, released, mask):
    valid = (a for a in addresses if (not a.quarantine and a.group == group if released else a.quarantine))
    return min(valid, key=(lambda a: (a.distance, a.name)) if enabled(mask,4) else (lambda a: a.name), default=None)

def pick_job(jobs, now, count, capacity, predictions, mask):
    """A job is (registration, pallet, group, seconds_of_empty_and_loaded_travel).
    FIFO in ordinary conditions. Under pressure choose among existing jobs only.
    Expected work added during the trip is forecast arrivals * trip/1800.
    """
    if not jobs:
        return None
    fifo = min(jobs, key=lambda j:j[:2])
    if not enabled(mask,4) or count < capacity / 2:
        return fifo
    def finish(j):
        service = j[3]
        additional = max(0, predictions[j[2]])*service/1800
        return (service + service*additional - max(0,now-j[0]), j[1])
    return min(jobs, key=finish)

