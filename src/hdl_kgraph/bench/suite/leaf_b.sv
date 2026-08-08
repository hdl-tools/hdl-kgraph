// Case 4 support: the other definition of `dual_leaf`. Which one a tool
// actually compiles depends on the filelist and library order — neither the
// graph nor grep can know from the source alone, so the graph reports the
// match as ambiguous (confidence 0.6) rather than guessing.
module dual_leaf (
    input  logic d,
    output logic q
);
  assign q = ~d;
endmodule
