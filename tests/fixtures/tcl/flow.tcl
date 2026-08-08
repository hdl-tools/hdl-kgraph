# M10 fixture: tool-flow script with file references, a variable, and a source chain.
set RTL_DIR .

read_verilog $RTL_DIR/simple_counter.sv
read_verilog $RTL_DIR/top.v
read_sdc constraints.sdc

# helper.tcl intentionally does not exist: exercises the unresolved-stub convention.
source helper.tcl
