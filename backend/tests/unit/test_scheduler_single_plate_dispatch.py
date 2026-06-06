"""Wiring tests for the single-plate dispatch fix in ``print_scheduler.py``.

"Send all" uploads one consolidated multi-plate 3MF (~56MB) and the VP queue
enqueues one item per plate, all pointing at that same archive. Uploading the
full file once per plate (56MB x N) times out over weak WiFi. The fix extracts
just the item's plate into a small single-plate 3MF (~6MB) and uploads that.

Exercising the full ``_start_print`` requires a real DB + printer_manager +
ams fixture stack, so — mirroring ``test_scheduler_force_timelapse_wiring.py`` —
this pins the wiring at the source/AST level:

- ``extract_single_plate_3mf(file_path, item.plate_id)`` is called and its
  result reassigns ``file_path`` (so the *small* file is what gets injected
  and uploaded), BEFORE the g-code injection block.
- The extracted temp file is cleaned up after the upload attempt.

A behavioural test of the util itself (only the target plate survives, base
files kept, size shrinks) lives in ``test_threemf_tools.py``.
"""

import ast
from pathlib import Path

SCHEDULER_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "services" / "print_scheduler.py"


def _start_print_source() -> str:
    return SCHEDULER_PATH.read_text()


def _find_extract_call(tree: ast.AST) -> ast.Call:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "extract_single_plate_3mf":
                return node
    raise AssertionError("extract_single_plate_3mf(...) is not called in print_scheduler.py")


def test_extract_single_plate_is_called_with_item_plate_id():
    tree = ast.parse(_start_print_source())
    call = _find_extract_call(tree)
    assert len(call.args) == 2, "extract_single_plate_3mf(source, plate_id) takes two positional args"
    plate_arg = call.args[1]
    # Second arg must reference item.plate_id (the per-item target plate).
    assert isinstance(plate_arg, ast.Attribute) and plate_arg.attr == "plate_id", (
        "extract_single_plate_3mf must be called with item.plate_id as the plate"
    )
    assert isinstance(plate_arg.value, ast.Name) and plate_arg.value.id == "item"


def test_extraction_runs_before_injection():
    """The extracted small file must be the one injection operates on, so
    the extract block has to precede the injection block."""
    source = _start_print_source()
    extract_idx = source.index("extract_single_plate_3mf(file_path")
    injection_idx = source.index("inject_gcode_into_3mf")
    assert extract_idx < injection_idx, "extraction must run before g-code injection"


def test_extracted_path_reassigns_file_path():
    source = _start_print_source()
    # The result becomes file_path so the small file is uploaded.
    assert "file_path = extracted_path" in source


def test_extracted_temp_file_cleaned_up():
    source = _start_print_source()
    assert "extracted_path.unlink(missing_ok=True)" in source


def test_extraction_is_guarded_by_plate_id():
    """No plate_id → no extraction (single-source / library prints unchanged)."""
    source = _start_print_source()
    assert "if item.plate_id:" in source
