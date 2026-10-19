# Changelog

All notable changes to this project. Dates are when the change reached `main`; the Sketch to Scale fix merged in #1.

## Unreleased

### Added
- **Runs without Windows.** pywin32 is now optional, so the server starts on macOS, Linux and cloud
  containers. Every PDF tool works there; `bb_revu_status` reports that Revu is unavailable and
  `bb_open_in_revu` refuses with the reason. CI also runs the suite on Ubuntu.
- **Takeoff warns instead of dropping.** A markup that carries a measurement scale but has no
  measurement type the server recognises is now listed in the takeoff `warnings` and counted in
  `skipped`, rather than vanishing from the totals.

### Fixed
- **Takeoff counts Revu's Sketch to Scale markups.** Markups drawn with Revu's Sketch to Scale tools
  (`/IT /RectangleSketchToScale` and similar) carry a scale and `/MeasurementTypes` but not a
  `...Dimension` intent, so they were left out of `bb_takeoff_summary`, `bb_list_markups` and the
  exports with no warning. On a real grading plan this dropped 19 of 25 measurements (29,344.04 sf).
  Any markup with a `/Measure` scale and `/MeasurementTypes` now counts, classified by those bits;
  Cloud+, callouts, typewriter text and arrows stay out.

- Tests no longer leave stray `C:\Windows\...` files in the working folder off Windows (the
  protected-folder tests run only on Windows).

### Known issues
- Resizing a Sketch to Scale markup with `bb_edit_markups` does not recompute its displayed value.

## 2026-09-28

### Added
- First release: takeoff quantities, Markups List export (CSV, Excel, JSON), page rendering, text
  search and sheet index, Bluebeam-style markup writers, review tools, page operations and
  `Revu.Launcher` integration.
- Takeoff grouping by fill color and depth.
- Excel export: `group_by` for the Takeoff sheet, Fill Color column, colour swatches and number
  formats.
