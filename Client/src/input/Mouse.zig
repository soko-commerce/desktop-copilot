const std = @import("std");
const win = std.os.windows;
const c = @import("c.zig").c;

const Mouse = @This();

pub const Coordinates = struct {
    x: i32,
    y: i32,
};

scale_x: f64, // SendInput uses a scale factor for 0-65535 range
scale_y: f64,
display_width: u32, // gdigrab capture dimensions (physical pixels)
display_height: u32,
input_width: u32, // SendInput target space (SM_CXSCREEN, logical pixels)
input_height: u32,

pub fn new(display_width: u32, display_height: u32) Mouse {
    // SendInput ABSOLUTE mode maps 0-65535 to the primary monitor's logical dimensions
    // (SM_CXSCREEN x SM_CYSCREEN), NOT the physical/gdigrab dimensions.
    // On high-DPI displays, gdigrab captures physical pixels (e.g., 3840x2160) but
    // SendInput targets the logical resolution (e.g., 1920x1080 at 200% DPI).
    const sm_w = c.GetSystemMetrics(0); // SM_CXSCREEN
    const sm_h = c.GetSystemMetrics(1); // SM_CYSCREEN
    const input_w: u32 = if (sm_w > 0) @intCast(sm_w) else display_width;
    const input_h: u32 = if (sm_h > 0) @intCast(sm_h) else display_height;

    std.debug.print("Mouse: gdigrab={d}x{d}, SendInput target={d}x{d}\n", .{ display_width, display_height, input_w, input_h });

    return .{
        .scale_x = 65535.0 / @as(f64, @floatFromInt(input_w)),
        .scale_y = 65535.0 / @as(f64, @floatFromInt(input_h)),
        .display_width = display_width,
        .display_height = display_height,
        .input_width = input_w,
        .input_height = input_h,
    };
}

pub fn coordinates(mouse: *Mouse) !Coordinates {
    var point: c.POINT = undefined;
    if (c.GetCursorPos(&point) == 0) {
        return error.GetCursorPosFailed;
    }

    // GetCursorPos returns logical coordinates in the virtual screen space.
    // Rescale to input_width/input_height space (SM_CXSCREEN) for consistency
    // with SendInput absolute mode targeting the primary monitor.
    const virtual_width = c.GetSystemMetrics(c.SM_CXVIRTUALSCREEN);
    const virtual_height = c.GetSystemMetrics(c.SM_CYVIRTUALSCREEN);

    return Coordinates{
        .x = @intFromFloat((@as(f64, @floatFromInt(point.x)) / @as(f64, @floatFromInt(virtual_width))) * @as(f64, @floatFromInt(mouse.input_width))),
        .y = @intFromFloat((@as(f64, @floatFromInt(point.y)) / @as(f64, @floatFromInt(virtual_height))) * @as(f64, @floatFromInt(mouse.input_height))),
    };
}

pub fn move(mouse: *Mouse, target: Coordinates) !void {
    if (target.x < 0 or target.x >= mouse.input_width or target.y < 0 or target.y >= mouse.input_height) {
        return error.OutOfBounds;
    }

    // We use SendInput function in Absolute mode
    // which requires the screen to be scaled by a factor of 65535.0

    // Linear interpolate to target for smooth movement
    const current = try mouse.coordinates();
    var current_x = @as(i32, @intFromFloat(@as(f64, @floatFromInt(current.x)) * mouse.scale_x));
    var current_y = @as(i32, @intFromFloat(@as(f64, @floatFromInt(current.y)) * mouse.scale_y));
    const target_x = @as(i32, @intFromFloat(@as(f64, @floatFromInt(target.x)) * mouse.scale_x));
    const target_y = @as(i32, @intFromFloat(@as(f64, @floatFromInt(target.y)) * mouse.scale_y));

    const steps = 20;
    const step_time = 5 * std.time.ns_per_ms; // milliseconds
    const step_x = @divFloor(target_x - current_x, steps);
    const step_y = @divFloor(target_y - current_y, steps);

    for (0..steps) |_| {
        current_x += step_x;
        current_y += step_y;
        var input = c.INPUT{
            .type = c.INPUT_MOUSE,
            .unnamed_0 = .{
                .mi = .{
                    .dx = current_x, // note: dx and dy in absolute mode are just absolute coordinates
                    .dy = current_y,
                    .mouseData = 0,
                    .dwFlags = c.MOUSEEVENTF_MOVE | c.MOUSEEVENTF_ABSOLUTE,
                    .time = 0,
                    .dwExtraInfo = 0,
                },
            },
        };

        if (c.SendInput(1, &input, @sizeOf(c.INPUT)) != 1) {
            return error.SendInputFailed;
        }
        std.time.sleep(step_time);
    }

    // One final move to ensure the mouse is actually there
    var input = c.INPUT{
        .type = c.INPUT_MOUSE,
        .unnamed_0 = .{
            .mi = .{
                .dx = target_x,
                .dy = target_y,
                .mouseData = 0,
                .dwFlags = c.MOUSEEVENTF_MOVE | c.MOUSEEVENTF_ABSOLUTE,
                .time = 0,
                .dwExtraInfo = 0,
            },
        },
    };

    if (c.SendInput(1, &input, @sizeOf(c.INPUT)) != 1) {
        return error.SendInputFailed;
    }

    // Wait for mouse to arrive at target since SendInput has a queue
    var arrived: bool = false;
    for (0..100) |_| {
        const coords = try mouse.coordinates();
        if ((coords.x >= target.x + 2 or coords.x <= target.x - 2) or
            (coords.y >= target.y + 2 or coords.y <= target.y - 2))
        {
            std.time.sleep(1 * std.time.ns_per_ms);
            continue;
        }
        arrived = true;
        break;
    }
}

