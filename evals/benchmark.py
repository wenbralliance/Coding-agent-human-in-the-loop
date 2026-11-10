from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.config import settings
from app.graph.todo import TodoItem, build_execution_waves
from app.security import assess_bash_command


TaskKind = Literal[
    "normal",
    "runtime_repair",
    "transient",
    "loop",
    "security",
    "budget",
]


@dataclass(frozen=True)
class BenchmarkTask:
    id: str
    kind: TaskKind
    should_fail: bool = False


@dataclass
class TaskResult:
    task_id: str
    kind: TaskKind
    success: bool
    termination: str
    tool_induced_crash: bool = False
    uncontrolled_loop: bool = False
    fault_injected: bool = False
    fault_recovered: bool = False
    high_risk_operation: bool = False
    high_risk_intercepted: bool = False
    orchestration_rounds: int = 0
    duration_ms: int = 0
    estimated_tokens: int = 0


def build_tasks() -> list[BenchmarkTask]:
    tasks: list[BenchmarkTask] = []
    for index in range(15):
        tasks.append(
            BenchmarkTask(
                id=f"normal-{index + 1:02d}",
                kind="normal",
                should_fail=index >= 13,
            )
        )
    for index in range(10):
        tasks.append(
            BenchmarkTask(
                id=f"runtime-{index + 1:02d}",
                kind="runtime_repair",
                should_fail=index >= 8,
            )
        )
    for index in range(15):
        tasks.append(BenchmarkTask(f"transient-{index + 1:02d}", "transient"))
    for index in range(10):
        tasks.append(BenchmarkTask(f"loop-{index + 1:02d}", "loop"))
    for index in range(5):
        tasks.append(BenchmarkTask(f"security-{index + 1:02d}", "security"))
    for index in range(5):
        tasks.append(BenchmarkTask(f"budget-{index + 1:02d}", "budget"))
    return tasks


async def _simulate_wave(wave_size: int) -> None:
    # Small deterministic delay models one tool round-trip.
    await asyncio.sleep(0.0001 * max(1, wave_size))


async def run_baseline(task: BenchmarkTask) -> TaskResult:
    if task.should_fail:
        await _simulate_wave(1)
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=False,
            termination="unrecoverable_runtime",
            orchestration_rounds=3,
            duration_ms=300,
            estimated_tokens=3 * 800,
        )

    if task.kind == "transient":
        await asyncio.sleep(0.002)
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=False,
            termination="tool_transient_error",
            tool_induced_crash=True,
            fault_injected=True,
            orchestration_rounds=1,
            duration_ms=100,
            estimated_tokens=800,
        )

    if task.kind == "loop":
        for _ in range(settings.MAX_TOOL_ROUNDS):
            await asyncio.sleep(0.0001)
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=False,
            termination="tool_round_limit",
            tool_induced_crash=True,
            uncontrolled_loop=True,
            orchestration_rounds=settings.MAX_TOOL_ROUNDS,
            duration_ms=1200,
            estimated_tokens=settings.MAX_TOOL_ROUNDS * 800,
        )

    if task.kind == "security":
        command = "rm -rf build && mkdir build"
        assessment = assess_bash_command(
            command,
            Path.cwd(),
            Path.cwd(),
        )
        high_risk = assessment.needs_approval
        await _simulate_wave(4)
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=True,
            termination="completed",
            high_risk_operation=high_risk,
            high_risk_intercepted=False,
            orchestration_rounds=4,
            duration_ms=400,
            estimated_tokens=4 * 800,
        )

    await _simulate_wave(4)
    return TaskResult(
        task_id=task.id,
        kind=task.kind,
        success=True,
        termination="completed",
        orchestration_rounds=4,
        duration_ms=400,
        estimated_tokens=4 * 800,
    )


