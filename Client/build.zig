const std = @import("std");

// Zig 0.13.0 required.

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

    // Add FFmpeg include and lib paths (from vendor directory)
    exe.addSystemIncludePath(b.path("vendor/ffmpeg/include"));
    exe.addSystemIncludePath(b.path("vendor/ffmpeg/include/ffmpeg"));
    exe.addLibraryPath(b.path("vendor/ffmpeg/lib"));

    // Additional vendor lib path for x264, winpthread, etc.
    exe.addLibraryPath(b.path("vendor/lib"));

    const host_os = @import("builtin").os.tag;
    const target_info = target.result;

    if (host_os == .windows) {
        // Native Windows build — use MSYS2 MinGW64 paths if available
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
        exe.linkSystemLibrary("ssl");
        exe.linkSystemLibrary("crypto");

        // winpthread — try vendor/lib first, fall back to system paths
        exe.linkSystemLibrary("winpthread");

        // Define Windows threading model to match FFmpeg build
        exe.defineCMacro("WIN32_LEAN_AND_MEAN", null);
        exe.defineCMacro("HAVE_WIN32_THREADS", "1");
        exe.defineCMacro("PTWS32_STATIC_LIB", "1");
        exe.defineCMacro("_WIN32_WINNT", "0x0601");

        // Third-party libraries
        exe.linkSystemLibrary("z");
        exe.linkSystemLibrary("x264");
        exe.linkSystemLibrary("d3d11");
    }

    // Link FFmpeg static libraries
    exe.linkSystemLibrary("avcodec");
    exe.linkSystemLibrary("avfilter");
    exe.linkSystemLibrary("avdevice");
    exe.linkSystemLibrary("avformat");
    exe.linkSystemLibrary("avutil");
    exe.linkSystemLibrary("swresample");
    exe.linkSystemLibrary("swscale");

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
