# M10 acceptance fixture: SDC for two_clock_cdc.sv.
# create_clock is authoritative evidence (upgrades CLOCKED_BY 0.4 -> 1.0);
# set_clock_groups -asynchronous declares the clk_a -> clk_b crossing safe.
create_clock -name clk_a -period 10.000 [get_ports clk_a]
create_clock -name clk_b -period 7.000 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
