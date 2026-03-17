#!/usr/bin/env python3
"""
sdf_inject.py — Inject SDF delays into a gate-level Verilog netlist

Reads a synthesis-level SDF file and a mapped Verilog netlist, then
produces a new Verilog file where each cell instantiation is replaced
by a uniquified wrapper module that contains a specify block with the
back-annotated IOPATH delays.

The result is a self-contained timed netlist that iverilog can simulate
without $sdf_annotate, specify-block issues, or power-port problems.

Usage:
    python3 sdf_inject.py \\
        --sdf   results/netlist_8.sdf \\
        --netlist results/netlist_8.v \\
        --cells /path/to/sky130_fd_sc_hd.v \\
        --out   results/netlist_8_timed.v

The output file is self-contained — compile with just:
    iverilog -g2012 -g specify \\
        -o sim_exe \\
        sim/tb_gl.v results/netlist_8_timed.v
"""

import re
import sys
import argparse
from collections import defaultdict


# ---------------------------------------------------------------------------
# SDF parser
# ---------------------------------------------------------------------------

def parse_sdf(sdf_path):
    """
    Parse an SDF file and return a dict:
        delays[instance_path][(port_in, port_out)] = (rise_max, fall_max)

    Only IOPATH entries are extracted (cell propagation delays).
    TIMINGCHECK (setup/hold) entries are ignored — they are handled by
    the FF models themselves.

    SDF delay triple format: (min:typ:max) or single value (v)
    We extract the max value, consistent with STA -path_delay max analysis.
    """
    delays = defaultdict(dict)

    with open(sdf_path) as f:
        text = f.read()

    # Remove C-style block comments
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)

    # Tokenise into parenthesised s-expressions
    # We walk character by character building a simple token stream
    tokens = re.findall(r'\(|\)|[^\s()]+', text)

    def parse_value(triple_str):
        """Extract max value from an SDF delay value string.
        Handles: (v), (min:typ:max), (::max), (min::max)
        Returns float delay in ns, or 0.0 if unparseable.
        """
        s = triple_str.strip().strip('()')
        parts = s.split(':')
        if len(parts) == 1:
            try:
                return float(parts[0])
            except ValueError:
                return 0.0
        elif len(parts) == 3:
            # min:typ:max — take max (index 2), fall back to min if max empty
            for idx in [2, 0, 1]:
                try:
                    v = float(parts[idx])
                    return v
                except ValueError:
                    continue
            return 0.0
        return 0.0

    # Simple recursive-descent parser for SDF s-expressions
    pos = [0]

    def peek():
        while pos[0] < len(tokens) and tokens[pos[0]] in ('', ):
            pos[0] += 1
        if pos[0] >= len(tokens):
            return None
        return tokens[pos[0]]

    def consume():
        t = tokens[pos[0]]
        pos[0] += 1
        return t

    def skip_block():
        """Skip a parenthesised block we don't care about."""
        depth = 1
        while depth > 0 and pos[0] < len(tokens):
            t = consume()
            if t == '(':
                depth += 1
            elif t == ')':
                depth -= 1

    # Walk top-level structure looking for CELL blocks
    current_instance = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == '(' and i + 1 < len(tokens):
            keyword = tokens[i + 1].upper()

            if keyword == 'CELL':
                # (CELL (CELLTYPE "...") (INSTANCE path) (DELAY ...) )
                i += 2  # skip '(' 'CELL'
                cell_type = None
                instance_path = None

                # Read sub-blocks until matching ')'
                depth = 1
                while depth > 0 and i < len(tokens):
                    t = tokens[i]
                    if t == '(':
                        sub = tokens[i + 1].upper() if i + 1 < len(tokens) else ''

                        if sub == 'CELLTYPE':
                            # (CELLTYPE "sky130_fd_sc_hd__fa_1")
                            i += 2
                            cell_type = tokens[i].strip('"')
                            i += 1
                            # skip closing )
                            while tokens[i] != ')':
                                i += 1
                            i += 1

                        elif sub == 'INSTANCE':
                            # (INSTANCE dut.U42) or (INSTANCE *)
                            i += 2
                            if tokens[i] != ')':
                                instance_path = tokens[i]
                                i += 1
                            while tokens[i] != ')':
                                i += 1
                            i += 1

                        elif sub == 'DELAY':
                            # (DELAY (ABSOLUTE (IOPATH port1 port2 (rise) (fall)) ...))
                            i += 2
                            delay_depth = 1
                            while delay_depth > 0 and i < len(tokens):
                                dt = tokens[i]
                                if dt == '(':
                                    dsub = tokens[i+1].upper() if i+1 < len(tokens) else ''
                                    if dsub == 'IOPATH':
                                        # (IOPATH port_in port_out (rise_triple) (fall_triple))
                                        i += 2
                                        port_in  = tokens[i]; i += 1
                                        port_out = tokens[i]; i += 1
                                        # rise value
                                        rise_str = tokens[i]; i += 1
                                        # fall value
                                        fall_str = tokens[i]; i += 1
                                        rise = parse_value(rise_str)
                                        fall = parse_value(fall_str)
                                        # skip closing )
                                        while i < len(tokens) and tokens[i] != ')':
                                            i += 1
                                        i += 1  # consume ')'
                                        if instance_path and instance_path != '*':
                                            delays[instance_path][(port_in, port_out)] = (rise, fall)
                                    else:
                                        i += 1
                                        delay_depth += 1
                                elif dt == ')':
                                    delay_depth -= 1
                                    i += 1
                                else:
                                    i += 1

                        elif sub == 'TIMINGCHECK':
                            # Skip setup/hold checks — not needed for propagation sim
                            i += 1
                            tc_depth = 1
                            while tc_depth > 0 and i < len(tokens):
                                if tokens[i] == '(':
                                    tc_depth += 1
                                elif tokens[i] == ')':
                                    tc_depth -= 1
                                i += 1
                        else:
                            i += 1
                            depth += 1
                    elif t == ')':
                        depth -= 1
                        i += 1
                    else:
                        i += 1
            else:
                i += 1
        else:
            i += 1

    return delays


