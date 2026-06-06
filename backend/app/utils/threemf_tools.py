"""3MF file parsing utilities for filament tracking.

This module provides functions to parse Bambu Lab 3MF files and extract
per-layer filament usage data from the embedded G-code. This enables
accurate partial usage reporting for multi-material prints.
"""

import ast
import hashlib
import json
import logging
import math
import operator
import re
import tempfile
import zipfile
from pathlib import Path

import defusedxml.ElementTree as ET

logger = logging.getLogger(__name__)

# Default filament properties
DEFAULT_FILAMENT_DIAMETER = 1.75  # mm
DEFAULT_FILAMENT_DENSITY = 1.24  # g/cm³ (PLA)


def parse_gcode_layer_filament_usage(gcode_content: str) -> dict[int, dict[int, float]]:
    """Parse G-code to extract per-layer, per-filament cumulative extrusion in mm.

    This function tracks filament extrusion across layers and tool changes,
    building a cumulative usage map that can be used to calculate partial
    usage at any layer.

    Args:
        gcode_content: The raw G-code content as a string

    Returns:
        A nested dictionary mapping layer numbers to filament usage:
        {layer: {filament_id: cumulative_mm}, ...}

    Example:
        {0: {0: 125.5}, 1: {0: 250.0, 1: 50.0}, 2: {0: 375.0, 1: 150.0}}

        This shows:
        - Layer 0: filament 0 used 125.5mm cumulative
        - Layer 1: filament 0 used 250mm cumulative, filament 1 used 50mm
        - Layer 2: filament 0 used 375mm cumulative, filament 1 used 150mm

    G-code commands parsed:
        - M73 L<layer>: Layer change marker
        - M620 S<filament>: Filament/tool change (S255 = unload)
        - G0/G1/G2/G3 E<amount>: Extrusion moves
    """
    layer_filaments: dict[int, dict[int, float]] = {}
    current_layer = 0
    active_filament: int | None = None
    cumulative_extrusion: dict[int, float] = {}  # filament_id -> total mm

    for line in gcode_content.splitlines():
        line = line.strip()
        if not line:
            continue

        # Handle comments - skip but check for layer markers
        if line.startswith(";"):
            # Some slicers use comment-based layer markers
            # e.g., "; CHANGE_LAYER" or ";LAYER_CHANGE"
            continue

        # Split line into command and inline comment
        if ";" in line:
            line = line.split(";")[0].strip()

        # Extract command and parameters
        parts = line.split()
        if not parts:
            continue
        cmd = parts[0].upper()

        # Layer change: M73 L<layer>
        # Bambu printers use M73 with L parameter for layer indication
        if cmd == "M73":
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("L"):
                    try:
                        new_layer = int(part[1:])
                        # Save current state before layer change
                        if cumulative_extrusion:
                            layer_filaments[current_layer] = cumulative_extrusion.copy()
                        current_layer = new_layer
                    except ValueError:
                        pass  # Skip G-code lines with unparseable layer numbers

        # Filament change: M620 S<filament>
        # Bambu uses M620 for AMS filament switching
        # S255 means full unload (no active filament)
        elif cmd == "M620":
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("S"):
                    filament_str = part[1:]
                    if filament_str == "255":
                        # Full unload - no active filament
                        active_filament = None
                    else:
                        try:
                            # Extract digits (e.g., "0A" -> 0, "1" -> 1)
                            match = re.match(r"(\d+)", filament_str)
                            if match:
                                active_filament = int(match.group(1))
                        except (ValueError, AttributeError):
                            pass  # Skip unparseable filament switch commands

        # Extrusion moves: G0/G1/G2/G3 with E parameter
        # Only G1 typically has extrusion, but check all for safety
        elif cmd in ("G0", "G1", "G2", "G3"):
            if active_filament is None:
                continue
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("E"):
                    try:
                        extrusion = float(part[1:])
                        # Only count positive extrusion (not retractions)
                        if extrusion > 0:
                            current = cumulative_extrusion.get(active_filament, 0)
                            cumulative_extrusion[active_filament] = current + extrusion
                    except ValueError:
                        pass  # Skip G-code lines with unparseable extrusion values

    # Save final layer state
    if cumulative_extrusion:
        layer_filaments[current_layer] = cumulative_extrusion.copy()

    return layer_filaments


def mm_to_grams(
    length_mm: float,
    diameter_mm: float = DEFAULT_FILAMENT_DIAMETER,
    density_g_cm3: float = DEFAULT_FILAMENT_DENSITY,
) -> float:
    """Convert filament length in mm to weight in grams.

    Uses the formula: mass = volume × density
    where volume = π × r² × length

    Args:
        length_mm: Length of filament in millimeters
        diameter_mm: Filament diameter in millimeters (default: 1.75)
        density_g_cm3: Material density in g/cm³ (default: 1.24 for PLA)

    Returns:
        Weight in grams
    """
    radius_cm = (diameter_mm / 2) / 10  # Convert mm to cm
    length_cm = length_mm / 10  # Convert mm to cm
    volume_cm3 = math.pi * radius_cm * radius_cm * length_cm
    return volume_cm3 * density_g_cm3


