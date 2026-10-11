import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_capacity_formatting_and_usage() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for dashboard arithmetic checks")
    source = (Path(__file__).parents[1] / "src/camvault/web_assets/dashboard.js").read_text()
    helpers = source[source.index("  function humanBytes(") : source.index("  function showToast(")]
    checks = r"""
const assert = require('node:assert/strict');
const policy = {max_storage_gb: 3000000000000 / 1024 ** 3};
assert.equal(capacityBytes(policy.max_storage_gb * 1024 ** 3), '3 TB');
assert.equal(capacityBytes(512 * 1024 ** 3), '549.76 GB');
assert.equal(capacityBytes(0), '0 B');
assert.equal(capacityBytes(null), '—');
let s = storageSummary({managed_archive_bytes: 285000000000, total_bytes: 1000, used_bytes: 900}, policy);
assert.equal(s.label, '9.5%');
assert.equal(s.detail, '285 GB 已用 / 3 TB 归档上限');
assert.equal(s.percent, 9.5);
s = storageSummary({managed_archive_bytes: 0}, policy);
assert.equal(s.label, '0.0%');
s = storageSummary({managed_archive_bytes: 3300000000000}, policy);
assert.equal(s.label, '110.0%');
s = storageSummary({total_bytes: 1000, used_bytes: 500, free_bytes: 500}, {});
assert.equal(s.label, '50.0%');
s = storageSummary({managed_archive_bytes: 1024}, {});
assert.equal(s.label, '1.00 KiB');
assert.equal(s.percent, null);
assert.match(s.detail, /使用率未知/);
s = storageSummary({managed_archive_bytes: null, total_bytes: 0}, policy);
assert.equal(s.label, '—');
assert.equal(s.percent, null);
"""
    subprocess.run([node, "-e", helpers + checks], check=True, capture_output=True, text=True)
