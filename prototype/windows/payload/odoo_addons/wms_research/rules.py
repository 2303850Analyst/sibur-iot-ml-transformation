def eligible(quality, group, cell_zone, cell_group):
    if quality != 'released':
        return cell_zone == 'quarantine'
    return cell_zone == 'storage' and group == cell_group


def identity_matches(observed, expected):
    return all(observed.get(name) == expected[name] for name in ('sku', 'lot', 'quantity'))