def extract_layer_filament_usage_from_3mf(file_path: Path) -> dict[int, dict[int, float]] | None:
    """Extract per-layer filament usage from a 3MF file's embedded G-code.

    Args:
        file_path: Path to the 3MF file

    Returns:
        Dictionary mapping layers to filament usage, or None if parsing fails.
        Format: {layer: {filament_id: cumulative_mm}, ...}
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Find G-code file(s) - usually plate_1.gcode or Metadata/plate_1.gcode
            gcode_files = [f for f in zf.namelist() if f.endswith(".gcode")]
            if not gcode_files:
                return None

            # Use the first G-code file (typically only one per 3MF export)
            gcode_path = gcode_files[0]
            gcode_content = zf.read(gcode_path).decode("utf-8", errors="ignore")

            return parse_gcode_layer_filament_usage(gcode_content)
    except Exception:
        return None


def get_cumulative_usage_at_layer(
    layer_usage: dict[int, dict[int, float]],
    target_layer: int,
) -> dict[int, float]:
    """Get cumulative filament usage (in mm) up to and including target_layer.

    Args:
        layer_usage: The output from parse_gcode_layer_filament_usage()
        target_layer: The layer number to get usage for

    Returns:
        Dictionary of {filament_id: cumulative_mm} for each filament used
        up to target_layer. Returns empty dict if no data available.
    """
    if not layer_usage:
        return {}

    # Find the highest recorded layer <= target_layer
    # (we store snapshots at layer changes, so we need the closest one)
    relevant_layers = [layer for layer in layer_usage if layer <= target_layer]
    if not relevant_layers:
        return {}

    max_layer = max(relevant_layers)
    return layer_usage.get(max_layer, {})


def extract_filament_properties_from_3mf(file_path: Path) -> dict[int, dict]:
    """Extract filament properties (density, diameter, type) from 3MF metadata.

    Args:
        file_path: Path to the 3MF file

    Returns:
        Dictionary mapping filament IDs to their properties:
        {filament_id: {"diameter": 1.75, "density": 1.24, "type": "PLA"}, ...}

        Note: filament_id is 1-based (matches slot_id in slice_info.config)
    """
    properties: dict[int, dict] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Try slice_info.config first for filament types
            if "Metadata/slice_info.config" in zf.namelist():
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)
                for f in root.findall(".//filament"):
                    try:
                        # id is 1-based in slice_info.config
                        fid = int(f.get("id", 0))
                        properties[fid] = {
                            "type": f.get("type", "PLA"),
                            "diameter": DEFAULT_FILAMENT_DIAMETER,
                            "density": DEFAULT_FILAMENT_DENSITY,
                        }
                    except ValueError:
                        pass  # Skip filament entries with unparseable IDs

            # Try project_settings.config for density values
            if "Metadata/project_settings.config" in zf.namelist():
                content = zf.read("Metadata/project_settings.config").decode()
                try:
                    data = json.loads(content)
                    densities = data.get("filament_density", [])
                    for i, density in enumerate(densities):
                        # project_settings uses 0-based indexing, convert to 1-based
                        fid = i + 1
                        if fid not in properties:
                            properties[fid] = {
                                "type": "",
                                "diameter": DEFAULT_FILAMENT_DIAMETER,
                            }
                        try:
                            properties[fid]["density"] = float(density)
                        except (ValueError, TypeError):
                            properties[fid]["density"] = DEFAULT_FILAMENT_DENSITY
                except json.JSONDecodeError:
                    pass  # Skip malformed project_settings.config JSON
    except Exception:
        pass  # Return whatever properties were collected before the error

    return properties


def _first_settings_id(value: object) -> str | None:
    """A ``*_settings_id`` value is usually a string, occasionally a list (one
    entry per extruder). Return the first non-empty string, else None."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def extract_embedded_presets_from_3mf(zf: zipfile.ZipFile) -> dict[str, str | None]:
    """Read the printer / process preset names a 3MF project was prepared with.

    BambuStudio / OrcaSlicer write the chosen preset names into
    ``Metadata/project_settings.config`` (``printer_settings_id`` and
    ``print_settings_id``). The SliceModal uses them to default its printer
    and process dropdowns to what the file was sliced for (#1325) instead of
    blindly taking the first listed preset.

    Returns ``{"printer": <name|None>, "process": <name|None>}``. Every failure
    mode (missing config, malformed JSON, unexpected shape) yields ``None``
    values so the modal falls back to its own defaults.
    """
    result: dict[str, str | None] = {"printer": None, "process": None}
    try:
        if "Metadata/project_settings.config" not in zf.namelist():
            return result
        data = json.loads(zf.read("Metadata/project_settings.config").decode())
    except (KeyError, ValueError, OSError):
        return result
    if not isinstance(data, dict):
        return result
    result["printer"] = _first_settings_id(data.get("printer_settings_id"))
    result["process"] = _first_settings_id(data.get("print_settings_id"))
    return result


