"""PyInstaller entrypoint for the ApplicantScout companion."""

import sys

from applicant_scout.screenshots_path_probe import dispatch_screenshots_path_probe

FROZEN_STARTUP_IMPORT_PROBE_ARG = "--startup-import-probe"


def _run_frozen_startup_probe() -> int:
    # Keep this path free of config, cache, logging, and single-instance state.
    # It must exercise the native components that normal imports leave lazy:
    # the Windows QPA plugin and zbar's actual scan entry point.
    from PIL import Image
    from PyQt6.QtWidgets import QApplication

    from applicant_scout import __main__ as runtime_main
    from applicant_scout.screenshot import _decode_qr_symbols

    # Exercise every eager overlay/settings/WCL/updater dependency before Qt
    # initialization, matching the normal run_application import order.
    if not callable(runtime_main.main):
        raise RuntimeError("Frozen application entry point is not callable.")
    application = QApplication(["ApplicantScout", FROZEN_STARTUP_IMPORT_PROBE_ARG])
    try:
        application.processEvents()
        if _decode_qr_symbols(Image.new("L", (32, 32), color=255)):
            raise RuntimeError("Blank QR decoder probe returned an unexpected symbol.")
    finally:
        application.quit()
    return 0


def main() -> int:
    args = sys.argv[1:]
    probe_exit_code = dispatch_screenshots_path_probe(args)
    if probe_exit_code is not None:
        return probe_exit_code

    if args == [FROZEN_STARTUP_IMPORT_PROBE_ARG]:
        return _run_frozen_startup_probe()

    # Keep the internal filesystem probe independent from cold GUI imports.
    from applicant_scout.__main__ import main as run_application

    return run_application(args)


if __name__ == "__main__":
    raise SystemExit(main())
