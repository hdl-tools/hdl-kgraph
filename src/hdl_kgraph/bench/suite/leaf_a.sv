// Case 4 support: one of two same-named definitions of `dual_leaf`.
module dual_leaf (
    input  logic d,
    output logic q
);
  assign q = d;
endmodule
