// Case 5 (the control): an ordinary, fully-spelled instantiation. Grep finds
// this one correctly, and the benchmark says so — a baseline that never wins
// would not be a credible baseline.
module plain_top (
    input logic clk,
    input logic rst
);
  fifo #(
      .DEPTH(8)
  ) u_plain_fifo (
      .clk(clk),
      .rst(rst)
  );
endmodule
