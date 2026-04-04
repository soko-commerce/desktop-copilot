const std = @import("std");

// Zig 0.13.0 required.
// Windows native build requires MSYS2 MinGW64 with ffmpeg and x264 packages:
//   pacman -S mingw-w64-x86_64-ffmpeg mingw-w64-x86_64-x264
// After build, copy DLLs from C:\msys64\mingw64\bin\ next to piglet.exe

pub fn build(b: *std.Build) void {
    const version = "0.0.7";

    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const build_options = b.addOptions();
    build_options.addOption([]const u8, "version", version);

    const exe = b.addExecutable(.{
        .name = "piglet",
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
    });

    const zigcli_dep = b.dependency("zig-cli", .{ .target = target });
    exe.root_module.addImport("zig-cli", zigcli_dep.module("zig-cli"));

    const httpz = b.dependency("httpz", .{
        .target = target,
        .optimize = optimize,
    });
    exe.root_module.addImport("httpz", httpz.module("httpz"));

    const websocket = b.dependency("websocket", .{
        .target = target,
        .optimize = optimize,
    });
    exe.root_module.addImport("websocket", websocket.module("websocket"));

    exe.root_module.addOptions("build_config", build_options);

    // FFmpeg headers (tracked in vendor/)
    exe.addSystemIncludePath(b.path("vendor/ffmpeg/include"));
    exe.addSystemIncludePath(b.path("vendor/ffmpeg/include/ffmpeg"));

    const host_os = @import("builtin").os.tag;
    const target_info = target.result;

    if (host_os == .windows) {
        // Native Windows build — MSYS2 MinGW64
        exe.addSystemIncludePath(.{ .cwd_relative = "C:/msys64/mingw64/include" });
        exe.addLibraryPath(.{ .cwd_relative = "C:/msys64/mingw64/lib" });
    } else {
        // Cross-compile from macOS/Linux
        exe.addSystemIncludePath(std.Build.LazyPath{ .cwd_relative = "/opt/homebrew/Cellar/mingw-w64/12.0.0_2/toolchain-x86_64/x86_64-w64-mingw32/include" });
    }

    exe.linkLibC();

    if (target_info.os.tag == .windows) {
        if (host_os != .windows) {
            // Cross-compile library paths (macOS/Linux → Windows)
            exe.addLibraryPath(.{ .cwd_relative = "/usr/local/x86_64-w64-mingw32/lib" });
            exe.addLibraryPath(.{ .cwd_relative = "/usr/local/x86_64-w64-mingw32/lib64" });
            exe.addLibraryPath(.{ .cwd_relative = "/opt/homebrew/Cellar/mingw-w64/12.0.0_2/toolchain-x86_64/x86_64-w64-mingw32/lib" });
        }

        // Windows system libraries
        exe.linkSystemLibrary("kernel32");
        exe.linkSystemLibrary("bcrypt");
        exe.linkSystemLibrary("shell32");
        exe.linkSystemLibrary("shlwapi");
        exe.linkSystemLibrary("ole32");
        exe.linkSystemLibrary("oleaut32");
        exe.linkSystemLibrary("winmm");
        exe.linkSystemLibrary("ntdll");
        exe.linkSystemLibrary("gdi32");
        exe.linkSystemLibrary("vfw32");
        exe.linkSystemLibrary("ws2_32");
        exe.linkSystemLibrary("secur32");
        exe.linkSystemLibrary("crypt32");

        // Define Windows threading model to match FFmpeg build
        exe.defineCMacro("WIN32_LEAN_AND_MEAN", null);
        exe.defineCMacro("HAVE_WIN32_THREADS", "1");
        exe.defineCMacro("PTWS32_STATIC_LIB", "1");
        exe.defineCMacro("_WIN32_WINNT", "0x0601");

        // Third-party libs + FFmpeg — use dynamic linking on native Windows
        // to avoid pulling in the entire MSYS2 static dependency tree
        const dynamic = .{ .preferred_link_mode = .dynamic };
        exe.linkSystemLibrary2("ssl", dynamic);
        exe.linkSystemLibrary2("crypto", dynamic);
        exe.linkSystemLibrary2("winpthread", dynamic);
        exe.linkSystemLibrary2("z", dynamic);
        exe.linkSystemLibrary2("x264", dynamic);
        exe.linkSystemLibrary2("stdc++", dynamic);
        exe.linkSystemLibrary("d3d11");

        // FFmpeg (dynamic on Windows)
        exe.linkSystemLibrary2("avcodec", dynamic);
        exe.linkSystemLibrary2("avfilter", dynamic);
        exe.linkSystemLibrary2("avdevice", dynamic);
        exe.linkSystemLibrary2("avformat", dynamic);
        exe.linkSystemLibrary2("avutil", dynamic);
        exe.linkSystemLibrary2("swresample", dynamic);
        exe.linkSystemLibrary2("swscale", dynamic);
    } else {
        // Cross-compile: static linking (macOS/Linux host)
        exe.linkSystemLibrary("ssl");
        exe.linkSystemLibrary("crypto");
        exe.linkSystemLibrary("avcodec");
        exe.linkSystemLibrary("avfilter");
        exe.linkSystemLibrary("avdevice");
        exe.linkSystemLibrary("avformat");
        exe.linkSystemLibrary("avutil");
        exe.linkSystemLibrary("swresample");
        exe.linkSystemLibrary("swscale");
    }

    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    const run_step = b.step("run", "Run the app");
    run_step.dependOn(&run_cmd.step);

    const lib_unit_tests = b.addTest(.{
        .root_source_file = b.path("src/root.zig"),
        .target = target,
        .optimize = optimize,
    });

    const run_lib_unit_tests = b.addRunArtifact(lib_unit_tests);
    const exe_unit_tests = b.addTest(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
    });
    const run_exe_unit_tests = b.addRunArtifact(exe_unit_tests);

    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_lib_unit_tests.step);
    test_step.dependOn(&run_exe_unit_tests.step);
}
