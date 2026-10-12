#!/usr/bin/env python3
#
# Copyright (c) 2026 CQSE GmbH
#
# This file is licensed under the MIT License.
# Full license text is available in 'licenses/MIT.txt'.
#
"""Turn Renode testwise-coverage records into Teamscale testwise-coverage reports.

Input: the `tcov-*.jsonl` files written by Renode (`renode-test --testwise-coverage <dir>`, or the monitor commands
`EnableTestwiseCoverageGlobally` / `TestwiseCoverage BeginTest|EndTest`). Each line is one test with the executed byte
ranges per CPU and the images (ELF path + MD5) loaded on its machines.

For every image, the DWARF line table is read once (cached by MD5). A source line counts as covered by a test if any of
its address ranges overlaps a range the test executed. Source files are assigned to Teamscale projects by path prefix
(config file), and one report (format version 2) is written per project.

Config (YAML or JSON):

    uniformPath: "{suite}/{test}"      # placeholders: {suite}, {test}
    projects:                          # first match wins; files matching none are counted as "unmapped"
      - name: zmk
        match: /work/zmk/app/          # DWARF path prefix selecting the files
        strip: /work/zmk/              # removed from the path (default: match); the rest is the path in Teamscale
    ignore: [/opt/zephyr-sdk]          # path prefixes that are dropped silently (toolchain, generated code)
"""

import argparse
import bisect
import json
import os
import pickle
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from execution_tracer import dwarf

RESULTS = {"PASS": "PASSED", "FAIL": "FAILURE", "SKIP": "SKIPPED"}


def load_config(path):
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml
        config = yaml.safe_load(text)
    else:
        config = json.loads(text)
    config.setdefault("uniformPath", "{suite}/{test}")
    config.setdefault("projects", [])
    config.setdefault("ignore", [])
    for project in config["projects"]:
        project.setdefault("strip", project["match"])
    return config


def read_records(paths):
    records = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            records.extend(json.loads(line) for line in f if line.strip())
    return records


def build_line_table(elf_path):
    """Sorted list of (low, high, file, line) from the DWARF line program of one image."""
    with open(elf_path, "rb") as f:
        info = dwarf.get_dwarf_info(f)
        table = [(e.address_low, e.address_high, os.path.normpath(os.path.join(e.file_path, e.file_name)), e.line_number)
                 for e in dwarf.get_addresses(info)]
    table.sort()
    return table


def load_line_table(image, cache_dir):
    """(md5, table); the table is cached on disk by MD5, since parsing DWARF is the expensive part."""
    md5 = image["md5"]
    cache_file = Path(cache_dir) / f"{md5}.pickle" if cache_dir else None
    if cache_file and cache_file.exists():
        with open(cache_file, "rb") as f:
            return md5, pickle.load(f)
    table = build_line_table(image["path"])
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(table, f)
        tmp.replace(cache_file)
    return md5, table


def covered_lines(table, ranges):
    """Lines whose address range overlaps one of the executed, sorted, disjoint [start, end) ranges."""
    starts = [r[0] for r in ranges]
    lines = set()
    for low, high, path, line in table:
        # The last executed range starting before `high` is the only candidate (ranges are disjoint and sorted).
        i = bisect.bisect_left(starts, high) - 1
        if i >= 0 and ranges[i][1] > low:
            lines.add((path, line))
    return lines


def unattributed_bytes(table, ranges):
    """Executed bytes outside of any line table entry (no debug info: ROM, other images, generated code)."""
    known = []
    for low, high, _, _ in table:
        if known and low <= known[-1][1]:
            known[-1][1] = max(known[-1][1], high)
        else:
            known.append([low, high])
    starts = [k[0] for k in known]
    total = 0
    for start, end in ranges:
        outside = end - start
        i = max(0, bisect.bisect_right(starts, start) - 1)
        while i < len(known) and known[i][0] < end:
            outside -= max(0, min(end, known[i][1]) - max(start, known[i][0]))
            i += 1
        total += outside
    return total


def map_path(path, config):
    """(project, path in project) or None (ignored); ('', path) for unmapped files."""
    for prefix in config["ignore"]:
        if path.startswith(prefix):
            return None
    for project in config["projects"]:
        if path.startswith(project["match"]):
            return project["name"], path[len(project["strip"]):].lstrip("/")
    return "", path


