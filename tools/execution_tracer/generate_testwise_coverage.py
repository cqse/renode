#!/usr/bin/env python3
#
# Copyright (c) 2010-2026 Antmicro
#
# This file is licensed under the MIT License.
# Full license text is available in 'licenses/MIT.txt'.
#

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple


# Matches a monitor command like `cpu CreateExecutionTracing "name" @path/trace.bin.gz PC ...`.
# Groups: trace_name = the quoted tracer name; output_path = the next whitespace-free token (the trace file path).
CREATE_EXECUTION_TRACING_RE = re.compile(
    r"CreateExecutionTracing\s+\"(?P<trace_name>[^\"]+)\"\s+(?P<output_path>\S+)"
)
# Matches a whole .resc variable assignment line like `$bin=@file.elf` or `$bin?=@file.elf`.
# Groups: name = variable name without `$`; conditional = `?` for "assign only if unset" (else None); value = the rest.
RESC_VARIABLE_ASSIGNMENT_RE = re.compile(
    r"^\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<conditional>\?)?=(?P<value>.+)$"
)
# Matches a .resc variable reference like `$ORIGIN` anywhere in a value. Group 1: the variable name without `$`.
RESC_VARIABLE_REFERENCE_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class TraceDefinition:
    trace_name: str
    output_path: str
    resolved_output_path: str


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "resc_file",
        type=Path,
        help="Path to a .resc file containing CreateExecutionTracing commands.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where generated report files will be written.",
    )
    parser.add_argument(
        "--lcov-dir",
        type=Path,
        default=None,
        help=(
            "Directory with LCOV files. Files named "
            "<execution-name>.info or <execution-name>.lcov are consumed."
        ),
    )
    parser.add_argument(
        "--allow-empty-coverage",
        action="store_true",
        help=(
            "Allow generating reports without real coverage data. "
            "By default this tool requires LCOV-derived coverage."
        ),
    )
    parser.add_argument(
        "--suite-name",
        type=str,
        default=None,
        help="Optional suite name. Defaults to the .resc file stem.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Execution duration in seconds for each generated test entry.",
    )
    parser.add_argument(
        "--result",
        choices=["PASSED", "FAILURE", "ERROR", "SKIPPED"],
        default="PASSED",
        help="Result status for each generated test entry.",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Mark generated report as partial (recommended for per-test uploads).",
    )
    parser.add_argument(
        "--method-accurate",
        action="store_true",
        help="Set methodAccurate=true in generated reports.",
    )
    parser.add_argument(
        "--renode-cwd",
        type=Path,
        default=Path.cwd(),
        help="Path used to resolve $CWD and relative trace paths from the RESC file.",
    )
    parser.add_argument(
        "--resc-var",
        action="append",
        default=[],
        help=(
            "Override/define a RESC variable for output-path resolution. "
            "Format: NAME=VALUE. Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--auto-lcov",
        action="store_true",
        help=(
            "Automatically convert trace files declared in the RESC script "
            "into LCOV files before generating testwise coverage JSON."
        ),
    )
    parser.add_argument(
        "--retracer-script",
        type=Path,
        default=Path(__file__).resolve().with_name("execution_tracer_reader.py"),
        help="Path to execution_tracer_reader.py wrapper script.",
    )
    parser.add_argument(
        "--auto-lcov-binary",
        type=Path,
        default=None,
        help="Path to ELF with DWARF debug info for auto-LCOV generation.",
    )
    parser.add_argument(
        "--auto-lcov-binary-url",
        type=str,
        default=None,
        help="URL to ELF with DWARF debug info for auto-LCOV generation.",
    )
    parser.add_argument(
        "--auto-lcov-pc2line",
        type=Path,
        default=None,
        help="Path to PC-to-line mapping file for auto-LCOV generation.",
    )
    parser.add_argument(
        "--auto-lcov-sources",
        nargs="+",
        type=Path,
        default=None,
        help=(
            "Optional source files for auto-LCOV generation. "
            "If omitted, execution_tracer_reader will attempt DWARF-based discovery."
        ),
    )
    parser.add_argument(
        "--auto-lcov-sub-source-path",
        action="append",
        default=[],
        help=(
            "Optional source path substitution for auto-LCOV generation. "
            "Format: old_path:new_path. Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project name for uniformPath prefix.",
    )
    parser.add_argument(
        "--architecture",
        type=str,
        default=None,
        help="Architecture name for uniformPath (e.g., arm_cortex_a9).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to test manifest JSON (from testwise_coverage_listener).",
    )


def parse_args() -> argparse.Namespace:
    # Backward compatibility: if the first non-option argument is not a known
    # subcommand, implicitly prepend "generate" so that old invocations
    # (e.g. `script.py my.resc --output-dir ...`) continue to work.
    known_subcommands = {"generate", "merge"}
    argv = sys.argv[1:]
    first_positional = next((a for a in argv if not a.startswith("-")), None)
    if first_positional not in known_subcommands:
        argv = ["generate"] + argv

    parser = argparse.ArgumentParser(
        description=(
            "Generate Teamscale testwise-coverage JSON reports "
            "for test executions described by a .resc file, "
            "or merge multiple reports into one."
        )
    )
    subparsers = parser.add_subparsers(dest="command")

    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate testwise-coverage JSON from a .resc file and LCOV data.",
    )
    _add_generate_args(generate_parser)

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge multiple testwise-coverage JSON files into one.",
    )
    merge_parser.add_argument(
        "input_files",
        nargs="+",
        type=Path,
        help="Testwise-coverage JSON files to merge.",
    )
    merge_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path for the merged output JSON file.",
    )

    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.command != "generate":
        return
    if args.auto_lcov:
        source_map_argument_count = sum(
            value is not None
            for value in (
                args.auto_lcov_binary,
                args.auto_lcov_binary_url,
                args.auto_lcov_pc2line,
            )
        )
        if source_map_argument_count != 1:
            raise RuntimeError(
                "In --auto-lcov mode, provide exactly one source-map input: "
                "--auto-lcov-binary, --auto-lcov-binary-url, or --auto-lcov-pc2line."
            )


