// M10 fixture: a process whose clock evidence is only the 0.4 name/ambiguity
// heuristic (two edge candidates -> ambiguous_sensitivity), so an SDC
// create_clock on `gclk` is what upgrades its CLOCKED_BY edge to 1.0.
module gated (
    input  logic gclk,
    input  logic altclk,
    input  logic d,
    output logic q
);
  always @(posedge gclk or posedge altclk) q <= d;
endmodule
