// #176 parity fixture: second of two same-named CLOCKED modules. See
// dup_clk_leaf_a.sv for what this pair pins.
module dup_clk_leaf (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(negedge clk) q <= ~d;
endmodule
