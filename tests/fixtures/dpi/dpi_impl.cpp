// M8 DPI-C boundary fixture: a C++ extern "C" implementation. DPI uses C
// linkage, so the unmangled name `cpp_fn` is what the linker matches.
extern "C" int cpp_fn(int a) {
    return a - 1;
}

namespace detail {
// A namespaced helper; recorded under its bare name `internal`.
int internal(int a) { return a; }
}  // namespace detail
