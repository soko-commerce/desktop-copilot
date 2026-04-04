const std = @import("std");
const Computer = @import("Computer.zig");
const MouseButton = @import("input/Mouse.zig").Button;
const Coordinates = @import("input/Mouse.zig").Coordinates;
const httpz = @import("httpz"); // Note we use the version pinned to zig 0.13
const win_c = @import("input/c.zig").c;

const Deps = struct {
    computer: *Computer,
};

pub fn run(allocator: std.mem.Allocator, computer: *Computer, port: u16) !void {
    var app = Deps{ .computer = computer };

    // Pass Deps into ServerApp makes that pointer available in the handler
    // Useful for DB, config, a
    var server = try httpz.ServerApp(*Deps)
        .init(
        allocator,
        .{ .port = port },
        &app,
    );
    defer {
        // clean shutdown, finishes serving any live request
        server.stop();
        server.deinit();
    }

    var router = server.router();

    // Display endpoints
    router.get("/computer/display/screenshot", getScreenshot);
    router.get("/computer/display/dimensions", getDimensions);
    router.get("/computer/display/metrics", getDisplayMetrics);

    // Input endpoints
    // Keyboard
    router.post("/computer/input/keyboard/type", postKeyboardType); // body: { text: string }
    router.post("/computer/input/keyboard/key", postKeyboardKey); // body: { key: string }

    // Mouse
    router.get("/computer/input/mouse/position", getMousePosition);
    router.post("/computer/input/mouse/move", postMouseMove); // body: { x: number, y: number }
    router.post("/computer/input/mouse/click", postMouseClick); // body: { button?: "left"|"right" }

    // File system endpoints
    router.get("/computer/fs/list", getFsList); // query: { path: string }
    router.get("/computer/fs/read", getFsRead); // query: { path: string }
    router.post("/computer/fs/write", postFsWrite); // body: { path: string, content: string }
    // router.ws("/computer/fs/watch", null); // query: { path: string } // TODO: Figure out websockets

    // Shell endpoints
    // Sync commands
    router.post("/computer/shell/cmd/exec", postHandleCMDExec); // body: { command: string }
    router.post("/computer/shell/powershell/exec", postHandlePowerShellExec); // body: { command: string }

    // Interactive sessions via websocket // TODO: Figure out websockets
    // router.ws("/computer/shell/cmd/session", handleCmdSession);
    // router.ws("/computer/shell/powershell/session", handlePsSession);

    // blocks
    std.debug.print("Local server running at http://localhost:{d}\n", .{port});
    try server.listen();
}

fn notImplemented(_: *httpz.Request, res: *httpz.Response) !void {
    res.status = 501;
    try res.json(.{}, .{});
}

fn getScreenshot(deps: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    // Check for region crop parameters
    const x_str = req.query("x");
    const y_str = req.query("y");
    const w_str = req.query("w");
    const h_str = req.query("h");

    const image = if (x_str != null and y_str != null and w_str != null and h_str != null) blk: {
        const x = std.fmt.parseInt(c_int, x_str.?, 10) catch {
            res.status = 400;
            try res.json(.{ .@"error" = "invalid x parameter" }, .{});
            return;
        };
        const y = std.fmt.parseInt(c_int, y_str.?, 10) catch {
            res.status = 400;
            try res.json(.{ .@"error" = "invalid y parameter" }, .{});
            return;
        };
        const w = std.fmt.parseInt(c_int, w_str.?, 10) catch {
            res.status = 400;
            try res.json(.{ .@"error" = "invalid w parameter" }, .{});
            return;
        };
        const h = std.fmt.parseInt(c_int, h_str.?, 10) catch {
            res.status = 400;
            try res.json(.{ .@"error" = "invalid h parameter" }, .{});
            return;
        };
        break :blk try deps.computer.display.screenshotRegion(res.arena, x, y, w, h);
    } else blk: {
        break :blk try deps.computer.display.screenshot(res.arena, .{
            .width = 1024,
            .height = 768,
            .mode = .Exact,
        });
    };

    res.status = 200;
    res.header("Content-Type", "image/png");
    try res.writer().writeAll(image.bytes);
}

fn getDimensions(deps: *Deps, _: *httpz.Request, res: *httpz.Response) !void {
    res.status = 200;
    try res.json(.{
        .width = deps.computer.mouse.input_width,
        .height = deps.computer.mouse.input_height,
        .capture_width = deps.computer.display.width(),
        .capture_height = deps.computer.display.height(),
    }, .{});
}

