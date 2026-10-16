// record_app.swift: capture one app's audio (and optionally the mic) via ScreenCaptureKit.
//
// Built on first use by `whosaid record --app` (see ensure_record_app_helper in
// the whosaid script) and cached under ~/.cache/whosaid, keyed on this file's hash.
//
//   record-app --list
//   record-app --app <bundle-id|name> --out-dir DIR [--no-mic] [--max-seconds N]
//
// Writes DIR/app.wav and, unless --no-mic, DIR/mic.wav: mono 16 kHz, both on
// the same timeline (sample 0 = the first buffer either leg delivered), padded
// to the same length so the caller can join them into one stereo file.
//
// ScreenCaptureKit only delivers app buffers while the app is producing sound,
// so each leg is placed by presentation timestamp and gaps are zero-filled;
// appending buffers back to back would drift the two legs apart on every pause.
// SCK also refuses to run audio-only, so a 2x2 video stream at 1 fps rides
// along and its frames are discarded. Ctrl-C / SIGTERM stops capture and
// closes the files (AVAudioFile finalizes the WAV header on close); exit 0.
// If macOS kills the stream mid-capture the files are closed the same way and
// the exit is 3 (5 if the Screen Recording grant was revoked).

import AVFoundation
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit

let sampleRate = 16_000.0

func log(_ s: String) {
    FileHandle.standardError.write(("record-app: " + s + "\n").data(using: .utf8)!)
}

func isPermissionError(_ error: Error) -> Bool {
    let ns = error as NSError
    if ns.domain == "com.apple.ScreenCaptureKit.SCStreamErrorDomain" && ns.code == -3801 { return true }
    let d = ns.localizedDescription.lowercased()
    return d.contains("not authorized") || d.contains("not granted") || d.contains("declined")
        || d.contains("permission")
}

func permissionExit() -> Never {
    permissionHelp()
    exit(5)
}

func permissionHelp() {
    log("Screen & System Audio Recording permission is missing for your terminal app.")
    log("Fix: System Settings -> Privacy & Security -> Screen & System Audio Recording -> enable your terminal, then restart it.")
    log("Open it with: open \"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture\"")
}

func shareableContent() async -> SCShareableContent {
    do {
        return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    } catch {
        if isPermissionError(error) { permissionExit() }
        log("ScreenCaptureKit failed: \(error.localizedDescription)")
        exit(1)
    }
}

/// One app per bundle ID, named, sorted by name.
func uniqueApps(_ content: SCShareableContent) -> [SCRunningApplication] {
    Dictionary(grouping: content.applications, by: { $0.bundleIdentifier })
        .compactMap { $0.value.first }
        .filter { !$0.applicationName.isEmpty && !$0.bundleIdentifier.isEmpty }
        .sorted { $0.applicationName.lowercased() < $1.applicationName.lowercased() }
}

/// Exact bundle ID first, then case-insensitive app name, then a unique
/// case-insensitive substring of either.
func resolveApp(_ query: String, in apps: [SCRunningApplication]) -> SCRunningApplication? {
    if let a = apps.first(where: { $0.bundleIdentifier == query }) { return a }
    let q = query.lowercased()
    if let a = apps.first(where: { $0.applicationName.lowercased() == q }) { return a }
    let partial = apps.filter {
        $0.applicationName.lowercased().contains(q) || $0.bundleIdentifier.lowercased().contains(q)
    }
    if partial.count == 1 { return partial[0] }
    if partial.count > 1 {
        log("'\(query)' matches several apps: " + partial.map { $0.bundleIdentifier }.joined(separator: ", "))
    }
    return nil
}

/// One output leg: converts to mono 16 kHz Float32 and writes at the sample
/// offset its timestamp implies, zero-filling any gap.
final class Leg {
    let name: String
    let file: AVAudioFile
    let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
    var converter: AVAudioConverter?
    var written: Int64 = 0
    var peak: Float = 0
    var buffers = 0

