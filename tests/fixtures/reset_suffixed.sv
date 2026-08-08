// Resets whose names carry a port suffix or an infix qualifier. An earlier
// `$`-anchored reset pattern matched none of these, so every `posedge clk or
// negedge <reset>` block below fell through to the ambiguous-sensitivity
// branch and emitted the RESET as a low-confidence CLOCKED_BY — turning reset
// nets into clock domains and blinding CDC detection.
module reset_suffixed (
    input  logic wr_clk_i,
    input  logic wr_rst_n_i,
    input  logic rd_clk_i,
    input  logic m_rst_sync_n,
    input  logic d_i,
    output logic q_o
);

  logic stage_q;

  // Port-suffixed reset: `wr_rst_n_i`.
  always_ff @(posedge wr_clk_i or negedge wr_rst_n_i) begin
    if (!wr_rst_n_i) stage_q <= 1'b0;
    else stage_q <= d_i;
  end

  // Infix-qualified reset: `m_rst_sync_n`.
  always_ff @(posedge rd_clk_i or negedge m_rst_sync_n) begin
    if (!m_rst_sync_n) q_o <= 1'b0;
    else q_o <= stage_q;
  end

endmodule
