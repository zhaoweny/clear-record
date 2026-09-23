"""The `calibrate` command's report: write calibration.json and print it.

The arithmetic itself is the pipeline's
(:func:`clear_record.pipeline.stages.calibration_report`), so the terminal
report and the console's accuracy axis cannot disagree. Writing the artifact
and rendering the raw numbers are the command surface's: they are what the
`calibrate` command promises, not a step of the pipeline.
"""

from __future__ import annotations

from clear_record.core import write_json
from clear_record.pipeline.stages import calibration_report
from clear_record.pipeline.workspace import Workspace


def calibrate_report(directory: str, reference: str | None = None) -> dict:
    """Write the workspace calibration.json and print the raw report."""
    report = calibration_report(directory, reference)
    out = Workspace.at(directory).export_file("calibration.json")
    write_json(out, report)
    print("\n[calibrate] report:")
    for k, v in report.items():
        print(f"  {k:18s} {v}")
    print(f"  -> {out}")
    return report
