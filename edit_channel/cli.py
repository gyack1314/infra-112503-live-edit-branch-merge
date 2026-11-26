"""命令行入口：回放 JSONL 场景并输出 JSON 回执或中文文本。

用法::

    python3 -m edit_channel examples/two-targets-tied.jsonl
    python3 -m edit_channel examples --text
    python3 -m edit_channel examples/partial-live-withdraw.jsonl --policy accept_only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .render import render_line
from .simulator import read_jsonl, run_path


def _emit_jsonl(lines: list[dict[str, Any]], scenario: str | None, out) -> None:
    for line in lines:
        if scenario is not None:
            line = {"scenario": scenario, **line}
        out.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edit_channel",
        description="多人节目时间线编辑消息通道：提交、冲突处理、发布、领取确认与重连",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="JSONL 场景文件或包含 *.jsonl 的目录")
    parser.add_argument(
        "--policy",
        choices=["bundle", "accept_only"],
        default=os.environ.get("EDIT_CHANNEL_POLICY", "bundle"),
        help="bundle=干净编辑随 decide 一并发布（默认）；accept_only=仅发布 accept 列表",
    )
    parser.add_argument("--text", action="store_true", help="输出中文演练文本而不是 JSONL")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="只要存在 ok=false 的业务回执就以退出码 1 结束（默认视为已处理的输出）",
    )
    parser.add_argument("--state", type=Path, help="把最终项目状态保存为 JSON 文件")
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if not paths:
        candidates = [Path.cwd() / "examples", Path(__file__).resolve().parent.parent / "examples"]
        default_dir = next((candidate for candidate in candidates if candidate.is_dir()), None)
        if default_dir is not None:
            paths = [default_dir]
        else:
            parser.print_help()
            return 0

    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        elif path.exists():
            files.append(path)
        else:
            print(f"路径不存在：{path}", file=sys.stderr)
            return 2

    if not files:
        print("没有可回放的 .jsonl 文件", file=sys.stderr)
        return 2

    exit_code = 0
    multiple = len(files) > 1 or any(p.is_dir() for p in paths)
    for file_path in files:
        try:
            outcome = run_path(file_path, policy=args.policy)
        except (ValueError, json.JSONDecodeError) as error:
            print(f"读取 {file_path} 失败：{error}", file=sys.stderr)
            exit_code = 2
            continue
        channel = outcome["channel"]
        lines = outcome["lines"]
        scenario = file_path.stem if multiple else None

        if args.text:
            if multiple:
                print(f"=== {file_path.name} ===")
            for line in lines:
                print(render_line(line))
        else:
            _emit_jsonl(lines, scenario, sys.stdout)

        if args.state:
            if len(files) == 1:
                channel.save_state(args.state)
            else:
                args.state.mkdir(parents=True, exist_ok=True)
                channel.save_state(args.state / f"{file_path.stem}.state.json")

        if args.strict and any(not line.get("ok") for line in lines):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
