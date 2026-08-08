// Case 4: instantiates `dual_leaf`, which has two candidate definitions.
module dup_top (
    input  logic d,
    output logic q
);
  dual_leaf u_leaf (
      .d(d),
      .q(q)
  );
endmodule
