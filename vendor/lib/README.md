# vendor/lib

`libprism_core.dll` is the Prism Engine, built from upstream prism-core and committed here on
purpose. It is the `acbfd50` pin (CLAUDE.md hard rule 1) as a file rather than a note, it
survives a formatted machine, and a booth laptop can clone and run without a compiler. The engine
source is never committed: `vendor/prism-core/` is a local clone, and it is gitignored.

| | |
|---|---|
| Upstream | https://github.com/RidhwanAhamed/prism-core |
| Commit | `acbfd50f6ae140a27e36b07c749737370e7cbcb2` (2 Aug 2026, "feat(bindings): expose the scene crossfade to Dart") |
| `prism_version()` | `0.3.0` |
| File | `libprism_core.dll`, 1,721,856 bytes, Windows x64 (PE32+) |
| SHA-256 | `36715de00941d75b47ea2cfcbf21daba8fc1217ea60a5e35db9834c87e62751b` |
| Exports | The 20 `PRISM_API` functions of `include/prism/prism_core.h`, and nothing else |
| Imports | `KERNEL32.dll` and the Windows Universal C Runtime (`api-ms-win-crt-*`) only. No MinGW runtime DLLs: the C++ runtime is linked in |

## Toolchain

MSYS2 UCRT64 (`D:\ANP\tools\msys64\ucrt64` on the machine it was built on):

- GCC 16.1.0 (Rev5, Built by MSYS2 project)
- CMake 4.4.0
- Ninja 1.13.2
- GNU Binutils 2.46.1

No Visual Studio. The Windows portability fixes it needs are upstream, in commit `686c45c`.

## Build flags

```
-G Ninja
-DCMAKE_BUILD_TYPE=RelWithDebInfo
-DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
-DPRISM_BUILD_SHARED=ON
-DPRISM_BUILD_TESTS=OFF -DPRISM_BUILD_HARNESS=OFF -DPRISM_BUILD_TOOLS=OFF
-DCMAKE_SHARED_LINKER_FLAGS="-static -s -Wl,--no-insert-timestamp"
```

Target `prism_core_shared`.

- RelWithDebInfo is the engine's own `release` preset (`-O2 -g -DNDEBUG`).
- `-static` links libstdc++, libgcc and winpthread into the DLL.
- `-s` drops the debug info and symbol table at link time. The export table stays.
- `--no-insert-timestamp` leaves the PE timestamp at 0, so the build is reproducible.
- Tests are off because they download GoogleTest at configure time. The harness and tools are not needed.

## Rebuild

From the prism-live root, in a shell with the UCRT64 `bin` directory on `PATH`:

```
git clone --no-checkout https://github.com/RidhwanAhamed/prism-core.git vendor/prism-core
git -C vendor/prism-core checkout acbfd50
cmake -S vendor/prism-core -B vendor/prism-core/build/win-x64-shared -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ -DPRISM_BUILD_SHARED=ON -DPRISM_BUILD_TESTS=OFF -DPRISM_BUILD_HARNESS=OFF -DPRISM_BUILD_TOOLS=OFF "-DCMAKE_SHARED_LINKER_FLAGS=-static -s -Wl,--no-insert-timestamp"
cmake --build vendor/prism-core/build/win-x64-shared --target prism_core_shared
cp vendor/prism-core/build/win-x64-shared/core/libprism_core.dll vendor/lib/
sha256sum vendor/lib/libprism_core.dll
```

With the same toolchain the SHA-256 matches the one above byte for byte: two build directories gave identical files. A different compiler or binutils version gives a different hash, and that is a different library until it is checked again.

To debug a crash inside the engine, rebuild without `-s` and load that DLL instead. Never commit a debug build here.

Never commit to prism-core, and never patch `vendor/prism-core` to make this build. If the engine seems to need a change, stop and say so (hard rule 1).