# ---------------------------------------------------------------------------
# Verilog netlist parser  (structural, not a full parser)
# ---------------------------------------------------------------------------

# Matches a cell instantiation:
#   sky130_fd_sc_hd__fa_1 U42 ( .A(net1), .B(net2), .X(net3) );
INST_RE = re.compile(
    r'^\s*(sky130_\w+)\s+(\w+)\s*\(([^;]+)\)\s*;',
    re.MULTILINE | re.DOTALL
)

def parse_netlist_instances(netlist_path):
    """
    Return list of (cell_type, instance_name, port_str, full_match_text)
    for every sky130 cell instantiation in the netlist.
    """
    with open(netlist_path) as f:
        text = f.read()
    instances = []
    for m in INST_RE.finditer(text):
        instances.append((m.group(1), m.group(2), m.group(3), m.group(0)))
    return instances, text


def parse_cell_ports(cell_verilog_path, cell_types):
    """
    For each cell type in cell_types, extract the ordered port list
    from the cell library Verilog.  Returns dict {cell_type: [ports]}.
    We need port directions to build the specify block correctly.
    """
    with open(cell_verilog_path) as f:
        text = f.read()

    port_info = {}  # {cell_type: {'inputs': [...], 'outputs': [...]}}

    for cell in cell_types:
        # Find the module definition for this cell
        pat = re.compile(
            r'module\s+' + re.escape(cell) + r'\s*\(([^)]+)\)(.*?)endmodule',
            re.DOTALL
        )
        m = pat.search(text)
        if not m:
            continue

        port_list_str = m.group(1)
        body = m.group(2)

        # Extract input/output declarations from body
        inputs  = re.findall(r'\binput\b\s+(?:wire\s+)?(?:\[\d+:\d+\]\s+)?(\w+)', body)
        outputs = re.findall(r'\boutput\b\s+(?:wire\s+)?(?:\[\d+:\d+\]\s+)?(\w+)', body)

        # Filter out power ports — we don't want those in wrappers
        power_ports = {'VPWR', 'VGND', 'VPB', 'VNB', 'VDD', 'VSS'}
        inputs  = [p for p in inputs  if p not in power_ports]
        outputs = [p for p in outputs if p not in power_ports]

        port_info[cell] = {'inputs': inputs, 'outputs': outputs}

    return port_info


# ---------------------------------------------------------------------------
# Wrapper module generator
# ---------------------------------------------------------------------------

