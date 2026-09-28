#!/usr/bin/env python3
#
# Copyright (c) 2010-2026 Antmicro
#
# This file is licensed under the MIT License.
# Full license text is available in 'licenses/MIT.txt'.
#

import json
from pathlib import Path


# The name isn't CamelCase typically used for Python classes only because it
# has to match the name of the file and snake_case is used for other listeners.
class testwise_coverage_listener:
    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, manifest_path):
        self.manifest_path = Path(manifest_path)
        self.tests = []

    def end_test(self, data, result):
        if result.passed:
            status = "PASSED"
        elif result.skipped:
            status = "SKIPPED"
        else:
            status = "FAILURE"

        self.tests.append({
            "name": data.name,
            "longname": data.longname,
            "result": status,
            "duration": result.elapsed_time.total_seconds(),
        })

    def close(self):
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps({"tests": self.tests}, indent=2),
            encoding="utf-8",
        )