    init(name: String, url: URL) throws {
        self.name = name
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    func convert(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        if input.format == target { return input }
        if converter == nil || converter!.inputFormat != input.format {
            converter = AVAudioConverter(from: input.format, to: target)
        }
        guard let conv = converter else { return nil }
        let cap = AVAudioFrameCount(Double(input.frameLength) * sampleRate / input.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return nil }
        var fed = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        if let err { log("\(name): convert failed: \(err.localizedDescription)"); return nil }
        return out
    }

    func writeZeros(_ n: Int64) {
        var left = n
        while left > 0 {
            let chunk = AVAudioFrameCount(min(left, 16_000))
            guard let z = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: chunk) else { return }
            z.frameLength = chunk  // AVAudioPCMBuffer memory starts zeroed
            try? file.write(from: z)
            written += Int64(chunk)
            left -= Int64(chunk)
        }
    }

    /// `offset`: where this buffer belongs on the shared timeline, in samples.
    func append(_ input: AVAudioPCMBuffer, at offset: Int64) {
        guard let buf = convert(input), buf.frameLength > 0 else { return }
        // Fill gaps over 20 ms (the app went quiet and SCK stopped sending);
        // smaller jitter is ignored so a continuous leg stays continuous.
        if offset - written > Int64(sampleRate / 50) { writeZeros(offset - written) }
        if let ch = buf.floatChannelData?[0] {
            for i in 0..<Int(buf.frameLength) { peak = max(peak, abs(ch[i])) }
        }
        do { try file.write(from: buf) } catch { log("\(name): write failed: \(error.localizedDescription)") }
        written += Int64(buf.frameLength)
        buffers += 1
    }

    func takePeakDB() -> String {
        defer { peak = 0 }
        return peak > 0 ? String(format: "%.0f dB", 20 * log10(peak)) : "silent"
    }
}

func pcmBuffer(_ sb: CMSampleBuffer) -> AVAudioPCMBuffer? {
    guard CMSampleBufferIsValid(sb), CMSampleBufferDataIsReady(sb),
          let fd = CMSampleBufferGetFormatDescription(sb) else { return nil }
    let fmt = AVAudioFormat(cmAudioFormatDescription: fd)
    let n = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
    guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { return nil }
    buf.frameLength = n
    let st = CMSampleBufferCopyPCMDataIntoAudioBufferList(sb, at: 0, frameCount: Int32(n), into: buf.mutableAudioBufferList)
    return st == noErr ? buf : nil
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let app: Leg
    let mic: Leg?
    /// All sample callbacks land here, serially, so buffers stay in capture
    /// order (a concurrent queue reorders ~10 ms chunks under load).
    let queue = DispatchQueue(label: "whosaid.record-app.samples", qos: .userInitiated)
    var t0: Double?
    var stream: SCStream?
    var finished = false

    init(app: Leg, mic: Leg?) {
        self.app = app
        self.mic = mic
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        let leg: Leg
        switch type {
        case .audio: leg = app
        case .microphone:
            guard let mic else { return }
            leg = mic
        default: return  // the placeholder video frames
        }
        guard !finished, let buf = pcmBuffer(sb) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb).seconds
        if t0 == nil { t0 = pts }
        let offset = Int64(((pts - t0!) * sampleRate).rounded())
        leg.append(buf, at: max(0, offset))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // Close the files either way: the audio captured so far is valid and
        // the caller keeps it (exit 3 = ended early, 5 = permission revoked).
        log("capture stopped by macOS: \(error.localizedDescription)")
        if isPermissionError(error) {
            permissionHelp()
            finish(code: 5)
        } else {
            finish(code: 3)
        }
    }

    /// Stop capture, pad both legs to the same length, close the files.
    func finish(code: Int32) {
        queue.sync {
            if finished { return }
            finished = true
            if let mic {
                let n = max(app.written, mic.written)
                app.writeZeros(n - app.written)
                mic.writeZeros(n - mic.written)
            }
            // close() writes the final WAV header; exit() would skip deinit.
            app.file.close()
            mic?.file.close()
        }
        let s = stream
        Task {
            try? await s?.stopCapture()
            let secs = Double(self.app.written) / sampleRate
            log(String(format: "stopped after %.1fs (app buffers: %d%@)", secs, self.app.buffers,
                       self.mic.map { ", mic buffers: \($0.buffers)" } ?? ""))
            exit(code)
        }
    }

    func start(target: SCRunningApplication, display: SCDisplay) async throws {
        let filter = SCContentFilter(display: display, including: [target], exceptingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 5
        cfg.showsCursor = false
        cfg.capturesAudio = true
        cfg.sampleRate = Int(sampleRate)
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        if mic != nil { cfg.captureMicrophone = true }
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: .global(qos: .utility))
        if mic != nil { try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue) }
        try await s.startCapture()
        stream = s
    }
}

