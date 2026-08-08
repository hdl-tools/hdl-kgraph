# M10 fixture: create_clock makes gclk authoritative (CLOCKED_BY 0.4 -> 1.0).
create_clock -name gclk -period 5.0 [get_ports gclk]
