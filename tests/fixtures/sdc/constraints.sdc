# M10 fixture: SDC constraints targeting the top.v / simple_counter.sv pair.
create_clock -name sys_clk -period 10.000 [get_ports clk]
create_generated_clock -name div_clk -source [get_ports clk] -divide_by 2 \
    [get_pins u_counter/count[0]]
set_clock_groups -asynchronous -group {sys_clk} -group {div_clk}
set_false_path -from [get_ports rst_n]
set_multicycle_path 2 -from [get_clocks sys_clk] -to [get_ports value*]
set_input_delay 2.0 -clock sys_clk [get_ports rst_n]
set_output_delay 1.5 -clock sys_clk [get_ports value*]
