// #176 for resets: reset nets alias through the same shared formal ports as
// clocks, so a module instantiated twice with the reset actuals swapped
// over-merges its reset groups the same way. Less harmful than the CDC case
// (an over-merged group is visibly wrong, whereas a zero crossing count reads
// as a clean design) but the two reports must not disagree about whether the
// aliasing can be trusted.
module reset_leaf (
    input  logic clk_i,
    input  logic a_rst_n_i,
    input  logic b_rst_n_i,
    input  logic d_i,
    output logic q_o
);
  logic a_q;
  always_ff @(posedge clk_i or negedge a_rst_n_i) begin
    if (!a_rst_n_i) a_q <= 1'b0;
    else            a_q <= d_i;
  end
  always_ff @(posedge clk_i or negedge b_rst_n_i) begin
    if (!b_rst_n_i) q_o <= 1'b0;
    else            q_o <= a_q;
  end
endmodule

module reset_top (
    input  logic clk_i,
    input  logic por_rst_n,
    input  logic soft_rst_n,
    input  logic d_i,
    output logic q_o
);
  logic mid;

  reset_leaf u_fwd (
      .clk_i    (clk_i),
      .a_rst_n_i(por_rst_n),
      .b_rst_n_i(soft_rst_n),
      .d_i      (d_i),
      .q_o      (mid)
  );

  // Reset actuals swapped: this is the collapse.
  reset_leaf u_rev (
      .clk_i    (clk_i),
      .a_rst_n_i(soft_rst_n),
      .b_rst_n_i(por_rst_n),
      .d_i      (mid),
      .q_o      (q_o)
  );
endmodule
