import argparse
import json
import math
import os
import random
import statistics


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def bootstrap_mean_ci(values, samples=20000, seed=20260606):
    if not values:
        return [0.0, 0.0]
    rng = random.Random(seed)
    size = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(size)] for _ in range(size)) / size)
    return [percentile(means, 0.025), percentile(means, 0.975)]


def sign_flip_p_value(values, samples=50000, seed=20260607):
    nonzero = [value for value in values if value != 0]
    if not nonzero:
        return 1.0
    observed = abs(sum(nonzero) / len(nonzero))
    rng = random.Random(seed)
    extreme = 1
    for _ in range(samples):
        simulated = sum(value if rng.random() < 0.5 else -value for value in nonzero)
        if abs(simulated / len(nonzero)) >= observed - 1e-12:
            extreme += 1
    return extreme / (samples + 1)


def exact_sign_test(positive, negative):
    count = positive + negative
    if count == 0:
        return 1.0
    tail = min(positive, negative)
    probability = sum(math.comb(count, index) for index in range(tail + 1)) / (2 ** count)
    return min(1.0, 2.0 * probability)


def summarize(values):
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    zero = sum(value == 0 for value in values)
    mean_value = statistics.mean(values) if values else 0.0
    median_value = statistics.median(values) if values else 0.0
    ci_low, ci_high = bootstrap_mean_ci(values)
    flip_p = sign_flip_p_value(values)
    sign_p = exact_sign_test(positive, negative)

    if ci_low > 0 and flip_p < 0.05:
        verdict = "显著增强"
    elif mean_value > 0 and positive > negative:
        verdict = "提升趋势"
    elif mean_value < 0 and negative > positive:
        verdict = "退化趋势"
    else:
        verdict = "证据不足"

    return {
        "count": len(values),
        "positive": positive,
        "negative": negative,
        "zero": zero,
        "sum": round(sum(values), 3),
        "mean": round(mean_value, 3),
        "median": round(median_value, 3),
        "bootstrap_mean_ci95": [round(ci_low, 3), round(ci_high, 3)],
        "sign_flip_p_value": round(flip_p, 6),
        "sign_test_p_value": round(sign_p, 6),
        "verdict": verdict,
    }


def load_matchups(run_dirs):
    matchups = {}
    metadata = []
    for run_dir in run_dirs:
        run_dir = os.path.abspath(run_dir)
        run_data = load_json(os.path.join(run_dir, "run.json"))
        statistics_data = load_json(os.path.join(run_dir, "pool_statistics.json"))
        metadata.append({
            "run_dir": run_dir,
            "candidate": run_data.get("candidate", {}),
            "config": run_data.get("config", {}),
        })
        for row in statistics_data.get("matchups", []):
            opponent = row["opponent"]
            if opponent in matchups:
                raise ValueError("对手 {} 在多个运行目录中重复出现".format(opponent))
            matchups[opponent] = {
                "run_dir": run_dir,
                "pair_chips": row["pair_chips"],
                "total_chips": row["total_chips"],
            }
    return matchups, metadata


def compare_runs(baseline_dirs, candidate_dirs):
    baselines, baseline_metadata = load_matchups(baseline_dirs)
    candidates, candidate_metadata = load_matchups(candidate_dirs)
    if set(baselines) != set(candidates):
        missing_candidate = sorted(set(baselines) - set(candidates))
        missing_baseline = sorted(set(candidates) - set(baselines))
        raise ValueError(
            "对手集合不一致；候选缺少 {}，基线缺少 {}".format(
                missing_candidate,
                missing_baseline,
            )
        )

    opponent_rows = []
    all_deltas = []
    for opponent in sorted(baselines):
        baseline_pairs = baselines[opponent]["pair_chips"]
        candidate_pairs = candidates[opponent]["pair_chips"]
        if len(baseline_pairs) != len(candidate_pairs):
            raise ValueError("对手 {} 的镜像组数量不一致".format(opponent))
        deltas = [
            round(candidate - baseline, 3)
            for baseline, candidate in zip(baseline_pairs, candidate_pairs)
        ]
        all_deltas.extend(deltas)
        opponent_rows.append({
            "opponent": opponent,
            "baseline_total": baselines[opponent]["total_chips"],
            "candidate_total": candidates[opponent]["total_chips"],
            "deltas": deltas,
            "summary": summarize(deltas),
        })

    return {
        "baseline_runs": baseline_metadata,
        "candidate_runs": candidate_metadata,
        "opponents": opponent_rows,
        "overall": summarize(all_deltas),
    }


def render_markdown(result):
    overall = result["overall"]
    lines = [
        "# 策略候选配对实验对比",
        "",
        "## 判定口径",
        "",
        "- 每个数据点是同一对手、同一固定牌堆的一组换牌换庄镜像净值差：`候选 - 基线`。",
        "- 仅当 95% 重采样均值区间下界大于 0，且配对符号置换检验 `p < 0.05`，才判为“显著增强”。",
        "- 该检验衡量本次对手池与牌堆样本，不等于对所有未知对手都成立。",
        "",
        "## 分对手结果",
        "",
        "| 对手 | 镜像组数 | 正-负-零 | 总增益 | 组均增益 | 中位增益 | 均值 95% 区间 | 置换 p 值 | 结论 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result["opponents"]:
        summary = row["summary"]
        lines.append(
            "| `{}` | {} | {}-{}-{} | {} | {} | {} | [{}, {}] | {} | {} |".format(
                row["opponent"],
                summary["count"],
                summary["positive"],
                summary["negative"],
                summary["zero"],
                summary["sum"],
                summary["mean"],
                summary["median"],
                *summary["bootstrap_mean_ci95"],
                summary["sign_flip_p_value"],
                summary["verdict"],
            )
        )

    lines.extend([
        "",
        "## 合并结果",
        "",
        "- 镜像组数：`{}`；正-负-零：`{}-{}-{}`。".format(
            overall["count"],
            overall["positive"],
            overall["negative"],
            overall["zero"],
        ),
        "- 总增益：`{}`；组均增益：`{}`；中位增益：`{}`。".format(
            overall["sum"],
            overall["mean"],
            overall["median"],
        ),
        "- 组均增益 95% 重采样区间：`[{}, {}]`。".format(
            *overall["bootstrap_mean_ci95"]
        ),
        "- 配对符号置换检验：`p={}`；精确符号检验：`p={}`。".format(
            overall["sign_flip_p_value"],
            overall["sign_test_p_value"],
        ),
        "- 综合结论：`{}`。".format(overall["verdict"]),
        "",
        "## 各镜像组差值",
        "",
    ])
    for row in result["opponents"]:
        lines.append("- `{}`：`{}`".format(
            row["opponent"],
            ", ".join(str(value) for value in row["deltas"]),
        ))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="比较基线与策略候选的配对镜像实验")
    parser.add_argument("--baseline", nargs="+", required=True, help="一个或多个基线运行目录")
    parser.add_argument("--candidate", nargs="+", required=True, help="一个或多个候选运行目录")
    parser.add_argument("--output-dir", required=True, help="对比报告输出目录")
    args = parser.parse_args()

    result = compare_runs(args.baseline, args.candidate)
    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "comparison.json")
    markdown_path = os.path.join(output_dir, "comparison.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))
    print("已生成：{}".format(markdown_path))


if __name__ == "__main__":
    main()