def line_ranges(lines):
    """[1, 2, 3, 5] -> "1-3,5" (Teamscale's covered-lines notation)."""
    ordered = sorted(lines)
    parts = []
    start = prev = ordered[0]
    for line in ordered[1:] + [None]:
        if line is not None and line == prev + 1:
            prev = line
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        if line is not None:
            start = prev = line
    return ",".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("records", nargs="+", help="tcov-*.jsonl files (or directories containing them)")
    parser.add_argument("--config", required=True, help="tia.yaml / tia.json")
    parser.add_argument("--output-dir", required=True, type=Path, help="Writes <project>.json per Teamscale project")
    parser.add_argument("--cache-dir", default=None, help="Cache for parsed DWARF line tables (by image MD5)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count(), help="Parallel DWARF parsing")
    parser.add_argument("--partial", action="store_true", help="Mark reports as partial (not all tests were run)")
    args = parser.parse_args()

    config = load_config(args.config)
    paths = []
    for item in args.records:
        paths.extend(sorted(Path(item).glob("tcov-*.jsonl")) if Path(item).is_dir() else [Path(item)])
    records = read_records(paths)

    images = {}
    for record in records:
        for image in record["images"]:
            images.setdefault(image["md5"], image)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        tables = dict(pool.map(load_line_table, images.values(), [args.cache_dir] * len(images)))

    # Group attempts (retries) by uniform path: union of coverage, result and duration of the last attempt.
    tests = {}
    executed_bytes = unattributed = 0
    for record in records:
        uniform_path = config["uniformPath"].format(suite=record["suite"], test=record["test"])
        test = tests.setdefault(uniform_path, {"lines": set(), "attempt": 0})
        if record.get("attempt", 1) >= test["attempt"]:
            test.update(attempt=record.get("attempt", 1), result=RESULTS.get(record["result"], "ERROR"),
                        duration=record["durationMs"] / 1000.0)
        # Images are per machine; a CPU's ranges are resolved against the images loaded on its machine.
        machine_tables = defaultdict(list)
        for image in record["images"]:
            machine_tables[image["machine"]].append(tables[image["md5"]])
        for cpu in record["cpus"]:
            ranges = [tuple(r) for r in cpu["ranges"]]
            executed_bytes += sum(end - start for start, end in ranges)
            cpu_tables = machine_tables.get(cpu["machine"], [])
            for table in cpu_tables:
                test["lines"] |= covered_lines(table, ranges)
            # With several images on a machine, a byte is attributed if any image knows it; approximate with the first.
            unattributed += unattributed_bytes(cpu_tables[0], ranges) if cpu_tables else sum(e - s for s, e in ranges)

    # Per project: file index + coverage per test.
    reports = defaultdict(lambda: {"files": {}, "tests": []})
    unmapped = defaultdict(int)
    for uniform_path, test in sorted(tests.items()):
        per_project = defaultdict(lambda: defaultdict(set))
        for path, line in test["lines"]:
            mapped = map_path(path, config)
            if mapped is None:
                continue
            project, project_path = mapped
            if not project:
                unmapped[path] += 1
                continue
            per_project[project][project_path].add(line)
        for project in [p["name"] for p in config["projects"]]:
            report = reports[project]
            coverage = {}
            for project_path, lines in per_project[project].items():
                index = report["files"].setdefault(project_path, len(report["files"]))
                coverage[str(index)] = line_ranges(lines)
            entry = {"uniformPath": uniform_path, "duration": test["duration"], "result": test["result"]}
            if coverage:
                entry["coverage"] = coverage
            # Every test goes into every project's report, also without coverage there, so that TIA knows it exists.
            report["tests"].append(entry)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for project, report in reports.items():
        out = {"version": 2, "partial": args.partial,
               "files": [{"path": p} for p, _ in sorted(report["files"].items(), key=lambda x: x[1])],
               "tests": report["tests"]}
        (args.output_dir / f"{project}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        covered = sum(1 for t in report["tests"] if "coverage" in t)
        print(f"{project}: {len(report['tests'])} tests ({covered} with coverage), {len(report['files'])} files "
              f"-> {args.output_dir / (project + '.json')}")

    print(f"{len(records)} records, {len(tests)} tests, {len(images)} images")
    if executed_bytes:
        print(f"Unattributed executed bytes (no line info): {unattributed} of {executed_bytes} "
              f"({100.0 * unattributed / executed_bytes:.2f}%)")
    if unmapped:
        print(f"{len(unmapped)} covered files match no project and are not reported, e.g.:")
        for path, _ in sorted(unmapped.items(), key=lambda x: -x[1])[:10]:
            print(f"  {path}")


if __name__ == "__main__":
    sys.exit(main())