def extract_nozzle_mapping_from_3mf(zf: zipfile.ZipFile) -> dict[int, int] | None:
    """Extract per-slot nozzle/extruder mapping from a 3MF file.

    On dual-nozzle printers (H2D, H2D Pro), each filament slot is assigned to a
    specific nozzle. The slicer may override user preferences when using "Auto For
    Flush" mode, so the actual assignment comes from slice_info.config group_id
    attributes, not from the user's filament_nozzle_map preference.

    Priority:
        1. group_id on <filament> elements in slice_info.config (actual assignment)
        2. filament_nozzle_map in project_settings.config (user preference fallback)

    Both are mapped through physical_extruder_map to get MQTT extruder IDs (0=right, 1=left).

    Args:
        zf: An open ZipFile of the 3MF archive

    Returns:
        Dictionary mapping {slot_id: extruder_id} for dual-nozzle files,
        or None if single-nozzle, missing data, or parse error.
    """
    try:
        if "Metadata/project_settings.config" not in zf.namelist():
            return None

        content = zf.read("Metadata/project_settings.config").decode()
        data = json.loads(content)

        physical_extruder_map = data.get("physical_extruder_map")
        if not physical_extruder_map or len(physical_extruder_map) <= 1:
            return None  # Single-nozzle printer

        # Check if only one extruder is active.
        # If so, we can skip the mapping and just assign all slots to that extruder.
        # extruder_nozzle_stats format: ["Standard#0|High Flow#0", "Standard#1"]
        # Each entry = one extruder. Format: <NozzleVolumeType>#<count>[|...]
        # #N is the count of physical nozzles of that type (0 = none installed).
        # Types: Standard, High Flow, Hybrid, TPU High Flow

        active_extruders = []
        for stats_str in data.get("extruder_nozzle_stats") or []:
            nozzle_counts = [n.partition("#")[2] for n in stats_str.split("|")]
            active_extruders.append(1 if any(c not in ("0", "") for c in nozzle_counts) else 0)

        if sum(active_extruders) == 1:
            nozzle_mapping: dict[int, int] = {}
            active_idx = active_extruders.index(1)
            target_extruder = int(physical_extruder_map[active_idx])
            if "Metadata/slice_info.config" in zf.namelist():
                si_content = zf.read("Metadata/slice_info.config").decode()
                si_root = ET.fromstring(si_content)
                for filament_elem in si_root.findall(".//filament"):
                    try:
                        nozzle_mapping[int(filament_elem.get("id"))] = target_extruder
                    except (ValueError, TypeError):
                        pass
            return nozzle_mapping or None

        # Priority 1: Use group_id from slice_info filament elements.
        # This reflects the actual slicer assignment (respects "Auto For Flush").
        nozzle_mapping: dict[int, int] = {}
        if "Metadata/slice_info.config" in zf.namelist():
            si_content = zf.read("Metadata/slice_info.config").decode()
            si_root = ET.fromstring(si_content)
            for filament_elem in si_root.findall(".//filament"):
                group_id_str = filament_elem.get("group_id")
                filament_id_str = filament_elem.get("id")
                if group_id_str is not None and filament_id_str:
                    try:
                        group_id = int(group_id_str)
                        slot_id = int(filament_id_str)
                        if group_id < len(physical_extruder_map):
                            nozzle_mapping[slot_id] = int(physical_extruder_map[group_id])
                    except (ValueError, TypeError, IndexError):
                        pass

        if nozzle_mapping:
            return nozzle_mapping

        # Priority 2: Fall back to filament_nozzle_map (user preference).
        # This is correct when the user manually assigned nozzles, but may be
        # wrong when the slicer overrides via "Auto For Flush".
        filament_nozzle_map = data.get("filament_nozzle_map")
        if not filament_nozzle_map:
            return None

        for i, slicer_ext_str in enumerate(filament_nozzle_map):
            slot_id = i + 1
            try:
                slicer_ext = int(slicer_ext_str)
                if slicer_ext < len(physical_extruder_map):
                    nozzle_mapping[slot_id] = int(physical_extruder_map[slicer_ext])
            except (ValueError, TypeError, IndexError):
                pass

        return nozzle_mapping if nozzle_mapping else None
    except Exception:
        return None


def extract_filament_usage_from_3mf(file_path: Path, plate_id: int | None = None) -> list[dict]:
    """Extract per-filament total usage from 3MF slice_info.config.

    This extracts the slicer-estimated total usage per filament slot,
    not the per-layer breakdown.

    Args:
        file_path: Path to the 3MF file
        plate_id: Optional plate index to filter for (for multi-plate files)

    Returns:
        List of filament usage dictionaries:
        [{"slot_id": 1, "used_g": 50.5, "type": "PLA", "color": "#FF0000"}, ...]
    """
    filament_usage = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return []

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            if plate_id is not None:
                # Find the plate element with matching index
                for plate_elem in root.findall(".//plate"):
                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                plate_index = int(meta.get("value", "0"))
                            except ValueError:
                                pass
                            break

                    if plate_index == plate_id:
                        for f in plate_elem.findall("filament"):
                            filament_id = f.get("id")
                            used_g = f.get("used_g", "0")
                            try:
                                used_amount = float(used_g)
                                if filament_id:
                                    filament_usage.append(
                                        {
                                            "slot_id": int(filament_id),
                                            "used_g": used_amount,
                                            "type": f.get("type", ""),
                                            "color": f.get("color", ""),
                                        }
                                    )
                            except (ValueError, TypeError):
                                pass
                        break
            else:
                # No plate_id specified - extract all filaments
                for f in root.findall(".//filament"):
                    filament_id = f.get("id")
                    used_g = f.get("used_g", "0")
                    try:
                        used_amount = float(used_g)
                        if filament_id:
                            filament_usage.append(
                                {
                                    "slot_id": int(filament_id),
                                    "used_g": used_amount,
                                    "type": f.get("type", ""),
                                    "color": f.get("color", ""),
                                }
                            )
                    except (ValueError, TypeError):
                        pass  # Skip filament entries with unparseable usage values

    except Exception:
        pass  # Return whatever usage data was collected before the error

    return filament_usage