// ---- main ---------------------------------------------------------------------

var args = Array(CommandLine.arguments.dropFirst())
var list = false
var appQuery: String?
var outDir: String?
var withMic = true
var maxSeconds = 10_800.0

func usage() -> Never {
    log("usage: record-app --list | --app <bundle-id|name> --out-dir DIR [--no-mic] [--max-seconds N]")
    exit(2)
}

func nextValue() -> String {
    if args.isEmpty { usage() }
    return args.removeFirst()
}

while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--list": list = true
    case "--app": appQuery = nextValue()
    case "--out-dir": outDir = nextValue()
    case "--no-mic": withMic = false
    case "--max-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0 else { usage() }
        maxSeconds = v
    default: usage()
    }
}

// Ignore default SIGINT/SIGTERM handling; dispatch sources below finish cleanly.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
var recorder: Recorder?

Task {
    let content = await shareableContent()
    let apps = uniqueApps(content)
    if list {
        for a in apps { print("\(a.bundleIdentifier)\t\(a.applicationName)") }
        exit(0)
    }
    guard let query = appQuery, let dir = outDir else { usage() }
    guard let target = resolveApp(query, in: apps) else {
        log("no running app matches '\(query)'. Running apps: whosaid record --list-apps")
        exit(2)
    }
    guard let display = content.displays.first else {
        log("no display available to attach the capture stream to.")
        exit(1)
    }
    let base = URL(fileURLWithPath: dir)
    let rec: Recorder
    do {
        rec = Recorder(app: try Leg(name: "app", url: base.appendingPathComponent("app.wav")),
                       mic: withMic ? try Leg(name: "mic", url: base.appendingPathComponent("mic.wav")) : nil)
    } catch {
        log("cannot open output in \(dir): \(error.localizedDescription)")
        exit(1)
    }
    recorder = rec
    for sig in [SIGINT, SIGTERM] {
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { rec.finish(code: 0) }
        src.resume()
        signalSources.append(src)
    }
    do {
        try await rec.start(target: target, display: display)
    } catch {
        if isPermissionError(error) { permissionExit() }
        log("could not start capture: \(error.localizedDescription)")
        exit(1)
    }
    log("capturing \(target.applicationName) (\(target.bundleIdentifier))\(withMic ? " + mic" : "") — Ctrl-C to stop.")

    // Progress: one line every 10 s so a dead leg is visible while it runs.
    let started = Date()
    while true {
        try? await Task.sleep(nanoseconds: 10_000_000_000)
        let elapsed = Date().timeIntervalSince(started)
        var line = String(format: "%5.0fs  app peak %@", elapsed, rec.queue.sync { rec.app.takePeakDB() })
        if let mic = rec.mic { line += "  mic peak " + rec.queue.sync { mic.takePeakDB() } }
        log(line)
        if elapsed >= maxSeconds {
            log("reached --max-seconds \(Int(maxSeconds)); stopping.")
            rec.finish(code: 0)
        }
    }
}

dispatchMain()
