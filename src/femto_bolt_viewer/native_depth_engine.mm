// Native macOS compute host. Camera-specific programs and calibration are
// supplied separately in a private, hash-verified profile; none are embedded.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr size_t MAX_RESOURCE = 32 * 1024 * 1024;
NSData *read_resource(NSString *base, NSString *name) {
    if (![name isKindOfClass:[NSString class]] || name.length == 0 || name.length > 128
        || ![name.lastPathComponent isEqual:name] || [name isEqual:@"."] || [name isEqual:@".."])
        throw std::runtime_error("Invalid native profile resource name");
    NSData *data = [NSData dataWithContentsOfFile:[base stringByAppendingPathComponent:name]];
    if (!data || data.length == 0 || data.length > MAX_RESOURCE)
        throw std::runtime_error("Missing or excessive native profile resource");
    return data;
}
NSUInteger integer(NSDictionary *object, NSString *key, NSUInteger minimum, NSUInteger maximum) {
    id value = object[key];
    if (![value isKindOfClass:[NSNumber class]]) throw std::runtime_error("Missing numeric profile field");
    double number = [value doubleValue];
    if (!std::isfinite(number) || number < minimum || number > maximum || std::floor(number) != number)
        throw std::runtime_error("Invalid numeric profile field");
    return static_cast<NSUInteger>(number);
}
bool read_exact(void *target, size_t size, bool clean_eof = false) {
    auto *bytes = static_cast<uint8_t *>(target);
    size_t done = 0;
    while (done < size) {
        size_t amount = fread(bytes + done, 1, size - done, stdin);
        if (!amount) {
            if (clean_eof && done == 0 && feof(stdin)) return false;
            throw std::runtime_error("Truncated native frame request");
        }
        done += amount;
    }
    return true;
}
void write_exact(const void *source, size_t size) {
    if (fwrite(source, 1, size, stdout) != size) throw std::runtime_error("Native response write failed");
}
#pragma pack(push, 1)
struct Ready { char magic[4]; uint32_t width, height, raw_bytes, thermal_bytes; };
struct Request { char magic[4]; uint32_t raw_bytes, thermal_bytes; uint64_t sequence; };
struct Response { char magic[4]; uint32_t status; uint64_t sequence; uint32_t width, height; float process_ms; };
#pragma pack(pop)
static_assert(sizeof(Ready) == 20 && sizeof(Request) == 20 && sizeof(Response) == 28);

