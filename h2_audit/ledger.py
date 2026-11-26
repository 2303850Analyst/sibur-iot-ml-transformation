from collections import Counter
import hashlib
import json

KINDS = ('duplicate_receipts', 'illegal_moves', 'illegal_release', 'negative_stock')
PASSIVE = {'scanner_miss', 'tsd_fallback', 'identity_exception', 'preparation_released',
           'preparation_started', 'preparation_completed'}


class Finding(Exception):
    def __init__(self, kind, reason):
        super().__init__(reason)
        self.kind = kind


def require(condition, kind, reason):
    if not condition:
        raise Finding(kind, reason)


class Ledger:
    def __init__(self, initial):
        self.inbound = initial['inbound']
        self.outbound = initial['outbound']
        self.input_hash = hashlib.sha256(json.dumps([self.inbound, self.outbound], separators=(',', ':')).encode()).hexdigest()
        self.held = set(initial['held'])
        self.expected_release = set(self.held)
        self.cells = {cell: (None, sku) for cell, sku in initial['cells']}
        self.initial_count = len(self.cells)
        self.available = Counter(sku for pallet, sku in self.cells.values())
        self.location = {}
        self.observed = set()
        self.registered = set()
        self.reservations = {}
        self.moves = {}
        self.confirming = {}
        self.confirmed = set()
        self.first_confirm = set()
        self.request_index = 0
        self.arrivals = 0
        self.time = -1
        self.count = Counter()

    def snapshot(self, row):
        pallet = row.get('pallet')
        cell = row.get('cell')
        return dict(location=self.location.get(pallet), registered=pallet in self.registered,
                    quality='hold' if pallet in self.held else 'released', destination_contents=self.cells.get(cell),
                    reservation=self.reservations.get(cell), moving=self.moves.get(pallet),
                    confirming=self.confirming.get(pallet))

    def apply(self, row):
        event, pallet, time = row['event'], row['pallet'], row['time']
        require(time >= self.time, 'trace_integrity', 'time order')
        self.time = time
        self.count[event] += 1
        if event in PASSIVE:
            return
        if pallet is not None:
            require(type(pallet) is int and 0 <= pallet < len(self.inbound), 'trace_integrity', 'unknown pallet')
            arrival, sku, source = self.inbound[pallet]
        pos = self.location.get(pallet)
        if 'quality' in row:
            require(row['quality'] == ('hold' if pallet in self.held else 'released'),
                    'illegal_release', 'quality disagrees with independent release ledger')
        if event == 'physical_arrival':
            require(pallet == self.arrivals and pos is None and (row['sku'], row['source_row'], time) ==
                    (sku, source, round(arrival, 6)), 'trace_integrity', 'arrival/input mismatch')
            self.location[pallet] = 'entry'
            self.arrivals += 1
        elif event == 'physical_buffer':
            require(pos == 'entry', 'negative_stock', 'entry has no pallet to transfer')
            self.location[pallet] = 'buffer'
        elif event == 'external_release':
            require(pos is not None and pallet in self.held and abs(time - (arrival + 7200)) <= .000001,
                    'illegal_release', 'external release absent from schedule, repeated or premature')
            self.held.remove(pallet)
        elif event in ('observation_received', 'idempotent_duplicate'):
            require(pos is not None and (event != 'idempotent_duplicate' or pallet in self.observed),
                    'trace_integrity', 'unmatched observation')
            self.observed.add(pallet)
        elif event == 'register':
            require(pallet not in self.registered, 'duplicate_receipts', 'second committed receipt')
            require(pos == 'buffer' and pallet in self.observed and row['quantity'] == 1 and row['lot'] == f'LOT-{pallet}',
                    'illegal_moves', 'receipt lacks matching physical unit/identity')
            self.registered.add(pallet)
        elif event == 'reserve_and_start':
            cell = row['cell']
            zone = 2 if pallet in self.held else sku // 10
            require(pallet in self.registered and cell not in self.reservations and cell not in self.cells
                    and 0 <= cell < 5329 and cell // 2600 == zone and pallet not in self.moves
                    and pallet not in self.confirming, 'illegal_moves', 'invalid reservation or prior receipt')
            if pos != 'buffer':
                require(isinstance(pos, int) and self.cells.get(pos) == (pallet, sku),
                        'negative_stock', 'no balance at dispatch source')
                require(pos >= 5200 and pallet in self.confirmed and pallet not in self.held,
                        'illegal_moves', 'final transfer requires released quarantine stock')
                self.confirmed.remove(pallet)
            self.moves[pallet] = (pos, cell)
            self.reservations[cell] = pallet
            self.location[pallet] = 'transit'
        elif event == 'physical_move_end':
            require(pos == 'transit' and pallet in self.moves, 'negative_stock', 'transit has no pallet')
            origin, cell = self.moves[pallet]
            require(row['cell'] == cell and self.reservations.get(cell) == pallet and cell not in self.cells,
                    'illegal_moves', 'wrong or occupied destination')
            if origin != 'buffer':
                require(self.cells.get(origin) == (pallet, sku), 'negative_stock', 'quarantine source lock lost')
                del self.cells[origin]
            del self.moves[pallet], self.reservations[cell]
            self.cells[cell] = (pallet, sku)
            self.location[pallet] = cell
            self.confirming[pallet] = cell
        elif event == 'confirm_place':
            cell = row['cell']
            require(self.confirming.get(pallet) == cell and pos == cell and self.cells.get(cell) == (pallet, sku)
                    and row['primary'] == (pallet not in self.first_confirm), 'illegal_moves', 'confirmation mismatch')
            require(cell >= 5200 or pallet not in self.held, 'illegal_release', 'held stock confirmed in ordinary zone')
            del self.confirming[pallet]
            self.confirmed.add(pallet)
            self.first_confirm.add(pallet)
            if cell < 5200:
                self.available[sku] += 1
        elif event in ('outbound', 'unfilled_outbound'):
            sku = row['sku']
            if event == 'outbound':
                cell = row['cell']
                require(self.cells.get(cell) == (pallet, sku) and self.available[sku] > 0,
                        'negative_stock', 'outbound source lacks unit of requested SKU')
                require(cell < 5200 and (pallet is None or
                        (pos == cell and pallet not in self.held and pallet in self.confirmed)),
                        'illegal_moves', 'outbound before confirmed release')
                del self.cells[cell]
                self.available[sku] -= 1
                if pallet is not None:
                    self.location[pallet] = 'departed'
                    self.confirmed.remove(pallet)
            else:
                require(pallet is None and self.available[sku] == 0, 'trace_integrity', 'unfilled despite available SKU')
            require(self.request_index < len(self.outbound), 'trace_integrity', 'extra outbound request')
            expected_time, expected_sku = self.outbound[self.request_index]
            require((round(expected_time, 6), expected_sku) == (time, sku), 'trace_integrity', 'outbound/input mismatch')
            self.request_index += 1
        else:
            raise Finding('trace_integrity', 'unknown event '+event)

    def finish(self):
        require(self.arrivals == len(self.inbound) and self.request_index == len(self.outbound),
                'trace_integrity', 'incomplete arrival/outbound stream')
        require(not any(self.inbound[pallet][0]+7200 < 32*86400 for pallet in self.held),
                'illegal_release', 'scheduled release missing')
        actual_available = Counter(sku for cell, (pallet, sku) in self.cells.items() if cell < 5200
                                   and (pallet is None or pallet in self.confirmed))
        require(actual_available == self.available, 'negative_stock', 'stock disagrees with cell balances')
        physical_count = sum(pos != 'departed' for pos in self.location.values()) + sum(
            pallet is None for pallet, sku in self.cells.values())
        require(physical_count == self.initial_count + self.arrivals - self.count['outbound'],
                'negative_stock', 'physical conservation failed')
