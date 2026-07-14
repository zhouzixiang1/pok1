"""Read-only report for the formal cgroup-v2 resource enforcer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..resource_enforcer import (
    RESOURCE_ENFORCER_DIGEST,
    probe_resource_enforcer,
    required_controllers_digest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delegated-root",
        type=Path,
        default=Path(
            os.environ.get("POK_FORMAL_CGROUP_ROOT", "/sys/fs/cgroup/pok-formal")
        ),
    )
    parser.add_argument(
        "--mountinfo", type=Path, default=Path("/proc/self/mountinfo")
    )
    args = parser.parse_args()
    probe = probe_resource_enforcer(
        args.delegated_root,
        mountinfo_path=args.mountinfo,
    )
    payload = probe.to_dict()
    payload["controllers_digest"] = required_controllers_digest()
    payload["enforcer_digest"] = RESOURCE_ENFORCER_DIGEST
    payload["mode"] = "read_only_no_cgroup_creation_no_process_launch"
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