# Header values exposed as `{placeholder}` substitutions inside snippets.
# Aliases let users write Prusa-style names (`{max_layer_z}`) that map onto
# Bambu/Orca header keys (`max_z_height`).
_HEADER_PLACEHOLDER_ALIASES = {
    "max_layer_z": "max_z_height",
    "max_print_height": "max_z_height",
    "total_layers": "total_layer_number",
}

_HEADER_KEY_RE = re.compile(r"^;\s*([^:]+?)\s*:\s*(.+?)\s*$")
# A placeholder is anything inside braces — either a bare header key
# (`{max_layer_z}`) or a small arithmetic expression over header keys
# (`{max_layer_z - 5}`, `{max_z_height / 2}`). The contents are parsed and
# validated below; the regex only delimits the candidate.
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_BARE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_START_GCODE_END_MARKER = "; MACHINE_START_GCODE_END"
_EXECUTABLE_BLOCK_END_MARKER = "; EXECUTABLE_BLOCK_END"

# Arithmetic allowed inside `{...}` expressions. Restricted to the four basic
# operators (plus unary +/-) so snippets can express e.g. a sweep height of
# `max_z_height - 5` or a mid-layer of `max_z_height / 2` without resorting to
# eval() — the expression is walked as an AST and anything outside this set is
# rejected (the placeholder is then left verbatim with a warning).
_ARITH_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_ARITH_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


# Whitelisted functions callable inside `{...}` expressions. `clamp(x, lo, hi)`
# is the safety one: it bounds a computed coordinate to the printer's valid
# range, e.g. `{clamp(max_z_height - 4, 1.5, 176)}` so a short print can't drive
# the bed past the nozzle (and a tall one can't exceed the eject ceiling).
# min/max take two or more args.
_ARITH_FUNCS = {
    "min": min,
    "max": max,
    "clamp": _clamp,
}


def _parse_3mf_gcode_header(content: str) -> dict[str, str]:
    """Parse the `; HEADER_BLOCK_START..END` block into a normalised dict.

    Keys are lowercased, ` [units]` suffixes stripped, and spaces converted
    to underscores so callers can look up `total_layer_number` regardless of
    whether the source line is `; total layer number: 80` or
    `; total filament length [mm] : 12155.34`.
    """
    header: dict[str, str] = {}
    in_header = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line == "; HEADER_BLOCK_START":
            in_header = True
            continue
        if line == "; HEADER_BLOCK_END":
            break
        if not in_header:
            continue
        m = _HEADER_KEY_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        key = re.sub(r"\s*\[[^\]]*\]\s*$", "", key)
        key = key.strip().lower().replace(" ", "_")
        header[key] = value
    return header


def _resolve_header_value(name: str, header: dict[str, str]) -> str | None:
    """Look up a header key, falling back to its alias (e.g. `max_layer_z`)."""
    value = header.get(name)
    if value is None:
        alias = _HEADER_PLACEHOLDER_ALIASES.get(name)
        if alias is not None:
            value = header.get(alias)
    return value


