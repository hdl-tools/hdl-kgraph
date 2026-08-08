// M8 DPI-C boundary fixture: SV import/export "DPI-C" declarations whose
// foreign targets live in dpi_impl.c / dpi_impl.cpp.
module dpi_top;
  // Plain import: linkage name == SV name (defined in dpi_impl.c).
  import "DPI-C" function int my_add(input int a, input int b);

  // Aliased import: C linkage name c_mult, SV-visible name sv_mult.
  import "DPI-C" c_mult = function int sv_mult(input int a, input int b);

  // Imported task (defined as a C void function in dpi_impl.c).
  import "DPI-C" context task my_task(input int x);

  // Import resolved into a C++ extern "C" definition (dpi_impl.cpp).
  import "DPI-C" function int cpp_fn(input int a);

  // Import with no C definition anywhere: must degrade to an unresolved stub.
  import "DPI-C" function int missing_c(input int a);

  // Export: makes this SV function callable from C.
  export "DPI-C" function sv_export;
  function int sv_export(input int a);
    return a + 1;
  endfunction

  int result;
  initial result = my_add(1, 2);
endmodule