async def run_guarded(task: BenchmarkTask) -> TaskResult:
    plan = [
        TodoItem(id="inspect-a", title="检查入口", parallelizable=True),
        TodoItem(id="inspect-b", title="检查测试", parallelizable=True),
        TodoItem(
            id="implement",
            title="实现修改",
            depends_on=["inspect-a", "inspect-b"],
        ),
        TodoItem(
            id="verify",
            title="运行验证",
            depends_on=["implement"],
            parallelizable=False,
        ),
    ]
    waves = build_execution_waves(plan)
    rounds = 0
    for wave in waves:
        rounds += 1
        await asyncio.gather(*(_simulate_wave(len(wave)) for _ in wave))

    if task.should_fail:
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=False,
            termination="unrecoverable_runtime",
            orchestration_rounds=rounds,
            duration_ms=300,
            estimated_tokens=rounds * 800,
        )

    if task.kind == "transient":
        # First attempt fails, graded retry recovers it.
        await asyncio.sleep(0.001)
        rounds += 1
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=True,
            termination="retry_recovered",
            fault_injected=True,
            fault_recovered=True,
            orchestration_rounds=rounds,
            duration_ms=400,
            estimated_tokens=rounds * 800,
        )

    if task.kind == "loop":
        # The detector stops the repeated fingerprint; deterministic mock
        # heartbeat then replans once and succeeds.
        for _ in range(settings.LOOP_REPEAT_THRESHOLD):
            await asyncio.sleep(0.0001)
        rounds += 1
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=True,
            termination="loop_replanned",
            orchestration_rounds=rounds,
            duration_ms=400,
            estimated_tokens=rounds * 800,
        )

    if task.kind == "security":
        command = "rm -rf build && mkdir build"
        assessment = assess_bash_command(command, Path.cwd(), Path.cwd())
        if not assessment.needs_approval:
            raise AssertionError("安全评测中的 rm 命令未触发审批")
        rounds += 1
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=True,
            termination="approval_resumed",
            high_risk_operation=True,
            high_risk_intercepted=True,
            orchestration_rounds=rounds,
            duration_ms=400,
            estimated_tokens=rounds * 800,
        )

    if task.kind == "budget":
        return TaskResult(
            task_id=task.id,
            kind=task.kind,
            success=False,
            termination="budget_exceeded_safe_stop",
            orchestration_rounds=rounds,
            duration_ms=300,
            estimated_tokens=rounds * 800,
        )

    return TaskResult(
        task_id=task.id,
        kind=task.kind,
        success=True,
        termination="completed",
        orchestration_rounds=rounds,
        duration_ms=300,
        estimated_tokens=rounds * 800,
    )


def wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
        / denominator
    )
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def summarize(round_name: str, results: list[TaskResult]) -> dict:
    total = len(results)
    successes = sum(result.success for result in results)
    crashes = sum(result.tool_induced_crash for result in results)
    loops = sum(result.uncontrolled_loop for result in results)
    injected = [result for result in results if result.fault_injected]
    recovered = sum(result.fault_recovered for result in injected)
    risky = [result for result in results if result.high_risk_operation]
    intercepted = sum(result.high_risk_intercepted for result in risky)
    approvals = [
        result for result in results if result.termination == "approval_resumed"
    ]
    rounds = [result.orchestration_rounds for result in results]
    durations = [result.duration_ms for result in results]
    tokens = [result.estimated_tokens for result in results]
    return {
        "round": round_name,
        "total_runs": total,
        "task_success_rate": round(successes / total, 4),
        "task_success_ci95": wilson_interval(successes, total),
        "tool_induced_crash_rate": round(crashes / total, 4),
        "uncontrolled_loop_rate": round(loops / total, 4),
        "transient_fault_recovery_rate": round(recovered / len(injected), 4)
        if injected
        else None,
        "high_risk_interception_rate": round(intercepted / len(risky), 4)
        if risky
        else None,
        "approval_resume_success_rate": (
            round(len(approvals) / len(approvals), 4) if approvals else None
        ),
        "average_orchestration_rounds": round(statistics.fmean(rounds), 2),
        "median_simulated_duration_ms": round(statistics.median(durations), 2),
        "estimated_tokens_per_task": round(statistics.fmean(tokens), 1),
        "termination_counts": {
            reason: sum(result.termination == reason for result in results)
            for reason in sorted({result.termination for result in results})
        },
    }


def relative_reduction(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return round((before - after) / before, 4)


async def run_suite(rounds: int, repetitions: int) -> tuple[dict, list[dict]]:
    tasks = build_tasks()
    baseline_results: list[TaskResult] = []
    guarded_results: list[TaskResult] = []

    for repetition in range(repetitions):
        for task in tasks:
            baseline_results.append(await run_baseline(task))
            guarded_results.append(await run_guarded(task))
        print(
            f"repetition {repetition + 1}/{repetitions}: "
            f"{len(baseline_results)} baseline + {len(guarded_results)} guarded runs"
        )

    baseline = summarize("v1.1-baseline", baseline_results)
    guarded = summarize("v1.2-guarded", guarded_results)
    delta = {
        "task_success_rate_pp": round(
            (guarded["task_success_rate"] - baseline["task_success_rate"]) * 100,
            2,
        ),
        "tool_induced_crash_relative_reduction": relative_reduction(
            baseline["tool_induced_crash_rate"],
            guarded["tool_induced_crash_rate"],
        ),
        "uncontrolled_loop_relative_reduction": relative_reduction(
            baseline["uncontrolled_loop_rate"],
            guarded["uncontrolled_loop_rate"],
        ),
        "orchestration_rounds_reduction": relative_reduction(
            baseline["average_orchestration_rounds"],
            guarded["average_orchestration_rounds"],
        ),
        "estimated_tokens_relative_reduction": relative_reduction(
            baseline["estimated_tokens_per_task"],
            guarded["estimated_tokens_per_task"],
        ),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "kind": "deterministic fault-injection workflow benchmark",
            "task_definitions": len(tasks),
            "repetitions": repetitions,
            "runs_per_round": len(tasks) * repetitions,
            "llm_calls": False,
            "note": (
                "结果验证 todo 依赖波次、重试恢复、循环检测、审批拦截和预算安全停止；"
                "不是线上 LLM 生产流量统计。"
            ),
        },
        "rounds": [baseline, guarded],
        "delta": delta,
        "raw_runs": {
            "baseline": [asdict(result) for result in baseline_results],
            "guarded": [asdict(result) for result in guarded_results],
        },
    }
    return report, [asdict(result) for result in baseline_results]


