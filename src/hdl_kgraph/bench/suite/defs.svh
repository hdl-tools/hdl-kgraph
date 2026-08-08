// Case 1 support: the instantiation is hidden behind a macro, so the
// instantiated module's name never appears at the call site.
`define MAKE_FIFO(inst_name) fifo #(.DEPTH(4)) inst_name (.clk(clk), .rst(rst))
