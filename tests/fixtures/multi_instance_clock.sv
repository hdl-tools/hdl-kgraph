// #176 fixture: one dual-clock module instantiated twice with the clock
// actuals SWAPPED. Name-level aliasing gives cdc_gray_fifo one node per formal
// port, shared by both instances, so wr_clk_i unions with both s_clk_i and
// m_clk_i — merging two asynchronous clocks into a single domain. Nothing then
// "crosses" between them and CDC reports zero suspects.
//
// The two clock formals and the two clock actuals form a 4-cycle, so neither
// formal is a cut vertex: an articulation-point test does NOT detect this.
// Recovering the domains needs elaboration; this fixture pins that the
// collapse is at least *reported* (cdc_analysis: "degraded").
module cdc_gray_fifo (
    input  logic wr_clk_i,
    input  logic rd_clk_i,
    input  logic d_i,
    output logic q_o
);
  logic wr_q;
  always_ff @(posedge wr_clk_i) wr_q <= d_i;
  always_ff @(posedge rd_clk_i) q_o  <= wr_q;
endmodule

module async_axi_fifo (
    input  logic s_clk_i,
    input  logic m_clk_i,
    input  logic s_d_i,
    output logic m_q_o
);
  logic a2b;
  logic b2a;

  // Forward: written on s_clk_i, read on m_clk_i.
  cdc_gray_fifo u_fwd (
      .wr_clk_i(s_clk_i),
      .rd_clk_i(m_clk_i),
      .d_i     (s_d_i),
      .q_o     (a2b)
  );

  // Reverse: the same module, clock actuals swapped. This is the collapse.
  cdc_gray_fifo u_rev (
      .wr_clk_i(m_clk_i),
      .rd_clk_i(s_clk_i),
      .d_i     (a2b),
      .q_o     (b2a)
  );

  assign m_q_o = b2a;
endmodule
