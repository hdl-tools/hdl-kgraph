// #176 parity fixture: first of two same-named CLOCKED modules. Pairs with
// dup_clk_leaf_b.sv and dup_clk_top.sv — an instance with two candidate
// definitions, whose formal clock port is bound to two different actuals.
// The oracle's ``_formal_port`` returns the first matching definition's port
// while the SQL join returns both, so this pins that the collapse verdict
// stays identical across the two paths.
module dup_clk_leaf (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(posedge clk) q <= d;
endmodule
