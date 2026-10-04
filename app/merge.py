from dataclasses import dataclass

FIELDS = ("title", "content", "tags", "archived")


@dataclass(frozen=True)
class Merge:
    data: dict
    conflicts: dict


def three_way_merge(base: dict, current: dict, changes: dict) -> Merge:
    """Compare values, not clocks; omitted fields and base-equal values carry no new intent."""
    merged = current.copy()
    conflicts = {}
    for field, proposed in changes.items():
        if proposed == base[field]:
            continue
        if current[field] == base[field] or proposed == current[field]:
            merged[field] = proposed
        else:
            conflicts[field] = {
                "base": base[field],
                "server": current[field],
                "client": proposed,
            }
    return Merge(merged, conflicts)


def changed_fields(current: dict, proposed: dict) -> list[str]:
    return [field for field in FIELDS if current[field] != proposed[field]]
