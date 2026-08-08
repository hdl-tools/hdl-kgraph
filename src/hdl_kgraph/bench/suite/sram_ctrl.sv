// Case 2 support: a real module that nothing in this suite instantiates.
// `commented` names it in a comment and a string, and that is the only place
// its name appears outside this file.
module sram_ctrl (
    input logic clk,
    input logic rst
);
endmodule