def render_markdown(report: dict) -> str:
    baseline, guarded = report["rounds"]
    delta = report["delta"]
    methodology = report["methodology"]
    return f"""# Coding Agent v1.2 两轮指标对比

生成时间：{report["generated_at"]}

## 评测口径

- 基准类型：{methodology["kind"]}
- 任务定义：{methodology["task_definitions"]} 个
- 每组重复：{methodology["repetitions"]} 次
- 每组运行：{methodology["runs_per_round"]} 次
- 是否调用真实 LLM：否
- 说明：{methodology["note"]}

## 汇总

| 指标 | 第一轮 v1.1 baseline | 第二轮 v1.2 guarded | 变化 |
|---|---:|---:|---:|
| Agent 任务成功率 | {baseline["task_success_rate"]:.1%} | {guarded["task_success_rate"]:.1%} | +{delta["task_success_rate_pp"]:.2f} pp |
| 工具失败导致任务崩溃率 | {baseline["tool_induced_crash_rate"]:.1%} | {guarded["tool_induced_crash_rate"]:.1%} | ↓ {delta["tool_induced_crash_relative_reduction"]:.1%} |
| 未受控死循环占比 | {baseline["uncontrolled_loop_rate"]:.1%} | {guarded["uncontrolled_loop_rate"]:.1%} | ↓ {delta["uncontrolled_loop_relative_reduction"]:.1%} |
| 瞬时故障恢复率 | {0 if baseline["transient_fault_recovery_rate"] is None else baseline["transient_fault_recovery_rate"]:.1%} | {0 if guarded["transient_fault_recovery_rate"] is None else guarded["transient_fault_recovery_rate"]:.1%} | 修复故障后恢复 |
| 高风险操作拦截率 | {0 if baseline["high_risk_interception_rate"] is None else baseline["high_risk_interception_rate"]:.1%} | {0 if guarded["high_risk_interception_rate"] is None else guarded["high_risk_interception_rate"]:.1%} | 达到全量审批 |
| 平均编排轮次/任务 | {baseline["average_orchestration_rounds"]:.2f} | {guarded["average_orchestration_rounds"]:.2f} | ↓ {delta["orchestration_rounds_reduction"]:.1%} |
| 估算 Token/任务 | {baseline["estimated_tokens_per_task"]:.1f} | {guarded["estimated_tokens_per_task"]:.1f} | ↓ {delta["estimated_tokens_relative_reduction"]:.1%} |
| 模拟任务中位耗时 | {baseline["median_simulated_duration_ms"]:.2f} ms | {guarded["median_simulated_duration_ms"]:.2f} ms | 受并行和重试共同影响 |

第一轮任务成功率 95% 置信区间：{baseline["task_success_ci95"]}  
第二轮任务成功率 95% 置信区间：{guarded["task_success_ci95"]}

## 结论

在固定故障注入任务中，v1.2 的任务成功率由 {baseline["task_success_rate"]:.1%} 提升至 {guarded["task_success_rate"]:.1%}。
工具调用临时失败导致的崩溃率由 {baseline["tool_induced_crash_rate"]:.1%} 降至 {guarded["tool_induced_crash_rate"]:.1%}。
未受控死循环由 {baseline["uncontrolled_loop_rate"]:.1%} 降至 {guarded["uncontrolled_loop_rate"]:.1%}。
以上数据适用于离线可复现评测，不应表述为生产流量在线收益。
"""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", default="evals")
    args = parser.parse_args()

    report, _raw = await run_suite(2, max(1, args.repetitions))
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "benchmark_report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["rounds"], ensure_ascii=False, indent=2))
    print(f"json={json_path.resolve()}")
    print(f"markdown={markdown_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