def _format_number(value: float) -> str:
    """Render a computed number for g-code: trim trailing zeros, no exponent.

    `16.0 - 5` → `11`, `16.0 / 2` → `8`, `16.0 / 3` → `5.3333`. Integer-valued
    results drop the decimal point so they read like hand-written coordinates.
    """
    rounded = round(value, 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _eval_arith_node(node: ast.AST, header: dict[str, str]) -> float:
    """Recursively evaluate a whitelisted arithmetic AST node.

    Raises ValueError for anything outside the allowed grammar (unknown
    variable, unsupported operator/function, non-numeric header value).
    """
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH_BINOPS:
        left = _eval_arith_node(node.left, header)
        right = _eval_arith_node(node.right, header)
        return _ARITH_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITH_UNARYOPS:
        return _ARITH_UNARYOPS[type(node.op)](_eval_arith_node(node.operand, header))
    # Numeric literal. Exclude bool (a subclass of int) for tidiness.
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        raw = _resolve_header_value(node.id, header)
        if raw is None:
            raise ValueError(f"unknown variable {node.id!r}")
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"header value for {node.id!r} is not numeric: {raw!r}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ARITH_FUNCS:
        if node.keywords:
            raise ValueError(f"{node.func.id}() takes no keyword arguments")
        args = [_eval_arith_node(a, header) for a in node.args]
        fn = node.func.id
        if fn == "clamp" and len(args) != 3:
            raise ValueError("clamp() takes exactly 3 arguments (x, lo, hi)")
        if fn in ("min", "max") and len(args) < 2:
            raise ValueError(f"{fn}() takes at least 2 arguments")
        return float(_ARITH_FUNCS[fn](*args))
    raise ValueError("unsupported expression")


def _substitute_placeholders(snippet: str, header: dict[str, str]) -> str:
    """Replace `{...}` placeholders with header values, leaving unknowns intact.

    A placeholder is either a bare header key (`{max_layer_z}` → the raw header
    string, preserving its formatting) or a small arithmetic expression over
    header keys (`{max_z_height - 5}`, `{max_z_height / 2}`) evaluated to a
    number. Anything that can't be resolved or evaluated is left verbatim with
    a warning — a typo never silently expands to an empty string.
    """

    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()

        # Bare key: return the raw header string so existing formatting
        # (e.g. "16.00") is preserved exactly.
        if _BARE_IDENT_RE.match(expr):
            value = _resolve_header_value(expr, header)
            if value is None:
                logger.warning(
                    "G-code injection: placeholder {%s} not found in 3MF header; leaving as-is",
                    expr,
                )
                return m.group(0)
            return value

        # Otherwise evaluate as arithmetic over header variables.
        try:
            tree = ast.parse(expr, mode="eval")
            result = _eval_arith_node(tree.body, header)
        except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as e:
            logger.warning(
                "G-code injection: could not evaluate placeholder {%s} (%s); leaving as-is",
                expr,
                e,
            )
            return m.group(0)
        return _format_number(result)

    return _PLACEHOLDER_RE.sub(repl, snippet)


def _inject_start_at_marker(content: str, snippet: str) -> str:
    """Insert snippet immediately before `; MACHINE_START_GCODE_END`.

    The marker sits at the bottom of the printer's startup block — bed heat,
    homing, and nozzle prime are already done, so injected snippets land in
    the same place a slicer-side custom-start-gcode would. Falls back to
    prepending if the marker isn't present (older files / non-Bambu slicers).
    """
    marker_idx = content.find(_START_GCODE_END_MARKER)
    if marker_idx == -1:
        logger.warning(
            "G-code injection: '%s' not found, prepending start snippet to whole file",
            _START_GCODE_END_MARKER,
        )
        return snippet.rstrip("\n") + "\n" + content
    line_start = content.rfind("\n", 0, marker_idx)
    line_start = 0 if line_start == -1 else line_start + 1
    return content[:line_start] + snippet.rstrip("\n") + "\n" + content[line_start:]


def _inject_end_before_marker(content: str, snippet: str) -> str:
    """Insert snippet immediately before `; EXECUTABLE_BLOCK_END`.

    The end snippet must run *inside* the executable block. Bambu firmware
    (verified on a P1S) does not execute G-code that sits after
    `; EXECUTABLE_BLOCK_END`, so appending to the file end silently drops the
    snippet — auto-eject / plate-clear moves never fire. Inserting before the
    marker places the snippet after the printer's own machine-end sequence but
    still within the executed block. Falls back to appending at the file end if
    the marker isn't present.
    """
    marker_idx = content.find(_EXECUTABLE_BLOCK_END_MARKER)
    if marker_idx == -1:
        logger.warning(
            "G-code injection: '%s' not found, appending end snippet to file end",
            _EXECUTABLE_BLOCK_END_MARKER,
        )
        return content.rstrip("\n") + "\n" + snippet.rstrip("\n") + "\n"
    line_start = content.rfind("\n", 0, marker_idx)
    line_start = 0 if line_start == -1 else line_start + 1
    return content[:line_start] + snippet.rstrip("\n") + "\n" + content[line_start:]


def inject_gcode_into_3mf(
    source_path: Path,
    plate_id: int,
    start_gcode: str | None,
    end_gcode: str | None,
):
    """Create a temp copy of a 3MF with G-code injected at start/end.

    Snippets support `{placeholder}` substitution against values parsed from
    the 3MF G-code header block (e.g. `{max_layer_z}` → `16.00`). Start
    snippets are anchored to the `; MACHINE_START_GCODE_END` marker so they
    run after the printer's own startup (#422). End snippets are inserted just
    before `; EXECUTABLE_BLOCK_END` so they run inside the executable block —
    Bambu firmware (P1S) ignores g-code placed after that marker.

    The plate's `.gcode.md5` sidecar is recomputed so firmware that validates
    it against the gcode (e.g. P1S) still accepts the modified file.

    Args:
        source_path: Path to the original 3MF file.
        plate_id: Plate number (1-indexed) to inject into.
        start_gcode: G-code to insert after printer startup, or None.
        end_gcode: G-code to append, or None.

    Returns:
        Path to temp file with injected G-code, or None if injection failed.
        Caller is responsible for cleaning up the temp file.
    """
    import tempfile

    if not start_gcode and not end_gcode:
        return None

    try:
        # Find the target gcode file inside the 3MF
        with zipfile.ZipFile(source_path, "r") as zf:
            all_gcode = [f for f in zf.namelist() if f.endswith(".gcode")]
            if not all_gcode:
                return None

            # Try plate-specific gcode file first
            target_gcode = None
            plate_pattern = f"plate_{plate_id}.gcode"
            for f in all_gcode:
                if f.endswith(plate_pattern):
                    target_gcode = f
                    break

            # Fall back to first gcode file
            if target_gcode is None:
                target_gcode = all_gcode[0]

            # Read and modify gcode content
            gcode_content = zf.read(target_gcode).decode("utf-8", errors="ignore")
            header = _parse_3mf_gcode_header(gcode_content)

            if start_gcode:
                resolved = _substitute_placeholders(start_gcode, header)
                # Log the post-substitution snippet so the actually-injected G-code
                # (placeholders like {max_layer_z} already resolved) is visible at DEBUG.
                logger.debug("G-code injection [%s]: resolved START snippet:\n%s", target_gcode, resolved)
                gcode_content = _inject_start_at_marker(gcode_content, resolved)
            if end_gcode:
                resolved = _substitute_placeholders(end_gcode, header)
                logger.debug("G-code injection [%s]: resolved END snippet:\n%s", target_gcode, resolved)
                gcode_content = _inject_end_before_marker(gcode_content, resolved)

            # The printer validates the plate gcode against an embedded
            # `<plate>.gcode.md5` sidecar (uppercase hex, no trailing newline).
            # Rewriting the gcode without refreshing this hash makes firmware
            # reject the file at load (P1S: HMS 0500-4003 "unable to parse"),
            # so recompute it from the exact bytes we're about to write.
            gcode_bytes = gcode_content.encode("utf-8")
            md5_name = target_gcode + ".md5"
            # Not a security hash — this reproduces Bambu's `.gcode.md5` sidecar
            # format, so flag it as non-security for the linters (ruff S324 / bandit B324).
            md5_value = hashlib.md5(gcode_bytes, usedforsecurity=False).hexdigest().upper().encode("ascii")

            # Write modified 3MF to temp file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as tmp:
                tmp_path = Path(tmp.name)

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_write:
                for item in zf.namelist():
                    info = zf.getinfo(item)
                    if item == target_gcode:
                        zf_write.writestr(info, gcode_bytes)
                    elif item == md5_name:
                        zf_write.writestr(info, md5_value)
                    else:
                        zf_write.writestr(info, zf.read(item))

        return tmp_path

    except Exception:
        # Clean up temp file on error
        if "tmp_path" in locals() and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return None


_PLATE_BLOCK_RE = re.compile(r"<plate>.*?</plate>", re.DOTALL)
_SLICE_INFO_PATH = "Metadata/slice_info.config"


def _plate_index_of_block(block: str) -> int | None:
    """Pull the ``index`` (or ``plater_id``) value out of a single
    ``<plate>...</plate>`` block from ``slice_info.config``."""
    m = re.search(r'<metadata key="(?:index|plater_id)" value="(\d+)"', block)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def extract_single_plate_3mf(source: Path, plate_id: int) -> Path | None:
    """Write a temp 3MF holding only ``plate_id`` out of a multi-plate 3MF.

    Bambu Studio's "Send all" uploads one consolidated project 3MF carrying
    every plate's G-code (often ~56MB). The VP queue dispatches one item per
    plate, each pointing at that same archive — so a naive dispatch uploads
    the full file once per plate. This produces a small single-plate 3MF
    (the size of a normal single-plate send, ~6-23MB) containing just the
    target plate, so each dispatch uploads only what that plate needs.

    Strategy (mirrors :func:`slicer_3mf_convert.merge_plate_3mfs` in reverse):
    - KEEP every shared/base entry (anything that is not a per-plate artifact
      of *another* plate): ``3D/3dmodel.model``, ``[Content_Types].xml``,
      ``_rels/.rels``, ``project_settings.config``, ``model_settings.config``,
      Auxiliaries, cut_information, etc.
    - KEEP ``plate_id``'s own per-plate artifacts
      (:func:`slicer_3mf_convert.per_plate_artifact_names`).
    - DROP the per-plate artifacts of all other plates (the big
      ``Metadata/plate_M.gcode`` for M != plate_id is the main size win).
    - REWRITE ``Metadata/slice_info.config`` to list only ``plate_id``'s
      ``<plate>`` block.
    - The plate is kept at its original index ``plate_id`` (not renumbered to
      1) — working single-plate sends keep their original index, and the
      dispatch still calls ``start_print(plate_id=plate_id)``.

    Returns the temp file path (caller owns cleanup), or ``None`` (logged at
    debug) on any parse error or if the source isn't a multi-plate 3MF, so
    the caller can fall back to uploading the original file.
    """
    from backend.app.services.slicer_3mf_convert import per_plate_artifact_names

    try:
        with zipfile.ZipFile(source, "r") as zf:
            names = zf.namelist()

            # Determine the plate indices the source actually defines from the
            # per-plate gcode entries. Only a genuine multi-plate file is worth
            # splitting; a single-plate (or unparseable) source returns None.
            present_plates: set[int] = set()
            for name in names:
                m = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", name)
                if m:
                    present_plates.add(int(m.group(1)))
            if len(present_plates) < 2:
                logger.debug(
                    "extract_single_plate_3mf: source %s is not multi-plate (plates=%s); skipping",
                    source,
                    sorted(present_plates),
                )
                return None
            if plate_id not in present_plates:
                logger.debug(
                    "extract_single_plate_3mf: plate %s not in source %s (plates=%s); skipping",
                    plate_id,
                    source,
                    sorted(present_plates),
                )
                return None

            # Per-plate artifacts to drop = every other plate's set.
            keep_set = per_plate_artifact_names(plate_id)
            drop_set: set[str] = set()
            for other in present_plates:
                if other == plate_id:
                    continue
                drop_set |= per_plate_artifact_names(other)
            # Never drop something that's also in the target's keep set.
            drop_set -= keep_set

            # Rebuild slice_info.config with only the target plate's block.
            new_slice_info: bytes | None = None
            if _SLICE_INFO_PATH in names:
                try:
                    xml = zf.read(_SLICE_INFO_PATH).decode("utf-8", errors="replace")
                except (OSError, KeyError) as exc:
                    logger.debug("extract_single_plate_3mf: couldn't read slice_info (%s)", exc)
                    return None
                target_block = None
                for block in _PLATE_BLOCK_RE.findall(xml):
                    if _plate_index_of_block(block) == plate_id:
                        target_block = block
                        break
                if target_block is not None:
                    new_slice_info = (
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        "<config>\n"
                        "  <header>\n"
                        '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
                        '    <header_item key="X-BBL-Client-Version" value="02.06.00.51"/>\n'
                        f"  </header>\n  {target_block}\n</config>\n"
                    ).encode()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as tmp:
                tmp_path = Path(tmp.name)

            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as out_zf:
                for info in zf.infolist():
                    name = info.filename
                    if name in drop_set:
                        continue
                    if name == _SLICE_INFO_PATH and new_slice_info is not None:
                        out_zf.writestr(info, new_slice_info)
                    else:
                        out_zf.writestr(info, zf.read(name))

        return tmp_path

    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        logger.debug("extract_single_plate_3mf: failed to extract plate %s from %s (%s)", plate_id, source, exc)
        if "tmp_path" in locals() and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return None


def extract_project_filaments_from_3mf(zf: zipfile.ZipFile) -> list[dict]:
    """Project-wide AMS slot config from ``Metadata/project_settings.config``.

    Returns one dict per configured AMS slot in slot order (1-indexed), with
    ``type`` and ``color`` populated from the project's ``filament_type`` and
    ``filament_colour`` arrays. ``used_grams`` / ``used_meters`` are 0 because
    project_settings carries the configuration, not per-print usage — the
    fields exist for shape compatibility with the slice_info-derived list.

    The SliceModal needs this on **unsliced** project files: slice_info.config
    is empty until Bambu Studio has actually sliced the project, but the user
    can still pick filament profiles for a slice we're about to perform.
    """
    if "Metadata/project_settings.config" not in zf.namelist():
        return []
    try:
        proj = json.loads(zf.read("Metadata/project_settings.config").decode())
    except (ValueError, OSError):
        return []
    if not isinstance(proj, dict):
        return []
    types_arr = proj.get("filament_type") or []
    colors_arr = proj.get("filament_colour") or []
    slot_count = max(
        len(types_arr) if isinstance(types_arr, list) else 0, len(colors_arr) if isinstance(colors_arr, list) else 0
    )
    out: list[dict] = []
    for i in range(slot_count):
        out.append(
            {
                "slot_id": i + 1,
                "type": types_arr[i] if i < len(types_arr) and isinstance(types_arr[i], str) else "",
                "color": colors_arr[i] if i < len(colors_arr) and isinstance(colors_arr[i], str) else "",
                "used_grams": 0,
                "used_meters": 0,
            }
        )
    return out


_PAINT_COLOR_ATTR_RE = re.compile(rb'paint_color="([0-9A-Fa-f]+)"')

# Painted-face quadtree leaves include both real filament assignments and
# tiny edit artifacts (single-leaf accidents from "tried a colour, undid,
# repainted with a different one"). The threshold's only job is dropping
# accidents — anything the user spent meaningful effort on must survive.
# 5% of an object's painted triangles is well below any 60/40 / 70/30 /
# 33/33/33 split a real two- or three-colour print would hit, so all
# intentional colours are kept; one-off single-leaf paints (typically
# 0.1-1.5% in observed projects) are filtered. Note that this fallback
# path runs ONLY when the preview-slice path can't reach the sidecar; in
# the normal flow the slicer's own pruning produces the canonical list and
# this threshold isn't reached.
_PAINT_NOISE_THRESHOLD = 0.05


def extract_plate_extruder_set_from_3mf(zf: zipfile.ZipFile, plate_id: int) -> set[int]:
    """Extruder/AMS slot indices (1-indexed) used by objects on ``plate_id``.

    Three sources are unioned because Bambu Studio splits per-object extruder
    info across THREE places depending on how the user assigned colours:

    1. ``model_settings.config`` — top-level ``<metadata key="extruder">``
       on each ``<object>`` (the "default extruder" for the whole object).
    2. ``model_settings.config`` — per-``<part>`` ``<metadata key="extruder">``
       overrides (used when the user split an object into multiple parts
       with distinct filaments).
    3. ``3D/Objects/object_*.model`` — ``paint_color`` attributes on
       individual ``<triangle>`` elements (used when the user "painted" a
       face with a different filament). The encoding is a hex string where
       each nibble is a TriangleSelector tree node: ``0`` = unpainted leaf,
       ``F`` = branch (4 children follow), ``1``..``E`` = leaf painted with
       extruder N. We don't decode the tree — every leaf-paint nibble in
       the string IS the extruder number, so a flat scan over hex chars
       yields the correct set without recursive parsing.

    Without (3) the painted-face data is invisible: model_settings says
    every object on a multi-color plate uses extruder 1 by default but the
    actual print uses 3, 4, 12 etc. via face paint, so the SliceModal would
    render only one filament dropdown for what's clearly a multi-colour
    print (#1150 follow-up).
    """
    if "Metadata/model_settings.config" not in zf.namelist():
        return set()
    try:
        root = ET.fromstring(zf.read("Metadata/model_settings.config").decode())
    except (ET.ParseError, OSError):
        return set()

    # Pass 1: object → set of extruders from XML metadata (sources 1 + 2)
    # plus the per-object .model file path so we can later scan source 3.
    object_extruders: dict[str, set[int]] = {}
    object_model_paths: dict[str, list[str]] = {}
    for obj_elem in root.findall(".//object"):
        obj_id = obj_elem.get("id")
        if not obj_id:
            continue
        extruders: set[int] = set()
        top = obj_elem.find("metadata[@key='extruder']")
        if top is not None:
            try:
                v = int(top.get("value", "0"))
                if v > 0:
                    extruders.add(v)
            except (ValueError, TypeError):
                pass
        for part_elem in obj_elem.findall(".//part"):
            part_ext = part_elem.find("metadata[@key='extruder']")
            if part_ext is None:
                continue
            try:
                v = int(part_ext.get("value", "0"))
                if v > 0:
                    extruders.add(v)
            except (ValueError, TypeError):
                pass
        object_extruders[obj_id] = extruders

    # Pass 2: 3dmodel.model maps each <object id="N"> to its component
    # .model file path(s). Bambu wraps object IDs that match
    # model_settings.config IDs around <components><component
    # path="/3D/Objects/object_K.model" objectid="..." /></components>.
    # Strip xmlns prefixes on attributes so ElementTree can find them
    # without namespace gymnastics — `p:path` becomes `path` etc.
    if "3D/3dmodel.model" in zf.namelist():
        try:
            raw = zf.read("3D/3dmodel.model").decode()
            stripped = re.sub(r'xmlns:?\w*="[^"]*"', "", raw)
            stripped = re.sub(r"<(/?)\w+:", r"<\1", stripped)
            stripped = re.sub(r" \w+:(\w+=)", r" \1", stripped)
            model_root = ET.fromstring(stripped)
            for obj_elem in model_root.findall(".//object"):
                oid = obj_elem.get("id")
                if not oid:
                    continue
                comps = obj_elem.find("components")
                if comps is None:
                    continue
                paths = []
                for c in comps.findall("component"):
                    p = c.get("path")
                    if p:
                        paths.append(p.lstrip("/"))
                if paths:
                    object_model_paths[oid] = paths
        except (ET.ParseError, OSError):
            pass  # No 3dmodel — paint scan just won't apply

    # Pass 3: scan paint_color attrs in each per-object .model file. Cache
    # by file path because two objects often share the same component tree.
    paint_cache: dict[str, set[int]] = {}

    def _scan_paint(path: str) -> set[int]:
        if path in paint_cache:
            return paint_cache[path]
        out: set[int] = set()
        if path not in zf.namelist():
            paint_cache[path] = out
            return out
        try:
            data = zf.read(path)
        except OSError:
            paint_cache[path] = out
            return out
        # Per-extruder triangle coverage. Each painted triangle may have
        # multiple leaf nibbles (the quadtree subdivides the face into
        # painted regions); we count one triangle per unique extruder per
        # match so the resulting fraction is "what share of painted
        # triangles include at least one leaf with extruder N". Noise from
        # one-off edit artifacts is filtered out at the threshold below.
        extruder_triangles: dict[int, int] = {}
        total_painted = 0
        for match in _PAINT_COLOR_ATTR_RE.finditer(data):
            total_painted += 1
            seen: set[int] = set()
            for ch in match.group(1):
                # Hex digit → 4-bit value. 0 = unpainted leaf, F = branch
                # (decoded recursively but children are encoded inline, so
                # we'll see them on later iterations). 1-E = leaf painted
                # with extruder N.
                if ch in b"123456789":
                    seen.add(ch - 0x30)
                elif ch in b"ABCDEabcde":
                    seen.add((ch & 0x4F) - 0x37)
            for e in seen:
                extruder_triangles[e] = extruder_triangles.get(e, 0) + 1
        if total_painted > 0:
            cutoff = max(1, int(total_painted * _PAINT_NOISE_THRESHOLD))
            for ext, count in extruder_triangles.items():
                if count >= cutoff:
                    out.add(ext)
        paint_cache[path] = out
        return out

    # Walk plates — collect extruders for objects on the requested plate.
    used: set[int] = set()
    for plate_elem in root.findall(".//plate"):
        plater_id = None
        for meta in plate_elem.findall("metadata"):
            if meta.get("key") == "plater_id":
                try:
                    plater_id = int(meta.get("value", ""))
                except (ValueError, TypeError):
                    pass
                break
        if plater_id != plate_id:
            continue
        for inst in plate_elem.findall("model_instance"):
            for inst_meta in inst.findall("metadata"):
                if inst_meta.get("key") != "object_id":
                    continue
                obj_id = inst_meta.get("value")
                if not obj_id:
                    continue
                used.update(object_extruders.get(obj_id, set()))
                for path in object_model_paths.get(obj_id, []):
                    used.update(_scan_paint(path))
        break
    return used
