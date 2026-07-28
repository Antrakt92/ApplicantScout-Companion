"""PyInstaller entrypoint for the ApplicantScout companion."""

import sys

from applicant_scout.screenshots_path_probe import dispatch_screenshots_path_probe


def main() -> int:
    args = sys.argv[1:]
    probe_exit_code = dispatch_screenshots_path_probe(args)
    if probe_exit_code is not None:
        return probe_exit_code

    # Keep the internal filesystem probe independent from cold GUI imports.
    from applicant_scout.__main__ import main as run_application

    return run_application(args)


if __name__ == "__main__":
    raise SystemExit(main())
