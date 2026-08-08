// M8 cocotb fixture: the DUT exercised by test_counter.py.
module counter #(
    parameter int WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             enable,
    input  logic [WIDTH-1:0] data,
    output logic [WIDTH-1:0] count,
    output logic             overflow
);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= '0;
        else if (enable)
            count <= count + 1'b1;
    end

    assign overflow = &count;
endmodule