def make_wrapper(cell_type, instance_name, port_conn_str,
                 cell_delays, port_info):
    """
    Generate a uniquified wrapper module for one cell instance.

    The wrapper:
      - Has the same non-power ports as the original cell
      - Instantiates the original cell (without power ports)
      - Contains a specify block with the IOPATH delays from the SDF
      - Is named  <cell_type>__<instance_name>  to avoid conflicts

    Returns (wrapper_module_text, new_instantiation_text)
    """
    wrapper_name = f"{cell_type}__{instance_name}"

    info = port_info.get(cell_type, {'inputs': [], 'outputs': []})
    all_ports = info['inputs'] + info['outputs']

    if not all_ports:
        # Can't build wrapper without port info — return original unchanged
        return None, None

    # ---- Port declarations ----
    port_decls = []
    for p in info['inputs']:
        port_decls.append(f"    input  wire {p};")
    for p in info['outputs']:
        port_decls.append(f"    output wire {p};")

    # ---- Inner cell instantiation (no power ports) ----
    conn_parts = [f".{p}({p})" for p in all_ports]
    inner_inst = (
        f"    {cell_type} _inner_ (\n"
        f"        " + ",\n        ".join(conn_parts) + "\n"
        f"    );"
    )

    # ---- Specify block ----
    specify_lines = []
    for (port_in, port_out), (rise, fall) in cell_delays.items():
        # SDF port names sometimes have bit indices like A[0] — strip them
        pin_in  = re.sub(r'\[\d+\]', '', port_in)
        pin_out = re.sub(r'\[\d+\]', '', port_out)
        # Only emit if both ports exist in this cell
        if pin_in in all_ports and pin_out in all_ports:
            # Convert ns to ps for specify (Verilog specify uses same timescale
            # as the module — our timescale is 1ns/1ps so values stay in ns)
            specify_lines.append(
                f"        (posedge {pin_in} => {pin_out}) = ({rise:.3f}, {fall:.3f});"
            )
            specify_lines.append(
                f"        (negedge {pin_in} => {pin_out}) = ({rise:.3f}, {fall:.3f});"
            )

    if specify_lines:
        specify_block = (
            "    specify\n" +
            "\n".join(specify_lines) + "\n"
            "    endspecify\n"
        )
    else:
        specify_block = "    // no IOPATH delays found for this instance\n"

    # ---- Wrapper module ----
    port_list = ", ".join(all_ports)
    wrapper = (
        f"// Timed wrapper for instance {instance_name} ({cell_type})\n"
        f"module {wrapper_name} ({port_list});\n"
        + "\n".join(port_decls) + "\n"
        "\n"
        + inner_inst + "\n"
        "\n"
        + specify_block +
        f"endmodule\n"
    )

    # ---- New instantiation using wrapper ----
    # Parse port connections from original instantiation
    # port_conn_str is the content between the outer parens
    new_inst = f"    {wrapper_name} {instance_name} ({port_conn_str.strip()});"

    return wrapper, new_inst


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sdf',     required=True, help='Input SDF file')
    ap.add_argument('--netlist', required=True, help='Input gate-level Verilog netlist')
    ap.add_argument('--cells',   required=True, help='sky130 cell library Verilog')
    ap.add_argument('--out',     required=True, help='Output timed Verilog netlist')
    ap.add_argument('--timescale', default='1ns/1ps',
                    help='Verilog timescale (default: 1ns/1ps)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    # ---- 1. Parse SDF ----
    print(f"Parsing SDF: {args.sdf}")
    delays = parse_sdf(args.sdf)
    print(f"  Found delays for {len(delays)} instances")
    if args.verbose:
        for inst, paths in list(delays.items())[:5]:
            print(f"    {inst}: {list(paths.items())[:2]}")

    # ---- 2. Parse netlist ----
    print(f"Parsing netlist: {args.netlist}")
    instances, netlist_text = parse_netlist_instances(args.netlist)
    print(f"  Found {len(instances)} cell instances")

    cell_types_used = set(cell for cell, _, _, _ in instances)
    print(f"  Cell types: {sorted(cell_types_used)}")

    # ---- 3. Parse cell port info from library ----
    print(f"Parsing cell ports from: {args.cells}")
    port_info = parse_cell_ports(args.cells, cell_types_used)
    print(f"  Got port info for {len(port_info)}/{len(cell_types_used)} cell types")

    # ---- 4. Build output ----
    print(f"Generating timed netlist: {args.out}")

    wrappers = []
    patched_netlist = netlist_text

    matched = 0
    unmatched = 0

    for cell_type, inst_name, port_str, orig_text in instances:
        # SDF instance paths — OpenSTA writes them relative to the top module,
        # e.g. "U42" or "_U42_".  Try a few variants.
        candidates = [
            inst_name,
            f"_{inst_name}_",
            inst_name.lstrip('_').rstrip('_'),
        ]
        inst_delays = {}
        for c in candidates:
            if c in delays:
                inst_delays = delays[c]
                break

        if not inst_delays and args.verbose:
            print(f"  WARNING: no SDF delays for instance {inst_name} ({cell_type})")
            unmatched += 1
        else:
            matched += 1

        wrapper, new_inst = make_wrapper(
            cell_type, inst_name, port_str, inst_delays, port_info
        )

        if wrapper is None:
            if args.verbose:
                print(f"  WARNING: skipping {inst_name} — no port info for {cell_type}")
            continue

        wrappers.append(wrapper)
        patched_netlist = patched_netlist.replace(orig_text.strip(), new_inst, 1)

    print(f"  Matched SDF delays: {matched}/{matched+unmatched} instances")

    # ---- 5. Write output ----
    with open(args.out, 'w') as f:
        f.write(f"`timescale {args.timescale}\n\n")
        f.write("// -------------------------------------------------------\n")
        f.write("// Timed cell wrappers (generated by sdf_inject.py)\n")
        f.write("// Each wrapper contains a specify block with IOPATH delays\n")
        f.write("// back-annotated from the SDF file.\n")
        f.write("// -------------------------------------------------------\n\n")
        for w in wrappers:
            f.write(w)
            f.write("\n")
        f.write("// -------------------------------------------------------\n")
        f.write("// Patched netlist\n")
        f.write("// -------------------------------------------------------\n\n")
        f.write(patched_netlist)

    print(f"Done. Written to {args.out}")
    print(f"Compile with:")
    print(f"  iverilog -g2012 -g specify -o sim_exe tb_gl.v {args.out}")


if __name__ == '__main__':
    main()
