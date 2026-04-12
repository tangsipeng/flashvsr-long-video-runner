# NOTES

## Design choices

1. **Do not touch upstream FlashVSR**
   - This repo imports the upstream infer script dynamically.
   - Weight path handling is done by changing runtime cwd and, when needed, creating a temporary symlinked runtime directory.

2. **Manifest-first orchestration**
   - Planning and execution are separated.
   - The manifest stores exact source frame ranges, render frame ranges, trim offsets, and output paths.
   - Resume can therefore be chunk-index based instead of iterator-position based.

3. **Tail merge heuristic**
   - Trailing tiny source chunks borrow left-context frames from the previous chunk.
   - The final rendered window can be larger than the final source chunk.
   - Output is sliced back to the exact source-owned range.

4. **Testable non-GPU core**
   - Planning, manifest serialization, padding logic, and resume selection are all unit-testable without torch/diffsynth.

## Things I intentionally did not do

- No vendored weights
- No copied upstream model code
- No attempt to support every upstream FlashVSR variant yet
- No heavy state machine for partial concat recovery

## Practical next steps if this becomes production-critical

- add per-chunk metadata validation with frame counts / duration checks
- add optional ffmpeg-based chunk frame extraction for lower Python-side memory pressure
- add chunk completion sidecars or hashes
- add a dry-run report for estimated overlap re-render cost
- test concat compatibility more broadly across codecs / containers
