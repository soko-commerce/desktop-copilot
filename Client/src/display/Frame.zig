// Frame is a zig wrapper around AVFrame
// This is a raw (unformatted) frame, passed from the capture device to the encoder

const std = @import("std");
const c = @import("c.zig").c;

const Frame = @This();

raw_frame: [*c]c.AVFrame,
dimensions: Dimensions,
pub const Dimensions = struct { width: c_int, height: c_int };

pub fn init(raw_frame: [*c]c.AVFrame) Frame {
    return Frame{
        .raw_frame = raw_frame,
        .dimensions = .{
            .width = raw_frame.*.width,
            .height = raw_frame.*.height,
        },
    };
}

pub fn deinit(self: *Frame) void {
    c.av_frame_free(&self.raw_frame);
}

/// Crop a rectangular region from this frame.
/// Coordinates are in the frame's pixel space.
/// The caller owns the returned frame and must call deinit().
pub fn crop(self: Frame, x: c_int, y: c_int, w: c_int, h: c_int) !Frame {
    // Validate bounds
    if (x < 0 or y < 0 or w <= 0 or h <= 0) return error.InvalidCropRegion;
    if (x + w > self.dimensions.width or y + h > self.dimensions.height) return error.CropOutOfBounds;

    // Create new frame with cropped dimensions
    var new_frame = c.av_frame_alloc();
    if (new_frame == null) return error.FrameAllocFailed;
    errdefer c.av_frame_free(&new_frame);

    new_frame.*.format = self.raw_frame.*.format;
    new_frame.*.width = w;
    new_frame.*.height = h;

    const ret = c.av_frame_get_buffer(new_frame, 0);
    if (ret < 0) return error.FrameBufferAllocFailed;

    // Determine bytes per pixel from format
    // gdigrab outputs BGR0 (4 bpp), but handle RGB24 (3 bpp) too
    const bpp: usize = switch (self.raw_frame.*.format) {
        c.AV_PIX_FMT_BGR0, c.AV_PIX_FMT_BGRA, c.AV_PIX_FMT_RGBA, c.AV_PIX_FMT_RGB0 => 4,
        c.AV_PIX_FMT_RGB24, c.AV_PIX_FMT_BGR24 => 3,
        else => 4, // default assumption for gdigrab
    };

    const src_data = self.raw_frame.*.data[0];
    const dst_data = new_frame.*.data[0];
    const src_linesize: usize = @intCast(self.raw_frame.*.linesize[0]);
    const dst_linesize: usize = @intCast(new_frame.*.linesize[0]);
    const copy_bytes: usize = @as(usize, @intCast(w)) * bpp;

    var row: usize = 0;
    const h_usize: usize = @intCast(h);
    while (row < h_usize) : (row += 1) {
        const src_offset = (@as(usize, @intCast(y)) + row) * src_linesize + @as(usize, @intCast(x)) * bpp;
        const dst_offset = row * dst_linesize;
        @memcpy(dst_data[dst_offset..dst_offset + copy_bytes], src_data[src_offset..src_offset + copy_bytes]);
    }

    return Frame.init(new_frame);
}

pub fn create(width: c_int, height: c_int, format: c_int) !Frame {
    var frame = c.av_frame_alloc();
    if (frame == null) return error.FrameAllocFailed;
    errdefer c.av_frame_free(&frame);

    frame.*.format = format;
    frame.*.width = width;
    frame.*.height = height;

    // Allocate actual frame buffers
    const ret = c.av_frame_get_buffer(frame, 0);
    if (ret < 0) return error.FrameBufferAllocFailed;

    return init(frame);
}