def normalize_resc_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", "\""):
        return value[1:-1]
    return value


def parse_resc_var_overrides(raw_items: List[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for item in raw_items:
        if "=" not in item:
            raise RuntimeError(f"Invalid --resc-var value '{item}'. Expected NAME=VALUE.")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise RuntimeError(f"Invalid --resc-var value '{item}'. Variable name is empty.")
        overrides[name] = value
    return overrides


def resolve_resc_variables(value: str, variables: Dict[str, str]) -> str:
    resolved = value
    for _ in range(10):
        next_value = RESC_VARIABLE_REFERENCE_RE.sub(
            lambda match: variables.get(match.group(1), match.group(0)),
            resolved,
        )
        if next_value == resolved:
            break
        resolved = next_value
    return resolved


def parse_trace_definitions(
    resc_file: Path,
    renode_cwd: Path,
    resc_var_overrides: Dict[str, str],
) -> List[TraceDefinition]:
    traces: List[TraceDefinition] = []
    variables: Dict[str, str] = {
        "CWD": str(renode_cwd.resolve()),
        "ORIGIN": str(resc_file.resolve().parent),
    }
    variables.update(resc_var_overrides)

    for raw_line in resc_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue

        variable_match = RESC_VARIABLE_ASSIGNMENT_RE.match(line)
        if variable_match:
            variable_name = variable_match.group("name")
            is_conditional = variable_match.group("conditional") is not None
            if is_conditional and variable_name in variables:
                continue
            value = normalize_resc_value(variable_match.group("value"))
            variables[variable_name] = resolve_resc_variables(value, variables)
            continue

        match = CREATE_EXECUTION_TRACING_RE.search(line)
        if not match:
            continue
        output_path = normalize_resc_value(match.group("output_path"))
        resolved_output_path = resolve_resc_variables(output_path, variables)
        traces.append(
            TraceDefinition(
                trace_name=match.group("trace_name"),
                output_path=output_path,
                resolved_output_path=resolved_output_path,
            )
        )
    return traces


def find_execution_name(trace_definition: TraceDefinition) -> str:
    output_name = Path(trace_definition.output_path.strip('\"')).name
    if output_name.endswith(".trace.bin.gz"):
        return output_name[: -len(".trace.bin.gz")]
    if output_name.endswith(".trace.bin"):
        return output_name[: -len(".trace.bin")]
    if output_name.endswith(".bin.gz"):
        return output_name[: -len(".bin.gz")]
    if output_name.endswith(".gz"):
        return output_name[: -len(".gz")]
    if output_name.endswith(".bin"):
        return output_name[: -len(".bin")]
    return Path(output_name).stem


def resolve_trace_file_path(trace_definition: TraceDefinition, renode_cwd: Path) -> Path:
    resolved = trace_definition.resolved_output_path.strip()
    if resolved.startswith("@"):
        resolved = resolved[1:]
    if "$" in resolved:
        raise RuntimeError(
            f"Unresolved variable in trace output path '{trace_definition.output_path}' "
            f"(resolved to '{trace_definition.resolved_output_path}'). "
            "Provide missing values with --resc-var NAME=VALUE or adjust --renode-cwd."
        )

    trace_path = Path(resolved)
    if not trace_path.is_absolute():
        trace_path = renode_cwd / trace_path
    return trace_path.resolve()


def run_auto_lcov_for_traces(
    args: argparse.Namespace,
    traces: List[TraceDefinition],
) -> Path:
    lcov_dir = args.lcov_dir if args.lcov_dir is not None else args.output_dir / "lcov"
    lcov_dir.mkdir(parents=True, exist_ok=True)

    retracer_script = args.retracer_script.resolve()
    if not retracer_script.exists():
        raise RuntimeError(f"Retracer script not found at {retracer_script}.")

    for trace in traces:
        execution_name = find_execution_name(trace)
        trace_file_path = resolve_trace_file_path(trace, args.renode_cwd)
        if not trace_file_path.exists():
            raise RuntimeError(
                f"Trace file for '{execution_name}' does not exist: {trace_file_path}"
            )

        lcov_output_path = lcov_dir / f"{execution_name}.info"
        command = [
            sys.executable,
            str(retracer_script),
            "coverage",
            str(trace_file_path),
            "--output",
            str(lcov_output_path),
        ]

        if args.auto_lcov_binary is not None:
            command.extend(["--binary", str(args.auto_lcov_binary)])
        elif args.auto_lcov_binary_url is not None:
            command.extend(["--binary-url", args.auto_lcov_binary_url])
        elif args.auto_lcov_pc2line is not None:
            command.extend(["--pc2line", str(args.auto_lcov_pc2line)])

        if args.auto_lcov_sources is not None:
            command.extend(["--sources", *[str(path) for path in args.auto_lcov_sources]])

        for substitution in args.auto_lcov_sub_source_path:
            command.extend(["--sub-source-path", substitution])

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = []
            if result.stdout:
                details.append(f"stdout:\n{result.stdout.strip()}")
            if result.stderr:
                details.append(f"stderr:\n{result.stderr.strip()}")
            details_text = "\n\n".join(details) if details else "No output captured."
            raise RuntimeError(
                f"Auto-LCOV generation failed for '{execution_name}' "
                f"(trace: {trace_file_path}).\n{details_text}"
            )

    return lcov_dir


def lcov_line_set_to_ranges(lines: Set[int]) -> str:
    if not lines:
        return ""
    ordered = sorted(lines)
    ranges: List[str] = []
    start = ordered[0]
    prev = ordered[0]
    for line in ordered[1:]:
        if line == prev + 1:
            prev = line
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = line
        prev = line
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def parse_lcov_executed_lines(lcov_file: Path) -> Tuple[List[str], Dict[str, str]]:
    file_index_map: Dict[str, int] = {}
    file_paths: List[str] = []
    executed_per_file: Dict[str, Set[int]] = {}
    current_file: str | None = None

    for raw_line in lcov_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("SF:"):
            current_file = line[3:]
            if current_file not in file_index_map:
                file_index_map[current_file] = len(file_paths)
                file_paths.append(current_file)
            executed_per_file.setdefault(current_file, set())
            continue
        if not line.startswith("DA:") or current_file is None:
            continue

        payload = line[3:]
        parts = payload.split(",", 2)
        if len(parts) < 2:
            continue
        line_number = int(parts[0])
        execution_count = int(parts[1])
        if execution_count > 0:
            executed_per_file[current_file].add(line_number)

    coverage_map: Dict[str, str] = {}
    for path in file_paths:
        range_spec = lcov_line_set_to_ranges(executed_per_file.get(path, set()))
        if range_spec:
            coverage_map[str(file_index_map[path])] = range_spec
    return file_paths, coverage_map


def build_uniform_path(
    test_name: str,
    project: str | None,
    architecture: str | None,
    suite_name: str,
) -> str:
    if project is not None and architecture is not None:
        return f"{project}/{architecture}/{test_name}"
    return f"{suite_name}/{test_name}"


def build_report(
    suite_name: str,
    test_name: str,
    result: str,
    duration: float,
    partial: bool,
    method_accurate: bool,
    files: List[str],
    coverage: Dict[str, str],
    project: str | None = None,
    architecture: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    uniform_path = build_uniform_path(test_name, project, architecture, suite_name)
    test_entry = {
        "uniformPath": uniform_path,
        "name": test_name,
        "startTime": now,
        "endTime": now,
        "duration": duration,
        "result": result,
    }
    if coverage:
        test_entry["coverage"] = coverage

    return {
        "version": 2,
        "partial": partial,
        "methodAccurate": method_accurate,
        "files": files,
        "tests": [test_entry],
    }


def merge_file_lists(
    per_trace_files: List[List[str]],
) -> Tuple[List[str], List[Dict[int, int]]]:
    """Build unified file list and per-trace index remapping.

    Returns:
        unified_files: deduplicated list of all file paths
        remap_tables: for each trace, a dict mapping old_index -> new_index
    """
    unified_map: Dict[str, int] = {}
    unified_files: List[str] = []
    remap_tables: List[Dict[int, int]] = []

    for trace_files in per_trace_files:
        remap: Dict[int, int] = {}
        for old_idx, path in enumerate(trace_files):
            if path not in unified_map:
                unified_map[path] = len(unified_files)
                unified_files.append(path)
            remap[old_idx] = unified_map[path]
        remap_tables.append(remap)

    return unified_files, remap_tables


def remap_coverage(
    coverage: Dict[str, str],
    remap: Dict[int, int],
) -> Dict[str, str]:
    """Remap coverage keys from old file indices to new unified indices."""
    remapped: Dict[str, str] = {}
    for old_idx_str, ranges in coverage.items():
        new_idx = remap[int(old_idx_str)]
        remapped[str(new_idx)] = ranges
    return remapped


def build_aggregated_report(
    project: str | None,
    architecture: str | None,
    suite_name: str,
    test_entries: List[dict],
    partial: bool,
    method_accurate: bool,
) -> dict:
    """Build a single testwise-coverage report with all tests merged.

    Each entry in test_entries has keys:
        name, result, duration, files (List[str]), coverage (Dict[str, str])
    """
    per_trace_files = [entry["files"] for entry in test_entries]
    unified_files, remap_tables = merge_file_lists(per_trace_files)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tests = []
    for i, entry in enumerate(test_entries):
        remapped_coverage = remap_coverage(entry["coverage"], remap_tables[i])
        uniform_path = build_uniform_path(
            entry["name"], project, architecture, suite_name,
        )
        test_info: dict = {
            "uniformPath": uniform_path,
            "name": entry["name"],
            "startTime": now,
            "endTime": now,
            "duration": entry["duration"],
            "result": entry["result"],
        }
        if remapped_coverage:
            test_info["coverage"] = remapped_coverage
        tests.append(test_info)

    return {
        "version": 2,
        "partial": partial,
        "methodAccurate": method_accurate,
        "files": [{"path": f} for f in unified_files],
        "tests": tests,
    }


def load_manifest(manifest_path: Path) -> Dict[str, dict]:
    """Load test manifest and return a dict keyed by test name."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest: Dict[str, dict] = {}
    for entry in data["tests"]:
        manifest[entry["name"]] = entry
    return manifest


def generate_reports(args: argparse.Namespace) -> List[Path]:
    resc_var_overrides = parse_resc_var_overrides(args.resc_var)
    traces = parse_trace_definitions(args.resc_file, args.renode_cwd, resc_var_overrides)
    if not traces:
        raise RuntimeError(
            f"No CreateExecutionTracing entries found in {args.resc_file}."
        )
    if args.auto_lcov:
        args.lcov_dir = run_auto_lcov_for_traces(args, traces)
    if args.lcov_dir is None and not args.allow_empty_coverage:
        raise RuntimeError(
            "Missing --lcov-dir. CreateExecutionTracing outputs trace files, not final coverage data. "
            "Generate LCOV files first (see execution-tracing/coverage-report docs), then rerun this tool."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    suite_name = args.suite_name or args.resc_file.stem

    manifest = None
    if args.manifest is not None:
        manifest = load_manifest(args.manifest)

    use_aggregated = args.project is not None and args.architecture is not None

    test_entries: List[dict] = []

    for trace in traces:
        execution_name = find_execution_name(trace)
        files: List[str] = []
        coverage: Dict[str, str] = {}
        lcov_found = False

        if args.lcov_dir is not None:
            candidates = [
                args.lcov_dir / f"{execution_name}.info",
                args.lcov_dir / f"{execution_name}.lcov",
            ]
            for candidate in candidates:
                if candidate.exists():
                    lcov_found = True
                    files, coverage = parse_lcov_executed_lines(candidate)
                    break
            if not lcov_found and not args.allow_empty_coverage:
                raise RuntimeError(
                    f"No LCOV file found for '{execution_name}' in {args.lcov_dir}. "
                    f"Expected one of: {candidates[0].name}, {candidates[1].name}"
                )
            if lcov_found and not coverage and not args.allow_empty_coverage:
                raise RuntimeError(
                    f"LCOV file for '{execution_name}' contains no executed lines; refusing to emit empty coverage report."
                )

        # Resolve test metadata from manifest or CLI defaults
        if manifest is not None and execution_name in manifest:
            meta = manifest[execution_name]
            test_result = meta["result"]
            test_duration = meta["duration"]
        else:
            test_result = args.result
            test_duration = args.duration

        test_entries.append({
            "name": execution_name,
            "result": test_result,
            "duration": test_duration,
            "files": files,
            "coverage": coverage,
        })

    if use_aggregated:
        report = build_aggregated_report(
            project=args.project,
            architecture=args.architecture,
            suite_name=suite_name,
            test_entries=test_entries,
            partial=args.partial,
            method_accurate=args.method_accurate,
        )
        output_file = args.output_dir / "testwise-coverage.json"
        output_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return [output_file]
    else:
        # Legacy mode: one file per trace
        written_files: List[Path] = []
        for entry in test_entries:
            report = build_report(
                suite_name=suite_name,
                test_name=entry["name"],
                result=entry["result"],
                duration=entry["duration"],
                partial=args.partial,
                method_accurate=args.method_accurate,
                files=entry["files"],
                coverage=entry["coverage"],
                project=args.project,
                architecture=args.architecture,
            )
            output_file = args.output_dir / f"{entry['name']}.testwise-coverage.json"
            output_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
            written_files.append(output_file)
        return written_files


def merge_reports(args: argparse.Namespace) -> Path:
    """Merge multiple testwise-coverage JSON files into one."""
    all_file_lists: List[List[str]] = []
    all_tests: List[List[dict]] = []
    # Track which file-list index each report's tests belong to
    report_indices: List[int] = []

    partial = False
    method_accurate = False

    for input_file in args.input_files:
        data = json.loads(input_file.read_text(encoding="utf-8"))
        partial = partial or data.get("partial", False)
        method_accurate = method_accurate or data.get("methodAccurate", False)

        # Files can be either strings or {"path": ...} objects
        raw_files = data.get("files", [])
        file_paths = []
        for f in raw_files:
            if isinstance(f, dict):
                file_paths.append(f["path"])
            else:
                file_paths.append(f)

        report_idx = len(all_file_lists)
        all_file_lists.append(file_paths)
        all_tests.append(data.get("tests", []))
        report_indices.append(report_idx)

    unified_files, remap_tables = merge_file_lists(all_file_lists)

    merged_tests: List[dict] = []
    for report_idx, tests in enumerate(all_tests):
        remap = remap_tables[report_idx]
        for test in tests:
            merged_test = dict(test)
            if "coverage" in test:
                merged_test["coverage"] = remap_coverage(test["coverage"], remap)
            merged_tests.append(merged_test)

    merged = {
        "version": 2,
        "partial": partial,
        "methodAccurate": method_accurate,
        "files": [{"path": f} for f in unified_files],
        "tests": merged_tests,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return args.output


def main() -> None:
    args = parse_args()
    validate_args(args)

    if args.command == "merge":
        output = merge_reports(args)
        print(f"Merged {len(args.input_files)} file(s) into: {output}")
    else:
        written_files = generate_reports(args)
        print(f"Generated {len(written_files)} report file(s):")
        for file in written_files:
            print(file)


if __name__ == "__main__":
    main()