class NativeEngine {
    id<MTLDevice> device_;
    id<MTLCommandQueue> queue_;
    NSDictionary *profile_;
    NSMutableDictionary *buffers_, *textures_;
    NSMutableArray *pipelines_;
    NSString *raw_key_, *thermal_key_, *depth_key_, *amplitude_key_;
public:
    NSUInteger width, height, raw_bytes;
    explicit NativeEngine(NSString *directory) {
        NSError *error = nil;
        profile_ = [NSJSONSerialization JSONObjectWithData:read_resource(directory, @"profile.json") options:0 error:&error];
        if (![profile_ isKindOfClass:[NSDictionary class]] || integer(profile_, @"schema_version", 1, 1) != 1)
            throw std::runtime_error("Unsupported native profile");
        width = integer(profile_, @"width", 1, 1024); height = integer(profile_, @"height", 1, 1024);
        if (!((width == 512 && height == 512) || (width == 640 && height == 576)))
            throw std::runtime_error("Unsupported native depth dimensions");
        raw_bytes = width * height * 9 * 2;
        for (NSString *key in @[@"buffers", @"textures"])
            if (![profile_[key] isKindOfClass:[NSDictionary class]] || [profile_[key] count] > 16)
                throw std::runtime_error("Invalid native resource set");
        if (![profile_[@"stages"] isKindOfClass:[NSArray class]] || [profile_[@"stages"] count] != 2)
            throw std::runtime_error("Native profile requires two compute stages");
        device_ = MTLCreateSystemDefaultDevice();
        if (!device_) throw std::runtime_error("No native Metal device available");
        queue_ = [device_ newCommandQueue];
        buffers_ = [NSMutableDictionary dictionary]; textures_ = [NSMutableDictionary dictionary];
        pipelines_ = [NSMutableArray array];
        for (NSString *key in profile_[@"buffers"]) {
            NSDictionary *spec = profile_[@"buffers"][key];
            NSUInteger length = integer(spec, @"length", 1, MAX_RESOURCE);
            id<MTLBuffer> buffer = [device_ newBufferWithLength:length options:MTLResourceStorageModeShared];
            if (!buffer) throw std::runtime_error("Native buffer allocation failed");
            memset(buffer.contents, 0, length);
            if (spec[@"file"]) {
                NSData *data = read_resource(directory, spec[@"file"]);
                if (data.length != length) throw std::runtime_error("Native buffer size mismatch");
                memcpy(buffer.contents, data.bytes, length);
            }
            buffers_[key] = buffer;
        }
        for (NSString *key in profile_[@"textures"]) {
            NSDictionary *spec = profile_[@"textures"][key];
            NSUInteger format = integer(spec, @"format", 1, 65535);
            if (format != 33322 && format != 33328) throw std::runtime_error("Unsupported native texture format");
            NSUInteger w = integer(spec, @"width", 1, 2048), h = integer(spec, @"height", 1, 4096);
            NSUInteger stride = w * (format == 33322 ? 2 : 8);
            MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:
                format == 33322 ? MTLPixelFormatR16Unorm : MTLPixelFormatRG32Float width:w height:h mipmapped:NO];
            descriptor.storageMode = MTLStorageModeShared;
            descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
            id<MTLTexture> texture = [device_ newTextureWithDescriptor:descriptor];
            if (!texture) throw std::runtime_error("Native texture allocation failed");
            NSData *data = spec[@"file"] ? read_resource(directory, spec[@"file"]) : [NSMutableData dataWithLength:stride * h];
            if (data.length != stride * h) throw std::runtime_error("Native texture size mismatch");
            [texture replaceRegion:MTLRegionMake2D(0, 0, w, h) mipmapLevel:0 withBytes:data.bytes bytesPerRow:stride];
            textures_[key] = texture;
        }
        for (NSDictionary *stage in profile_[@"stages"]) {
            NSString *source = [[NSString alloc] initWithData:read_resource(directory, stage[@"source"]) encoding:NSUTF8StringEncoding];
            if (!source) throw std::runtime_error("Invalid native compute source encoding");
            MTLCompileOptions *options = [MTLCompileOptions new];
            if (@available(macOS 15.0, *)) options.mathMode = MTLMathModeSafe;
            else options.fastMathEnabled = NO;
            id<MTLLibrary> library = [device_ newLibraryWithSource:source options:options error:&error];
            if (!library) throw std::runtime_error(error.description.UTF8String);
            id<MTLComputePipelineState> pipeline = [device_ newComputePipelineStateWithFunction:[library newFunctionWithName:@"main0"] error:&error];
            if (!pipeline) throw std::runtime_error(error.description.UTF8String);
            [pipelines_ addObject:pipeline];
        }
        raw_key_ = [@(integer(profile_, @"raw_buffer_id", 1, 16)) stringValue];
        thermal_key_ = [@(integer(profile_, @"thermal_buffer_id", 1, 16)) stringValue];
        depth_key_ = [@(integer(profile_, @"depth_texture_id", 1, 16)) stringValue];
        amplitude_key_ = [@(integer(profile_, @"amplitude_texture_id", 1, 16)) stringValue];
        if ([buffers_[raw_key_] length] != raw_bytes || [buffers_[thermal_key_] length] != 112)
            throw std::runtime_error("Native dynamic buffer dimensions mismatch");
        for (NSString *key in @[depth_key_, amplitude_key_]) {
            id<MTLTexture> texture = textures_[key];
            if (!texture || texture.width != width || texture.height != height || texture.pixelFormat != MTLPixelFormatR16Unorm)
                throw std::runtime_error("Native output texture dimensions mismatch");
        }
        fprintf(stderr, "Native Metal depth ready on %s (%lu x %lu)\n", device_.name.UTF8String, (unsigned long)width, (unsigned long)height);
    }
    float process(std::vector<uint8_t> &depth, std::vector<uint8_t> &amplitude) {
        read_exact([buffers_[raw_key_] contents], raw_bytes);
        read_exact([buffers_[thermal_key_] contents], 112);
        const auto start = std::chrono::steady_clock::now();
        id<MTLCommandBuffer> command = [queue_ commandBuffer];
        NSUInteger index = 0;
        for (NSDictionary *stage in profile_[@"stages"]) {
            id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
            id<MTLComputePipelineState> pipeline = pipelines_[index++];
            [encoder setComputePipelineState:pipeline];
            for (NSDictionary *binding in stage[@"buffers"]) {
                id<MTLBuffer> buffer = buffers_[[@(integer(binding, @"id", 1, 16)) stringValue]];
                if (!buffer) throw std::runtime_error("Missing native buffer binding");
                NSUInteger offset = integer(binding, @"offset", 0, buffer.length - 1);
                [encoder setBuffer:buffer offset:offset atIndex:integer(binding, @"index", 0, 15)];
            }
            for (NSDictionary *binding in stage[@"textures"]) {
                id<MTLTexture> texture = textures_[[@(integer(binding, @"id", 1, 16)) stringValue]];
                if (!texture) throw std::runtime_error("Missing native texture binding");
                [encoder setTexture:texture atIndex:integer(binding, @"index", 0, 15)];
            }
            NSArray *g = stage[@"groups"], *t = stage[@"threads"];
            if (![g isKindOfClass:[NSArray class]] || ![t isKindOfClass:[NSArray class]] || g.count != 3 || t.count != 3)
                throw std::runtime_error("Invalid native dispatch dimensions");
            NSUInteger threads = 1;
            for (NSUInteger n = 0; n < 3; ++n) {
                if ([g[n] unsignedIntegerValue] == 0 || [g[n] unsignedIntegerValue] > 4096 || [t[n] unsignedIntegerValue] == 0 || [t[n] unsignedIntegerValue] > 1024)
                    throw std::runtime_error("Native dispatch dimensions out of bounds");
                threads *= [t[n] unsignedIntegerValue];
            }
            if (threads > pipeline.maxTotalThreadsPerThreadgroup) throw std::runtime_error("Native threadgroup exceeds device limit");
            [encoder dispatchThreadgroups:MTLSizeMake([g[0] unsignedIntegerValue], [g[1] unsignedIntegerValue], [g[2] unsignedIntegerValue])
                   threadsPerThreadgroup:MTLSizeMake([t[0] unsignedIntegerValue], [t[1] unsignedIntegerValue], [t[2] unsignedIntegerValue])];
            [encoder endEncoding];
        }
        [command commit]; [command waitUntilCompleted];
        if (command.error) throw std::runtime_error(command.error.description.UTF8String);
        id<MTLTexture> z = textures_[depth_key_], ab = textures_[amplitude_key_];
        [z getBytes:depth.data() bytesPerRow:width * 2 fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        [ab getBytes:amplitude.data() bytesPerRow:width * 2 fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        return std::chrono::duration<float, std::milli>(std::chrono::steady_clock::now() - start).count();
    }
};
}

