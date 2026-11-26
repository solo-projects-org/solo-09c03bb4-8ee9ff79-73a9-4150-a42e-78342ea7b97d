"""Whole-litre rounding and correction planning.

All volumes are integers in millilitres.  Archive volumes are integers in
litres.  A whole-litre archive is valid for a millilitre table when every
contiguous interval [i, j] satisfies::

    abs(sum(litres[i:j+1]) * 1000 - sum(ml[i:j+1])) < 1000

The implementation keeps the cumulative rounding error inside a closed
window of 1000 integer millilitre levels, e.g. [0, 999].  A window of width
999 guarantees the strict "< 1000 ml" bound for every interval.

Key fact used by correction: when the archive is fixed for a prefix of the
day, the prefix is extendable to a valid full day iff the cumulative error
values of that prefix span less than 1000 ml.  If they do, the remaining
minutes can always be rounded by subtracting the millilitre remainder and
adding 1000 whenever the running value falls below the prefix minimum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


ML_PER_LITRE = 1000


class AlgorithmError(ValueError):
    """Raised when an archive cannot be built or verified."""


@dataclass(frozen=True)
class Verification:
    ok: bool
    max_interval_error_ml: int
    worst_start_index: int | None
    worst_end_index: int | None
    total_ml: int
    total_litres: int
    total_error_ml: int
    message: str = ""


def _validate_ml(ml: Sequence[int]) -> None:
    if not ml:
        raise AlgorithmError("毫升原表为空")
    for value in ml:
        if not isinstance(value, int) or isinstance(value, bool):
            raise AlgorithmError("毫升值必须为整数")
        if value < 0:
            raise AlgorithmError("毫升值不能为负数")


def build_initial_archive(ml: Sequence[int]) -> list[int]:
    """Build a valid whole-litre archive for a complete day.

    The running error is kept in [0, 999] ml.  Each minute is therefore
    rounded down or up, and never moves by more than 999 ml.
    """
    _validate_ml(ml)
    litres: list[int] = []
    cumulative_error = 0
    for value in ml:
        whole, remainder = divmod(value, ML_PER_LITRE)
        cumulative_error -= remainder
        if cumulative_error < 0:
            cumulative_error += ML_PER_LITRE
            whole += 1
        litres.append(whole)
    result = verify_archive(ml, litres)
    if not result.ok:
        # This should be unreachable; keep the invariant checked.
        raise AlgorithmError(f"首次归档生成失败：{result.message}")
    return litres


def max_unchanged_prefix(old_archive: Sequence[int], new_ml: Sequence[int]) -> int:
    """Largest number of leading minutes that can keep ``old_archive``.

    Returns ``p`` in [0, n].  Minutes ``0 .. p-1`` keep their old litres and
    minute ``p`` is the first minute that must be re-published (if p < n).
    """
    if len(old_archive) != len(new_ml):
        raise AlgorithmError("现行档与上传全表的分钟数不一致")
    if not new_ml:
        raise AlgorithmError("毫升原表为空")

    cumulative_error = 0
    minimum = 0
    maximum = 0
    kept = 0
    for index, value in enumerate(new_ml):
        cumulative_error += old_archive[index] * ML_PER_LITRE - value
        next_min = min(minimum, cumulative_error)
        next_max = max(maximum, cumulative_error)
        if next_max - next_min >= ML_PER_LITRE:
            break
        minimum, maximum = next_min, next_max
        kept = index + 1
    return kept


@dataclass(frozen=True)
class CorrectionPlan:
    p_index: int
    p_minute: int | None
    old_archive: tuple[int, ...]
    new_archive: tuple[int, ...]
    suffix: tuple[int, ...]
    new_ml: tuple[int, ...]
    verification: Verification


def build_correction_plan(
    old_archive: Sequence[int],
    new_ml: Sequence[int],
    minutes: Sequence[int],
) -> CorrectionPlan:
    """Build a plan that preserves the longest feasible old prefix.

    ``minutes`` contains the original minute numbers, used only to report the
    actual minute at which re-publishing starts.
    """
    if len(old_archive) != len(new_ml):
        raise AlgorithmError("现行档与上传全表的分钟数不一致")
    if len(minutes) != len(new_ml):
        raise AlgorithmError("分钟号与毫升值的数量不一致")
    _validate_ml(new_ml)

    p_index = max_unchanged_prefix(old_archive, new_ml)
    prefix = list(old_archive[:p_index])

    # Recompute the error range of the forced prefix.  It is valid by
    # construction of max_unchanged_prefix.
    cumulative_error = 0
    minimum = 0
    for index in range(p_index):
        cumulative_error += old_archive[index] * ML_PER_LITRE - new_ml[index]
        minimum = min(minimum, cumulative_error)

    suffix: list[int] = []
    for index in range(p_index, len(new_ml)):
        value = new_ml[index]
        whole, remainder = divmod(value, ML_PER_LITRE)
        cumulative_error -= remainder
        if cumulative_error < minimum:
            cumulative_error += ML_PER_LITRE
            whole += 1
        suffix.append(whole)

    new_archive = prefix + suffix
    verification = verify_archive(new_ml, new_archive)
    if not verification.ok:
        raise AlgorithmError(f"补正方案生成失败：{verification.message}")

    p_minute = minutes[p_index] if p_index < len(minutes) else None
    return CorrectionPlan(
        p_index=p_index,
        p_minute=p_minute,
        old_archive=tuple(old_archive),
        new_archive=tuple(new_archive),
        suffix=tuple(suffix),
        new_ml=tuple(new_ml),
        verification=verification,
    )


def verify_archive(ml: Sequence[int], litres: Sequence[int]) -> Verification:
    """Check every contiguous interval, plus per-minute rounding.

    ``max_interval_error_ml`` is the maximum over all contiguous intervals of
    ``abs(sum(litres) * 1000 - sum(ml))``.  It must be strictly below 1000.
    """
    total_ml = sum(ml)
    total_litres = sum(litres)
    if len(ml) != len(litres):
        return Verification(
            False,
            0,
            None,
            None,
            total_ml,
            total_litres,
            total_litres * ML_PER_LITRE - total_ml,
            "毫升表与整升档长度不一致",
        )
    if not ml:
        return Verification(
            True, 0, None, None, 0, 0, 0, "空表"
        )

    cumulative = 0
    # Prefix sums c[0..n], where c[0] = 0 and c[k] = sum of minute errors
    # for minutes 0 .. k-1.  The error of interval [i, j] is c[j+1] - c[i].
    running_min = 0
    running_min_index = 0
    running_max = 0
    running_max_index = 0
    worst_gap = 0
    worst_start: int | None = None
    worst_end: int | None = None

    for index, (value_ml, value_l) in enumerate(zip(ml, litres)):
        failure = None
        if not isinstance(value_l, int) or isinstance(value_l, bool):
            failure = f"第 {index} 个整升值不是整数"
        elif value_l < 0:
            failure = f"第 {index} 个整升值是负数"
        elif abs(value_l * ML_PER_LITRE - value_ml) >= ML_PER_LITRE:
            failure = (
                f"第 {index} 分钟的单分钟偏差为 "
                f"{value_l * ML_PER_LITRE - value_ml} 毫升，不满足 < 1000"
            )
        if failure is not None:
            return Verification(
                False,
                worst_gap,
                worst_start,
                worst_end,
                total_ml,
                total_litres,
                total_litres * ML_PER_LITRE - total_ml,
                failure,
            )

        cumulative += value_l * ML_PER_LITRE - value_ml

        gap_high = cumulative - running_min  # largest positive interval error
        gap_low = running_max - cumulative  # largest negative interval error
        if gap_high > worst_gap:
            worst_gap = gap_high
            worst_start = running_min_index
            worst_end = index
        if gap_low > worst_gap:
            worst_gap = gap_low
            worst_start = running_max_index
            worst_end = index

        if cumulative < running_min:
            running_min = cumulative
            running_min_index = index + 1
        if cumulative > running_max:
            running_max = cumulative
            running_max_index = index + 1

    if worst_gap >= ML_PER_LITRE:
        return Verification(
            False,
            worst_gap,
            worst_start,
            worst_end,
            total_ml,
            total_litres,
            total_litres * ML_PER_LITRE - total_ml,
            f"存在连续时段偏差 {worst_gap} 毫升，不满足 < 1000",
        )

    return Verification(
        True,
        worst_gap,
        worst_start,
        worst_end,
        total_ml,
        total_litres,
        total_litres * ML_PER_LITRE - total_ml,
        "所有连续时段偏差均小于 1000 毫升",
    )


def independent_interval_check(ml: Sequence[int], litres: Sequence[int]) -> int:
    """O(n^2) independent checker used by tests and diagnostics.

    Returns the maximum absolute contiguous-interval error in millilitres.
    """
    if len(ml) != len(litres):
        raise AlgorithmError("长度不一致")
    worst = 0
    for start in range(len(ml)):
        running = 0
        for finish in range(start, len(ml)):
            running += litres[finish] * ML_PER_LITRE - ml[finish]
            worst = max(worst, abs(running))
    return worst
