# Changelog

All notable changes to this project. Dates are when the change reached `main`.

## Unreleased

### Fixed
- **Takeoff counts Revu's Sketch to Scale markups.** Markups drawn with Revu's Sketch to Scale tools
  (`/IT /RectangleSketchToScale` and similar) carry a scale and `/MeasurementTypes` but not a
  `...Dimension` intent, so they were left out of `bb_takeoff_summary`, `bb_list_markups` and the
  exports with no warning. On a real grading plan this dropped 19 of 25 measurements (29,344.04 sf).
  Any markup with a `/Measure` scale and `/MeasurementTypes` now counts, classified by those bits;
  Cloud+, callouts, typewriter text and arrows stay out.

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
