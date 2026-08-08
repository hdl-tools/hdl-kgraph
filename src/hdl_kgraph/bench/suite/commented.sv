// Case 2: this module instantiates NOTHING. It only names `sram_ctrl` in a
// comment and in a string literal — the classic grep false positive.
module commented (
    input logic clk,
    input logic rst
);
  // TODO: hook this shift register up to sram_ctrl once the depth is settled.
  logic [3:0] shift_q;

  initial $display("commented: sram_ctrl is not wired yet");

  always_ff @(posedge clk) begin
    if (rst) shift_q <= '0;
    else shift_q <= {shift_q[2:0], 1'b1};
  end
endmodule
