// Case 3: a SystemVerilog module instantiating the VHDL entity `alu`.
// Asked about "ALU" — the spelling a datasheet or a reviewer would use — a
// case-sensitive search finds nothing, because the VHDL source spells it
// lowercase. The graph matches VHDL names case-insensitively.
module vhdl_user (
    input  logic [7:0] a,
    input  logic [7:0] b,
    output logic [7:0] result
);
  alu #(
      .WIDTH(8)
  ) u_alu (
      .a     (a),
      .b     (b),
      .result(result)
  );
endmodule
