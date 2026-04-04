# Building Piglet on Windows

## Prerequisites

### 1. Install Zig 0.13.0

Download from https://ziglang.org/download/ — grab the **zig-windows-x86_64-0.13.0.zip**.

Extract to `C:\zig` (or anywhere), then add to PATH:

```powershell
# Add to PATH for current session
$env:PATH = "C:\zig;$env:PATH"

# Verify
zig version
# Should print: 0.13.0
```

### 2. Install MSYS2 (provides FFmpeg and all C dependencies)

Download and install from https://www.msys2.org/

Then open **MSYS2 MINGW64** terminal (NOT the MSYS2 MSYS one) and run:

```bash
pacman -Syu
pacman -S mingw-w64-x86_64-ffmpeg mingw-w64-x86_64-x264 mingw-w64-x86_64-zlib mingw-w64-x86_64-openssl
```

This installs all the FFmpeg static libraries, headers, and dependencies to `C:\msys64\mingw64\`.

### 3. Set up vendor directory

From PowerShell, copy the FFmpeg headers and libs into the project:

```powershell
cd path\to\desktop-copilot\Client

# Create vendor directories
New-Item -ItemType Directory -Force -Path vendor\ffmpeg\include
New-Item -ItemType Directory -Force -Path vendor\ffmpeg\lib
New-Item -ItemType Directory -Force -Path vendor\lib

# Copy FFmpeg headers
Copy-Item -Recurse C:\msys64\mingw64\include\libavcodec   vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libavformat   vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libavutil     vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libavdevice   vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libavfilter   vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libswscale    vendor\ffmpeg\include\
Copy-Item -Recurse C:\msys64\mingw64\include\libswresample vendor\ffmpeg\include\

# Copy FFmpeg static libs
Copy-Item C:\msys64\mingw64\lib\libavcodec.a     vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libavformat.a    vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libavutil.a      vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libavdevice.a    vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libavfilter.a    vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libswscale.a     vendor\ffmpeg\lib\
Copy-Item C:\msys64\mingw64\lib\libswresample.a  vendor\ffmpeg\lib\

# Copy dependency libs
Copy-Item C:\msys64\mingw64\lib\libx264.a        vendor\lib\
Copy-Item C:\msys64\mingw64\lib\libz.a           vendor\lib\
Copy-Item C:\msys64\mingw64\lib\libssl.a         vendor\lib\
Copy-Item C:\msys64\mingw64\lib\libcrypto.a      vendor\lib\
Copy-Item C:\msys64\mingw64\lib\libwinpthread.a  vendor\lib\
```

## Build

```powershell
cd path\to\desktop-copilot\Client
zig build
```

Output: `zig-out\bin\piglet.exe`

## Deploy

```powershell
# Stop the running piglet (Ctrl+C or close the terminal)

# Copy the new binary
Copy-Item zig-out\bin\piglet.exe $env:USERPROFILE\.piglet\piglet.exe -Force

# Restart
& $env:USERPROFILE\.piglet\piglet.exe join --host piglet.genesisailab.com --secret <your-bridge-secret>
```

## Troubleshooting

**"library not found" errors**: Make sure all `.a` files are in the right vendor paths. Run `dir vendor\ffmpeg\lib\` and `dir vendor\lib\` to verify.

**MSYS2 packages missing**: The static `.a` files may be in a separate `-static` package variant. Try:
```bash
# In MSYS2 MINGW64 terminal
pacman -S mingw-w64-x86_64-ffmpeg  # Shared + import libs
# If .a files are missing, FFmpeg may need to be built from source.
# The shared .dll.a import libs may work — try the build first.
```

**Zig version mismatch**: This project requires exactly Zig 0.13.0. Newer versions will NOT work due to breaking changes in the build system.
