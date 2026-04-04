// Display is the high-level api for piglet into the display

const std = @import("std");
const Capture = @import("Capture.zig");
const Encoder = @import("Encoder.zig");
const Frame = @import("Frame.zig");
const Image = @import("Image.zig");

const Display = @This();

// internals
capture: Capture,
encoder: Encoder,

pub fn init() !Display {
    const capture = try Capture.init();
    const encoder = try Encoder.init(capture.dimensions.width, capture.dimensions.height);
    std.debug.print("Display dimensions: {}x{}\n", .{ capture.dimensions.width, capture.dimensions.height });
    return Display{
        .capture = capture,
        .encoder = encoder,
    };
}

pub fn deinit(self: *Display) void {
    self.encoder.deinit();
    self.capture.deinit();
}

pub fn screenshot(self: *Display, allocator: std.mem.Allocator, scale_options: Encoder.ScaleOptions) !Image {
    var frame = try self.capture.getFrame();
    defer frame.deinit();
    return try self.encoder.encode(frame, allocator, scale_options);
}

/// Capture a region of the screen as a PNG image.
/// Coordinates are in native display space.
pub fn screenshotRegion(self: *Display, allocator: std.mem.Allocator, x: c_int, y: c_int, w: c_int, h: c_int) !Image {
    var frame = try self.capture.getFrame();
    defer frame.deinit();

    var cropped = try frame.crop(x, y, w, h);
    defer cropped.deinit();

    // Encode at native crop size (no additional scaling)
    return try self.encoder.encode(cropped, allocator, .{
        .width = w,
        .height = h,
        .mode = .Exact,
    });
}

pub fn width(self: Display) u32 {
    // note: these are the dimensions of the capture, not the encoded image
    return @intCast(self.capture.dimensions.width);
}

pub fn height(self: Display) u32 {
    // note: these are the dimensions of the capture, not the encoded image
    return @intCast(self.capture.dimensions.height);
}