/// Move mouse instantly to target (no smooth interpolation).
/// Saves ~100ms per move compared to smooth movement.
pub fn moveInstant(mouse: *Mouse, target: Coordinates) !void {
    if (target.x < 0 or target.x >= mouse.input_width or target.y < 0 or target.y >= mouse.input_height) {
        return error.OutOfBounds;
    }

    const target_x = @as(i32, @intFromFloat(@as(f64, @floatFromInt(target.x)) * mouse.scale_x));
    const target_y = @as(i32, @intFromFloat(@as(f64, @floatFromInt(target.y)) * mouse.scale_y));

    var input = c.INPUT{
        .type = c.INPUT_MOUSE,
        .unnamed_0 = .{
            .mi = .{
                .dx = target_x,
                .dy = target_y,
                .mouseData = 0,
                .dwFlags = c.MOUSEEVENTF_MOVE | c.MOUSEEVENTF_ABSOLUTE,
                .time = 0,
                .dwExtraInfo = 0,
            },
        },
    };

    if (c.SendInput(1, &input, @sizeOf(c.INPUT)) != 1) {
        return error.SendInputFailed;
    }

    // Brief wait for cursor to arrive
    std.time.sleep(1 * std.time.ns_per_ms);
}

pub const Button = enum {
    left,
    right,
};

pub fn click(mouse: *Mouse, button: Button, down: bool, target: Coordinates) !void {
    // First move to target position
    try mouse.move(target);

    var flags: c_ulong = 0;
    switch (button) {
        .left => {
            switch (down) {
                true => flags = c.MOUSEEVENTF_LEFTDOWN,
                false => flags = c.MOUSEEVENTF_LEFTUP,
            }
        },
        .right => {
            switch (down) {
                true => flags = c.MOUSEEVENTF_RIGHTDOWN,
                false => flags = c.MOUSEEVENTF_RIGHTUP,
            }
        },
    }

    var input = c.INPUT{
        .type = c.INPUT_MOUSE,
        .unnamed_0 = .{
            .mi = .{
                .dx = 0, // No movement needed
                .dy = 0, // No movement needed
                .mouseData = 0,
                .dwFlags = flags,
                .time = 0,
                .dwExtraInfo = 0,
            },
        },
    };

    if (c.SendInput(1, &input, @sizeOf(c.INPUT)) != 1) {
        return error.SendInputFailed;
    }
}

pub fn clickInstant(mouse: *Mouse, button: Button, down: bool, target: Coordinates) !void {
    try mouse.moveInstant(target);

    var flags: c_ulong = 0;
    switch (button) {
        .left => {
            switch (down) {
                true => flags = c.MOUSEEVENTF_LEFTDOWN,
                false => flags = c.MOUSEEVENTF_LEFTUP,
            }
        },
        .right => {
            switch (down) {
                true => flags = c.MOUSEEVENTF_RIGHTDOWN,
                false => flags = c.MOUSEEVENTF_RIGHTUP,
            }
        },
    }

    var input = c.INPUT{
        .type = c.INPUT_MOUSE,
        .unnamed_0 = .{
            .mi = .{
                .dx = 0,
                .dy = 0,
                .mouseData = 0,
                .dwFlags = flags,
                .time = 0,
                .dwExtraInfo = 0,
            },
        },
    };

    if (c.SendInput(1, &input, @sizeOf(c.INPUT)) != 1) {
        return error.SendInputFailed;
    }
}