fn getDisplayMetrics(deps: *Deps, _: *httpz.Request, res: *httpz.Response) !void {
    // System metrics via user32.dll GetSystemMetrics — no privileges needed
    const SM_CXSCREEN = 0; // Primary monitor logical width
    const SM_CYSCREEN = 1; // Primary monitor logical height
    const SM_CXVIRTUALSCREEN = 78; // Virtual screen width (all monitors)
    const SM_CYVIRTUALSCREEN = 79; // Virtual screen height
    const SM_XVIRTUALSCREEN = 76; // Virtual screen left edge
    const SM_YVIRTUALSCREEN = 77; // Virtual screen top edge

    res.status = 200;
    try res.json(.{
        .pw = win_c.GetSystemMetrics(SM_CXSCREEN),
        .ph = win_c.GetSystemMetrics(SM_CYSCREEN),
        .vw = win_c.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        .vh = win_c.GetSystemMetrics(SM_CYVIRTUALSCREEN),
        .vx = win_c.GetSystemMetrics(SM_XVIRTUALSCREEN),
        .vy = win_c.GetSystemMetrics(SM_YVIRTUALSCREEN),
        .gdigrab_w = deps.computer.display.width(),
        .gdigrab_h = deps.computer.display.height(),
    }, .{});
}

const KeyboardPayload = struct { text: []const u8 };

fn postKeyboardType(deps: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(KeyboardPayload) orelse {
        res.status = 400;
        try res.json(.{}, .{});
        return;
    };
    try deps.computer.keyboard.inputString(body.text);
}

fn postKeyboardKey(deps: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(KeyboardPayload);
    if (body == null) {
        res.status = 400;
        try res.json(.{}, .{});
        return;
    }
    try deps.computer.keyboard.inputXDO(body.?.text);
}

fn getMousePosition(deps: *Deps, _: *httpz.Request, res: *httpz.Response) !void {
    const coordinates = try deps.computer.mouse.coordinates();
    res.status = 200;
    try res.json(.{
        .x = coordinates.x,
        .y = coordinates.y,
    }, .{});
}

const MouseMovePayload = struct { x: i32, y: i32, instant: bool = false };
const ButtonEnum = enum {
    left,
    right,
};
const MouseClickPayload = struct { x: ?i32 = null, y: ?i32 = null, button: ButtonEnum, down: bool, instant: bool = false };

fn postMouseMove(deps: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(MouseMovePayload) orelse {
        res.status = 400;
        try res.json(.{}, .{});
        return;
    };
    if (body.instant) {
        try deps.computer.mouse.moveInstant(.{ .x = body.x, .y = body.y });
    } else {
        try deps.computer.mouse.move(.{ .x = body.x, .y = body.y });
    }
}

fn postMouseClick(deps: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(MouseClickPayload) orelse {
        res.status = 400;
        try res.json(.{}, .{});
        return;
    };

    // If x,y not provided, use current position
    const target: Coordinates = if (body.x != null and body.y != null)
        .{ .x = body.x.?, .y = body.y.? }
    else
        try deps.computer.mouse.coordinates();

    // map public mouse enum API to our internal one
    const button: MouseButton = switch (body.button) {
        .left => MouseButton.left,
        .right => MouseButton.right,
    };
    if (body.instant) {
        try deps.computer.mouse.clickInstant(button, body.down, target);
    } else {
        try deps.computer.mouse.click(button, body.down, target);
    }
}

fn getFsList(_: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    try notImplemented(req, res);
}

fn getFsRead(_: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    try notImplemented(req, res);
}

fn postFsWrite(_: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    try notImplemented(req, res);
}

const ShellPayload = struct { command: []const u8 };

fn postHandleCMDExec(_: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(ShellPayload) orelse {
        res.status = 400;
        try res.json(.{ .@"error" = "missing command field" }, .{});
        return;
    };

    var child = std.process.Child.init(
        &.{ "cmd.exe", "/C", body.command },
        res.arena,
    );
    child.stdout_behavior = .Pipe;
    child.stderr_behavior = .Pipe;

    try child.spawn();

    const stdout = try child.stdout.?.reader().readAllAlloc(res.arena, 1024 * 1024);
    const stderr = try child.stderr.?.reader().readAllAlloc(res.arena, 1024 * 1024);
    const term = try child.wait();

    const exit_code: i32 = switch (term) {
        .Exited => |code| @as(i32, @intCast(code)),
        else => -1,
    };

    res.status = 200;
    try res.json(.{
        .stdout = stdout,
        .stderr = stderr,
        .exitCode = exit_code,
    }, .{});
}

fn postHandlePowerShellExec(_: *Deps, req: *httpz.Request, res: *httpz.Response) !void {
    const body = try req.json(ShellPayload) orelse {
        res.status = 400;
        try res.json(.{ .@"error" = "missing command field" }, .{});
        return;
    };

    var child = std.process.Child.init(
        &.{ "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", body.command },
        res.arena,
    );
    child.stdout_behavior = .Pipe;
    child.stderr_behavior = .Pipe;

    try child.spawn();

    const stdout = try child.stdout.?.reader().readAllAlloc(res.arena, 1024 * 1024);
    const stderr = try child.stderr.?.reader().readAllAlloc(res.arena, 1024 * 1024);
    const term = try child.wait();

    const exit_code: i32 = switch (term) {
        .Exited => |code| @as(i32, @intCast(code)),
        else => -1,
    };

    res.status = 200;
    try res.json(.{
        .stdout = stdout,
        .stderr = stderr,
        .exitCode = exit_code,
    }, .{});
}
