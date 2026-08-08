-- Case 3 support: a VHDL entity. VHDL identifiers are case-insensitive, so
-- `ALU`, `Alu`, and `alu` all name this entity.
library ieee;
use ieee.std_logic_1164.all;

entity alu is
  generic (
    WIDTH : integer := 8
  );
  port (
    a      : in  std_logic_vector(7 downto 0);
    b      : in  std_logic_vector(7 downto 0);
    result : out std_logic_vector(7 downto 0)
  );
end entity alu;

architecture rtl of alu is
begin
  result <= a;
end architecture rtl;
