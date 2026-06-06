"""Unit tests for G-code injection into 3MF files (#422)."""

import tempfile
import zipfile
from pathlib import Path

from backend.app.utils.threemf_tools import (
    _inject_start_at_marker,
    _parse_3mf_gcode_header,
    _substitute_placeholders,
    inject_gcode_into_3mf,
)


def _make_temp_path(suffix=".3mf") -> Path:
    """Create a temp file path without leaving it open (avoids SIM115)."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    import os

    os.close(fd)
    return Path(name)


def _make_test_3mf(gcode_content: str = "G28\nG1 X0 Y0\nM400\n", plate_id: int = 1) -> Path:
    """Create a minimal 3MF file with embedded G-code for testing."""
    tmp_path = _make_temp_path()

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Metadata/plate_{plate_id}.gcode", gcode_content)
        zf.writestr("Metadata/slice_info.config", "<config></config>")
        zf.writestr("3D/3dmodel.model", "<model></model>")

    return tmp_path


class TestInjectGcodeInto3mf:
    """Tests for inject_gcode_into_3mf()."""

    def test_inject_start_gcode(self):
        """Start G-code is prepended before the original content."""
        source = _make_test_3mf("G28\nM400\n")
        try:
            result = inject_gcode_into_3mf(source, 1, "M117 Start\nG92 E0", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.startswith("M117 Start\nG92 E0\n")
            assert "G28\nM400\n" in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_inject_end_gcode(self):
        """End G-code is appended after the original content."""
        source = _make_test_3mf("G28\nM400")
        try:
            result = inject_gcode_into_3mf(source, 1, None, "M104 S0\nG28 X")
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.endswith("M104 S0\nG28 X\n")
            assert gcode.startswith("G28\nM400")
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_inject_both_start_and_end(self):
        """Both start and end G-code are injected."""
        source = _make_test_3mf("G28\n")
        try:
            result = inject_gcode_into_3mf(source, 1, "; START", "; END")
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.startswith("; START\n")
            assert gcode.endswith("; END\n")
            assert "G28" in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_no_injection_returns_none(self):
        """Returns None when both start and end are None."""
        source = _make_test_3mf()
        try:
            result = inject_gcode_into_3mf(source, 1, None, None)
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    def test_empty_strings_returns_none(self):
        """Returns None when both start and end are empty strings."""
        source = _make_test_3mf()
        try:
            result = inject_gcode_into_3mf(source, 1, "", "")
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    def test_plate_id_selection(self):
        """Injects into the correct plate's G-code file."""
        source = _make_temp_path()

        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", "PLATE1\n")
            zf.writestr("Metadata/plate_2.gcode", "PLATE2\n")

        try:
            result = inject_gcode_into_3mf(source, 2, "; INJECTED", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                plate1 = zf.read("Metadata/plate_1.gcode").decode("utf-8")
                plate2 = zf.read("Metadata/plate_2.gcode").decode("utf-8")

            # Only plate 2 should be modified
            assert plate1 == "PLATE1\n"
            assert plate2.startswith("; INJECTED\n")
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_preserves_other_files(self):
        """Non-gcode files in the 3MF are preserved unchanged."""
        source = _make_test_3mf()
        try:
            result = inject_gcode_into_3mf(source, 1, "; START", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                names = zf.namelist()
                assert "Metadata/slice_info.config" in names
                assert "3D/3dmodel.model" in names
                config = zf.read("Metadata/slice_info.config").decode("utf-8")
                assert config == "<config></config>"
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_no_gcode_file_returns_none(self):
        """Returns None when the 3MF has no gcode files."""
        source = _make_temp_path()

        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("3D/3dmodel.model", "<model></model>")

        try:
            result = inject_gcode_into_3mf(source, 1, "; START", None)
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    def test_invalid_file_returns_none(self):
        """Returns None for a non-ZIP file."""
        source = _make_temp_path()
        source.write_bytes(b"not a zip file")

        try:
            result = inject_gcode_into_3mf(source, 1, "; START", None)
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    def test_fallback_to_first_gcode(self):
        """Falls back to first gcode file when plate-specific not found."""
        source = _make_temp_path()

        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", "ORIGINAL\n")

        try:
            # Request plate 5 which doesn't exist — should fall back to plate_1
            result = inject_gcode_into_3mf(source, 5, "; INJECTED", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.startswith("; INJECTED\n")
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_original_file_unchanged(self):
        """The source 3MF is never modified."""
        source = _make_test_3mf("ORIGINAL\n")
        try:
            result = inject_gcode_into_3mf(source, 1, "; START", "; END")
            assert result is not None

            # Verify original is untouched
            with zipfile.ZipFile(source, "r") as zf:
                original = zf.read("Metadata/plate_1.gcode").decode("utf-8")
            assert original == "ORIGINAL\n"
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)


# Realistic Bambu / Orca header + startup block — the start-gcode marker is the
# anchor point #422 reviewers (DevScarabyte, pleite) reported as the correct
# injection point. Snippets injected before this should land *after* the bed
# heat / homing / nozzle prime sequence, not before it.
_BAMBU_GCODE_TEMPLATE = """\
; HEADER_BLOCK_START
; BambuStudio 02.06.00.51
; total layer number: 80
; total filament length [mm] : 12155.34
; total filament weight [g] : 36.55
; max_z_height: 16.00
; HEADER_BLOCK_END
; MACHINE_START_GCODE_BEGIN
M104 S220 ; preheat
G28 ; home
M109 S220 ; wait for nozzle
G92 E0 ; reset extruder
; MACHINE_START_GCODE_END
G1 X10 Y10 Z0.2
G1 X100 Y100 E5
M104 S0
"""


class TestStartAnchoredInjection:
    """Tests for #422 follow-up: start g-code injected at MACHINE_START_GCODE_END."""

    def test_start_lands_after_printer_startup(self):
        """Start snippet sits immediately before MACHINE_START_GCODE_END, not at file head."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        try:
            result = inject_gcode_into_3mf(source, 1, "; SWAPMOD-START", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            # Original file head is preserved — snippet does NOT prepend.
            assert gcode.startswith("; HEADER_BLOCK_START\n")
            # Snippet sits right above the marker.
            marker_idx = gcode.index("; MACHINE_START_GCODE_END")
            snippet_idx = gcode.index("; SWAPMOD-START")
            assert snippet_idx < marker_idx
            # Nothing else between snippet and marker except the trailing newline.
            between = gcode[snippet_idx:marker_idx]
            assert between == "; SWAPMOD-START\n"
            # Printer's own startup commands still come BEFORE the snippet.
            startup_idx = gcode.index("M109 S220")
            assert startup_idx < snippet_idx
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_no_marker_falls_back_to_prepend(self):
        """Files without MACHINE_START_GCODE_END (older slicers) keep prepend behaviour."""
        source = _make_test_3mf("G28\nM400\n")
        try:
            result = inject_gcode_into_3mf(source, 1, "; LEGACY-START", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.startswith("; LEGACY-START\n")
            assert "G28" in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_end_still_appended_at_eof(self):
        """End g-code keeps the existing append-to-EOF behaviour even with marker present."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        try:
            result = inject_gcode_into_3mf(source, 1, None, "; SWAPMOD-END")
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.endswith("; SWAPMOD-END\n")
            # Marker anchor is irrelevant for end snippets.
            assert gcode.index("; SWAPMOD-END") > gcode.index("; MACHINE_START_GCODE_END")
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)


class TestPlaceholderSubstitution:
    """Tests for #422 follow-up: {placeholder} substitution from 3MF header values."""

    def test_max_z_height_substituted_in_end_snippet(self):
        """`G1 Z{max_layer_z}` resolves to the model's actual top-layer Z (DevScarabyte safety bug)."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        try:
            # Prusa-style alias: max_layer_z → max_z_height in the Bambu header
            result = inject_gcode_into_3mf(source, 1, None, "G1 Z{max_layer_z} F600")
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            # max_z_height in the template is 16.00 — the dangerous Z1 fallback is gone.
            assert "G1 Z16.00 F600" in gcode
            assert "{max_layer_z}" not in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_direct_header_key_lookup(self):
        """Snippets can reference normalised header keys directly without going through aliases."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        try:
            result = inject_gcode_into_3mf(
                source, 1, None, "; layers={total_layer_number} weight={total_filament_weight}"
            )
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert "; layers=80 weight=36.55" in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_unknown_placeholder_left_intact(self):
        """A typo or unsupported placeholder is preserved verbatim instead of becoming empty."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        try:
            result = inject_gcode_into_3mf(source, 1, None, "; nope={does_not_exist}")
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert "; nope={does_not_exist}" in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)

    def test_no_placeholders_no_header_required(self):
        """Snippets without placeholders inject correctly even when the header is absent."""
        source = _make_test_3mf("G28\nM400\n")
        try:
            result = inject_gcode_into_3mf(source, 1, "; PLAIN", None)
            assert result is not None

            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")

            assert gcode.startswith("; PLAIN\n")
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)


class TestHeaderParser:
    """Direct tests for `_parse_3mf_gcode_header`."""

    def test_parses_bambu_header_block(self):
        header = _parse_3mf_gcode_header(_BAMBU_GCODE_TEMPLATE)
        assert header["max_z_height"] == "16.00"
        assert header["total_layer_number"] == "80"
        # Units suffix is stripped from the key.
        assert header["total_filament_length"] == "12155.34"
        assert header["total_filament_weight"] == "36.55"

    def test_ignores_lines_outside_header_block(self):
        content = "; HEADER_BLOCK_START\n; key: in\n; HEADER_BLOCK_END\n; key: out\n"
        header = _parse_3mf_gcode_header(content)
        assert header == {"key": "in"}

    def test_returns_empty_when_no_header(self):
        assert _parse_3mf_gcode_header("G28\nG1 X0\n") == {}


class TestPlaceholderHelper:
    """Direct tests for `_substitute_placeholders`."""

    def test_substitutes_known_keys(self):
        assert _substitute_placeholders("Z={a} F={b}", {"a": "10", "b": "600"}) == "Z=10 F=600"

    def test_alias_resolves_to_underlying_key(self):
        assert _substitute_placeholders("Z={max_layer_z}", {"max_z_height": "16.00"}) == "Z=16.00"

    def test_unknown_left_verbatim(self):
        assert _substitute_placeholders("{nope}", {}) == "{nope}"


class TestStartMarkerHelper:
    """Direct tests for `_inject_start_at_marker`."""

    def test_inserts_before_marker_line(self):
        content = "first\nsecond\n; MACHINE_START_GCODE_END\ntail\n"
        result = _inject_start_at_marker(content, "INJECTED")
        assert result == "first\nsecond\nINJECTED\n; MACHINE_START_GCODE_END\ntail\n"

    def test_marker_at_start_of_file(self):
        content = "; MACHINE_START_GCODE_END\nrest\n"
        result = _inject_start_at_marker(content, "INJECTED")
        assert result == "INJECTED\n; MACHINE_START_GCODE_END\nrest\n"

    def test_missing_marker_falls_back_to_prepend(self):
        content = "G28\nG1 X0\n"
        result = _inject_start_at_marker(content, "INJECTED")
        assert result == "INJECTED\nG28\nG1 X0\n"


class TestArithmeticPlaceholders:
    """Tests for `{expr}` arithmetic in snippets (follow-up to #422 placeholders).

    A bare key still substitutes the raw header string verbatim; an expression
    over header keys (`{max_z_height - 5}`, `{max_z_height / 2}`) is evaluated
    to a number. Anything unresolvable is left intact with a warning.
    """

    HEADER = {"max_z_height": "16.00", "total_layer_number": "80", "total_filament_weight": "36.55"}

    def test_subtraction(self):
        assert _substitute_placeholders("G1 Z{max_z_height - 5} F600", self.HEADER) == "G1 Z11 F600"

    def test_division(self):
        assert _substitute_placeholders("G1 Z{max_z_height / 2}", self.HEADER) == "G1 Z8"

    def test_multiplication(self):
        assert _substitute_placeholders("{total_layer_number * 2}", self.HEADER) == "160"

    def test_addition(self):
        assert _substitute_placeholders("Z{max_z_height + 2}", self.HEADER) == "Z18"

    def test_non_integer_result_keeps_decimals(self):
        # 16.00 / 3 = 5.3333… — trimmed to 4 dp, trailing zeros removed.
        assert _substitute_placeholders("Z{max_z_height / 3}", self.HEADER) == "Z5.3333"

    def test_parentheses_and_precedence(self):
        assert _substitute_placeholders("{(max_z_height - 1) * 2}", self.HEADER) == "30"

    def test_alias_inside_expression(self):
        # max_layer_z is an alias for max_z_height.
        assert _substitute_placeholders("Z{max_layer_z - 6}", self.HEADER) == "Z10"

    def test_three_layer_sweep_sequence(self):
        """The motivating case: sweep at full height, half height, then near-zero."""
        snippet = "G1 Z{max_z_height}\nG1 Z{max_z_height / 2}\nG1 Z0.2"
        result = _substitute_placeholders(snippet, self.HEADER)
        # Bare key keeps its raw "16.00"; computed values are trimmed.
        assert result == "G1 Z16.00\nG1 Z8\nG1 Z0.2"

    def test_bare_key_unchanged_by_arithmetic_support(self):
        """Plain `{key}` still returns the verbatim header string, not a reformatted number."""
        assert _substitute_placeholders("w={total_filament_weight}", self.HEADER) == "w=36.55"

    def test_unknown_variable_in_expression_left_verbatim(self):
        assert _substitute_placeholders("Z{does_not_exist - 5}", self.HEADER) == "Z{does_not_exist - 5}"

    def test_division_by_zero_left_verbatim(self):
        assert _substitute_placeholders("Z{max_z_height / 0}", self.HEADER) == "Z{max_z_height / 0}"

    def test_disallowed_operator_left_verbatim(self):
        # Power (**) is not in the whitelist — must not evaluate.
        assert _substitute_placeholders("Z{max_z_height ** 2}", self.HEADER) == "Z{max_z_height ** 2}"

    def test_function_call_rejected(self):
        # abs() is not in the function whitelist (only min/max/clamp) → left verbatim.
        assert _substitute_placeholders("Z{abs(max_z_height)}", self.HEADER) == "Z{abs(max_z_height)}"

    def test_clamp_passthrough_in_range(self):
        # 16 - 4 = 12, within [1.5, 176] → unchanged.
        assert _substitute_placeholders("Z{clamp(max_z_height - 4, 1.5, 176)}", self.HEADER) == "Z12"

    def test_clamp_lower_bound_protects_short_print(self):
        # The safety case: a 2mm print would give 2-4 = -2 (bed into nozzle); clamped to 1.5.
        assert _substitute_placeholders("Z{clamp(max_z_height - 4, 1.5, 176)}", {"max_z_height": "2"}) == "Z1.5"

    def test_clamp_upper_bound_caps_tall_print(self):
        # 240 + 26 = 266; capped to the eject ceiling 206.
        assert _substitute_placeholders("Z{clamp(max_z_height + 26, 31.5, 206)}", {"max_z_height": "240"}) == "Z206"

    def test_min_and_max(self):
        assert _substitute_placeholders("{min(max_z_height, 10)} {max(max_z_height, 10)}", self.HEADER) == "10 16"

    def test_nested_clamp_arithmetic(self):
        # clamp result composes with further arithmetic: clamp(12,1.5,176)+30 = 42.
        assert _substitute_placeholders("Z{clamp(max_z_height - 4, 1.5, 176) + 30}", self.HEADER) == "Z42"

    def test_clamp_wrong_arity_left_verbatim(self):
        assert _substitute_placeholders("Z{clamp(max_z_height, 1.5)}", self.HEADER) == "Z{clamp(max_z_height, 1.5)}"

    def test_unknown_function_left_verbatim(self):
        assert _substitute_placeholders("Z{sqrt(max_z_height)}", self.HEADER) == "Z{sqrt(max_z_height)}"

    def test_non_numeric_header_value_in_expression_left_verbatim(self):
        header = {"slicer": "BambuStudio"}
        assert _substitute_placeholders("{slicer - 1}", header) == "{slicer - 1}"

    def test_end_to_end_through_inject(self):
        """Arithmetic resolves when injected through the public 3MF entry point."""
        source = _make_test_3mf(_BAMBU_GCODE_TEMPLATE)
        result = None
        try:
            # Template header carries max_z_height: 16.00 → sweep at 11.
            result = inject_gcode_into_3mf(source, 1, None, "G1 Z{max_layer_z - 5} F600")
            assert result is not None
            with zipfile.ZipFile(result, "r") as zf:
                gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8")
            assert "G1 Z11 F600" in gcode
            assert "{max_layer_z" not in gcode
        finally:
            source.unlink(missing_ok=True)
            if result:
                result.unlink(missing_ok=True)


class TestArithmeticAstBoundaries:
    """Adversarial / grammar-boundary tests for the restricted AST walker.

    The point of using an AST allowlist instead of eval() is that everything
    outside `{+ - * /}`, unary `+/-`, numeric literals, header names and the
    min/max/clamp calls must be *rejected* and left verbatim — never executed,
    never silently dropped. These lock that boundary down.
    """

    HEADER = {"max_z_height": "16.00", "slicer": "BambuStudio"}

    def _verbatim(self, expr: str):
        snippet = "Z{" + expr + "}"
        assert _substitute_placeholders(snippet, self.HEADER) == snippet

    # --- disallowed operators (each must NOT evaluate) ---
    def test_modulo_rejected(self):
        self._verbatim("max_z_height % 3")

    def test_floor_division_rejected(self):
        self._verbatim("max_z_height // 3")

    def test_bitwise_and_rejected(self):
        self._verbatim("max_z_height & 1")

    def test_bitwise_xor_rejected(self):
        self._verbatim("max_z_height ^ 1")

    def test_left_shift_rejected(self):
        self._verbatim("max_z_height << 2")

    def test_comparison_rejected(self):
        self._verbatim("max_z_height > 1")

    def test_boolean_op_rejected(self):
        self._verbatim("max_z_height and 1")

    # --- attribute / subscript / dunder reach-arounds (security) ---
    def test_attribute_access_rejected(self):
        self._verbatim("max_z_height.__class__")

    def test_subscript_rejected(self):
        self._verbatim("max_z_height[0]")

    def test_dunder_import_call_rejected(self):
        # The classic eval() escape — must never resolve a function name we
        # didn't whitelist, let alone a builtin.
        self._verbatim("__import__('os')")

    def test_lambda_rejected(self):
        self._verbatim("(lambda: 1)()")

    def test_walrus_rejected(self):
        self._verbatim("(x := 1)")

    # --- non-numeric / wrong literal types ---
    def test_boolean_literal_rejected(self):
        # True is an int subclass but explicitly excluded — must not become "1".
        self._verbatim("True + 1")

    def test_string_literal_rejected(self):
        self._verbatim("'16' + 1")

    def test_keyword_argument_rejected(self):
        self._verbatim("clamp(max_z_height, lo=1, hi=2)")

    def test_min_max_with_kwargs_rejected(self):
        self._verbatim("max(max_z_height, key=1)")

    # --- syntax / empty edge cases ---
    def test_empty_braces_left_verbatim(self):
        # `{ }` is whitespace only → empty expression → SyntaxError → verbatim.
        assert _substitute_placeholders("Z{ }", self.HEADER) == "Z{ }"

    def test_trailing_garbage_rejected(self):
        self._verbatim("max_z_height - ")

    def test_multiple_statements_rejected(self):
        # A comma makes it a tuple expr, not arithmetic → rejected.
        self._verbatim("max_z_height, 1")

    # --- positive boundary cases that MUST still work ---
    def test_unary_minus_on_variable(self):
        assert _substitute_placeholders("Z{-max_z_height}", self.HEADER) == "Z-16"

    def test_double_unary(self):
        assert _substitute_placeholders("Z{--max_z_height}", self.HEADER) == "Z16"

    def test_min_with_three_args(self):
        assert _substitute_placeholders("{min(max_z_height, 20, 5)}", self.HEADER) == "5"

    def test_deeply_nested_parens(self):
        assert _substitute_placeholders("Z{((((max_z_height))))}", self.HEADER) == "Z16"

    def test_negative_literal_in_clamp(self):
        # clamp must accept negative bounds without tripping the unary path.
        assert _substitute_placeholders("Z{clamp(-5, -10, -1)}", self.HEADER) == "Z-5"
