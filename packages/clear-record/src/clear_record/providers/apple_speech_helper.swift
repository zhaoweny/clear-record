// Apple SpeechTranscriber helper for clear-record's `apple-speech` backend.
//
// WHY THIS EXISTS
// ---------------
// The macOS 26 `SpeechAnalyzer` / `SpeechTranscriber` / `AssetInventory` /
// `AnalysisContext` API is **Swift-only**. The framework's Objective-C headers
// expose only the legacy `SFSpeechRecognizer` classes; the new types carry no
// `@objc` bridging (only the shared `@objc deinit`), so `pyobjc` cannot reach
// them. This tiny Swift program is therefore the bridge: it is compiled once and
// invoked as a subprocess, exactly like the project already invokes `whisper-cli`
// (see ADR-0019). It lives in `clear_record.providers`; `clear_record.core`
// stays vendor-free.
//
// CONTRACT
// --------
// Every subcommand writes one JSON object to `--out <path>` (or stdout when
// `--out` is omitted) and exits 0. Human-readable progress goes to stderr. On
// failure it writes a message to stderr and exits non-zero, and writes no JSON.
//
//   probe                       -> {"isAvailable": bool, "supportedLocales": [...]}
//   prepare [--locale X]        -> {"locale": "...", "installed": true, "reserved": bool}
//   transcribe --audio P
//              [--locale X]
//              [--term T] ...    -> {"locale": "...", "language": "...",
//                                    "audioDuration": s|null,
//                                    "segments": [{"start": s, "end": s,
//                                                  "text": "...",
//                                                  "confidence": c|null}]}
//
// Confidence is emitted only where Apple provides it (the
// `transcriptionConfidence` attributed string attribute); it is `null`
// otherwise, never invented. `--term` values become the `AnalysisContext`
// custom vocabulary (the glossary bias).

import Foundation
import Speech
import AVFoundation
import CoreMedia

@main
struct AppleSpeechHelper {
    static func main() async {
        do {
            var command = ""
            var audio: String?
            var localeHint: String?
            var out: String?
            var terms: [String] = []

            var args = CommandLine.arguments.dropFirst().makeIterator()
            while let arg = args.next() {
                switch arg {
                case "probe", "prepare", "transcribe":
                    command = arg
                case "--audio":
                    audio = args.next()
                case "--locale":
                    localeHint = args.next()
                case "--term":
                    if let term = args.next() { terms.append(term) }
                case "--out":
                    out = args.next()
                default:
                    fail("unknown argument \(arg)")
                }
            }

            switch command {
            case "probe":
                await runProbe(out: out)
            case "prepare":
                try await runPrepare(localeHint: localeHint, out: out)
            case "transcribe":
                try await runTranscribe(
                    audio: audio, localeHint: localeHint, terms: terms, out: out)
            default:
                fail("usage: apple_speech_helper {probe|prepare|transcribe} [options]")
            }
        } catch {
            fail("\(error)")
        }
    }

    /// The ASR module's own availability flag -- cheap, no asset download.
    static func runProbe(out: String?) async {
        let supported = await SpeechTranscriber.supportedLocales
        emit(
            [
                "isAvailable": SpeechTranscriber.isAvailable,
                "supportedLocales": supported.map { $0.identifier },
            ],
            to: out)
    }

    /// Install/reserve the locale's on-device asset. Provisioning only; once
    /// present, transcription is offline.
    static func runPrepare(localeHint: String?, out: String?) async throws {
        guard let locale = await resolveLocale(localeHint) else {
            fail("locale \(localeHint ?? "(current)") is not supported by SpeechTranscriber")
        }
        let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
        try await ensureAssets(locale: locale, transcriber: transcriber)
        let reserved = (try? await AssetInventory.reserve(locale: locale)) ?? false
        emit(
            [
                "locale": locale.identifier,
                "installed": true,
                "reserved": reserved,
            ],
            to: out)
    }