int main(int argc, char **argv) {
    if (argc != 2) { fprintf(stderr, "Usage: native_depth_engine PROFILE_DIRECTORY\n"); return 2; }
    @autoreleasepool { try {
        NativeEngine engine(@(argv[1]));
        Ready ready{{'N','D','R','1'}, static_cast<uint32_t>(engine.width), static_cast<uint32_t>(engine.height), static_cast<uint32_t>(engine.raw_bytes), 112};
        write_exact(&ready, sizeof(ready)); fflush(stdout);
        std::vector<uint8_t> depth(engine.width * engine.height * 2), amplitude(depth.size());
        Request request{}; uint64_t previous = 0;
        while (read_exact(&request, sizeof(request), true)) {
            @autoreleasepool {
                if (memcmp(request.magic, "NDP1", 4) || request.raw_bytes != engine.raw_bytes || request.thermal_bytes != 112 || request.sequence <= previous)
                    throw std::runtime_error("Invalid native frame request");
                previous = request.sequence;
                float ms = engine.process(depth, amplitude);
                Response response{{'N','D','O','1'}, 0, request.sequence, static_cast<uint32_t>(engine.width), static_cast<uint32_t>(engine.height), ms};
                write_exact(&response, sizeof(response)); write_exact(depth.data(), depth.size()); write_exact(amplitude.data(), amplitude.size()); fflush(stdout);
            }
        }
        return 0;
    } catch (const std::exception &error) { fprintf(stderr, "native_depth_engine: %s\n", error.what()); return 1; }}
}
