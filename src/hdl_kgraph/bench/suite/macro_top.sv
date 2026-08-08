`include "defs.svh"

// Case 1: `macro_top` instantiates `fifo`, but a search for the word "fifo"
// never matches here — the only text present is `MAKE_FIFO`, and `_` is a
// word character, so even a case-insensitive \bfifo\b does not match it.
module macro_top (
    input logic clk,
    input logic rst
);
  `MAKE_FIFO(u_fifo);
endmodule