    /// Transcribe a file into finalized, audio-timeline-aligned segments.
    static func runTranscribe(
        audio: String?, localeHint: String?, terms: [String], out: String?
    ) async throws {
        guard let audio else { fail("--audio is required") }
        guard let locale = await resolveLocale(localeHint) else {
            fail("locale \(localeHint ?? "(current)") is not supported by SpeechTranscriber")
        }

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults],
            attributeOptions: [.audioTimeRange, .transcriptionConfidence])

        // The pool prepares the backend before transcribing; a `--language` the
        // prepare seam could not see is provisioned here, on first use.
        try await ensureAssets(locale: locale, transcriber: transcriber)

        let context = AnalysisContext()
        if !terms.isEmpty { context.contextualStrings[.general] = terms }

        let analyzer = SpeechAnalyzer(modules: [transcriber], options: nil)
        try await analyzer.setContext(context)

        let fileURL = URL(fileURLWithPath: audio)
        let audioFile = try AVAudioFile(forReading: fileURL)

        // Consume results concurrently with the analysis: volatile results drive
        // progress on stderr, finalized results become segments.
        let collector = Task { () throws -> [[String: Any]] in
            var segments: [[String: Any]] = []
            var lastReportedSecond = -1
            for try await result in transcriber.results {
                if result.isFinal {
                    segments.append(segment(from: result))
                } else {
                    let end = CMTimeGetSeconds(CMTimeRangeGetEnd(result.range))
                    let second = end.isFinite ? Int(end) : 0
                    if second > lastReportedSecond {
                        lastReportedSecond = second
                        note("transcribing… \(second)s")
                    }
                }
            }
            return segments
        }

        var audioDuration: Any = NSNull()
        if let last = try await analyzer.analyzeSequence(from: audioFile) {
            audioDuration = CMTimeGetSeconds(last)
            try await analyzer.finalizeAndFinish(through: last)
        } else {
            await analyzer.cancelAndFinishNow()
        }

        let segments = try await collector.value
        let language = locale.language.languageCode?.identifier ?? locale.identifier
        emit(
            [
                "locale": locale.identifier,
                "language": language,
                "audioDuration": audioDuration,
                "segments": segments,
            ],
            to: out)
    }

    // --- helpers ---------------------------------------------------------- //

    /// Download and reserve the locale's asset when it is not already installed.
    /// A no-op once installed, so repeat runs stay offline.
    static func ensureAssets(
        locale: Locale, transcriber: SpeechTranscriber
    ) async throws {
        let installed = await SpeechTranscriber.installedLocales
        if installed.contains(where: { $0.identifier == locale.identifier }) { return }
        guard
            let request = try await AssetInventory.assetInstallationRequest(
                supporting: [transcriber])
        else { return }

        note("downloading the \(locale.identifier) speech asset (one time)…")
        let progressTask = Task {
            while !request.progress.isFinished {
                let pct = Int((request.progress.fractionCompleted * 100).rounded())
                note("  speech asset \(pct)%")
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
        try await request.downloadAndInstall()
        progressTask.cancel()
        note("speech asset for \(locale.identifier) installed")
    }

    /// Resolve a BCP-47-ish hint (e.g. whisper's "en" or "zh") to a locale the
    /// module can serve, or the current locale when no hint is given.
    static func resolveLocale(_ hint: String?) async -> Locale? {
        let raw = hint.map { Locale(identifier: $0) } ?? Locale.current
        return await SpeechTranscriber.supportedLocale(equivalentTo: raw)
    }

    static func segment(from result: SpeechTranscriber.Result) -> [String: Any] {
        var start = CMTimeGetSeconds(result.range.start)
        var end = CMTimeGetSeconds(CMTimeRangeGetEnd(result.range))
        if !start.isFinite { start = 0 }
        if !(end > start) {
            // A final result without a usable overall range: fall back to the
            // per-run audio time ranges.
            for run in result.text.runs {
                guard let range = run[keyPath: \.audioTimeRange] else { continue }
                let runStart = CMTimeGetSeconds(range.start)
                let runEnd = CMTimeGetSeconds(CMTimeRangeGetEnd(range))
                if runStart.isFinite { start = min(start, runStart) }
                if runEnd.isFinite { end = max(end, runEnd) }
            }
        }

        var confidences: [Double] = []
        for run in result.text.runs {
            if let confidence = run[keyPath: \.transcriptionConfidence] {
                confidences.append(confidence)
            }
        }
        let confidence: Any =
            confidences.isEmpty
            ? NSNull()
            : confidences.reduce(0, +) / Double(confidences.count)

        let text = String(result.text.characters)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return ["start": start, "end": end, "text": text, "confidence": confidence]
    }

    static func emit(_ object: [String: Any], to path: String?) {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: object, options: [.sortedKeys])
        else {
            fail("could not encode the JSON result")
        }
        if let path {
            do {
                try data.write(to: URL(fileURLWithPath: path))
            } catch {
                fail("could not write \(path): \(error)")
            }
        } else {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))
        }
    }

    static func note(_ message: String) {
        FileHandle.standardError.write(Data("clear-record: \(message)\n".utf8))
    }

    static func fail(_ message: String) -> Never {
        note("error: \(message)")
        exit(1)
    }
}
