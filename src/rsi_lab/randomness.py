"""Position-addressed streams; no shared mutable RNG across experimental arms."""

from .contracts import Coupling, RandomPlan, require_count, require_id
from .errors import ValidationError
from .serialization import digest


def derive_seed(plan: RandomPlan, *, root_id: str, pair_id: str, replicate: int,
                attempt_index: int, stream: str, semantic_position: str, arm: str) -> int:
    if stream not in ("generation", "task", "q", "v"):
        raise ValidationError(f"Unregistered random stream: {stream}")
    for name, value in (("root_id", root_id), ("pair_id", pair_id),
                        ("semantic_position", semantic_position), ("arm", arm)):
        require_id(value, name)
    require_count(replicate, "replicate")
    require_count(attempt_index, "attempt_index")
    key = dict(algorithm=plan.algorithm, master_seed=plan.master_seed, root_id=root_id,
               pair_id=pair_id, replicate=replicate, attempt_index=attempt_index,
               stream=stream, semantic_position=semantic_position)
    if getattr(plan, stream) == Coupling.INDEPENDENT_REQUESTS:
        key["arm"] = arm
    return int(digest(key), 16)

