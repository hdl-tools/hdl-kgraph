// #176 negative guard: a leaf instantiated under two different parents binds
// its one formal clock port to two DISTINCT actual nodes (pa.clk_i and
// pb.clk_i), so a naive "formal bound to >1 actual" test would flag it. But
// both parents are fed from the same grandparent clock through single-actual
// port bindings, which corroborate the merge — these really are one net, and
// the analysis must stay silent (cdc_analysis: "complete").
module leaf_ff (
    input  logic clk,
    input  logic d,
    output logic q
);
  always_ff @(posedge clk) q <= d;
endmodule

module parent_a (
    input  logic clk_i,
    input  logic d_i,
    output logic q_o
);
  leaf_ff u_ff (
      .clk(clk_i),
      .d  (d_i),
      .q  (q_o)
  );
endmodule

module parent_b (
    input  logic clk_i,
    input  logic d_i,
    output logic q_o
);
  leaf_ff u_ff (
      .clk(clk_i),
      .d  (d_i),
      .q  (q_o)
  );
endmodule

module shared_leaf_top (
    input  logic clk,
    input  logic d,
    output logic qa,
    output logic qb
);
  parent_a u_a (
      .clk_i(clk),
      .d_i  (d),
      .q_o  (qa)
  );

  parent_b u_b (
      .clk_i(clk),
      .d_i  (d),
      .q_o  (qb)
  );
endmodule
