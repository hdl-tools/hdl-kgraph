// #176 parity fixture: instantiates an ambiguously-defined clocked module
// twice on two different clocks. Exercises the collapse predicate on the one
// shape where the NetworkX and SQL alias builders are known to disagree about
// how many formal ports a binding resolves to (see dup_clk_leaf_a.sv).
module dup_clk_top (
    input  logic clk_x,
    input  logic clk_y,
    input  logic d,
    output logic qx,
    output logic qy
);
  dup_clk_leaf u_x (
      .clk(clk_x),
      .d  (d),
      .q  (qx)
  );

  dup_clk_leaf u_y (
      .clk(clk_y),
      .d  (d),
      .q  (qy)
  );
endmodule
