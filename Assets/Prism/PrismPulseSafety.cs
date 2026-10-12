using System;
using System.Collections.Generic;

namespace Prism
{
    /// The flashing-light rules for the visual heartbeat, in one place with no Unity types so
    /// they can be tested on their own. A straight port of the team's web/shared/field_mapping.js
    /// (visualRateGain, pulseAmplitude), field_pulse.js (BeatPulse) and the amplitude cap in
    /// field_view.js. See docs/vr-handoff.md section 10 in the prism-live repo.
    ///
    /// The sound is the evidence; the visual pulse is secondary and gives way. It is unchanged up
    /// to 95 bpm, half at 107.5 and exactly zero from 120 bpm (2 Hz), never above 0.22 of the
    /// field's own light, and never more than 0.09 linear luminance.
    public static class PrismPulseSafety
    {
        public const double MaxAmplitude = 0.22;          // peak-to-peak, fraction of base luminance
        public const double MaxAbsoluteLuminance = 0.09;  // linear-light ceiling on any brightening
        public const double RiseMs = 90, FallMs = 180, DurationMs = RiseMs + FallMs;
        public const double MaxOnsetLatenessMs = 45;      // a frame-starved onset is dropped, not started late
        public const double MinIntervalMs = 60000.0 / 120.0;   // at or under this, visuals are silent
        public const int MaxQueue = 64;

        static bool Finite(double x) => !double.IsNaN(x) && !double.IsInfinity(x);
        static double Clamp01(double x) => Math.Max(0.0, Math.Min(1.0, x));

        public static bool ActiveSegment(string segment) =>
            segment == "baseline" || segment == "load" || segment == "regulate" || segment == "resolve";

        /// Full through 95 bpm, zero from 120 bpm. Unknown, zero or invalid heart rate gives zero.
        public static double VisualRateGain(double bpm) =>
            Finite(bpm) && bpm > 0 ? Clamp01((120.0 - bpm) / 25.0) : 0.0;

        /// The segment's authored 62..95 bpm amplitude, then the taper, applied once.
        /// remainingMs (segment_nominal_ms - segment_elapsed_ms) matters only in resolve, whose
        /// last three seconds fade to nothing.
        public static double PulseAmplitude(string segment, double bpm, double remainingMs)
        {
            if (!ActiveSegment(segment)) return 0.0;
            double gain = VisualRateGain(bpm);
            if (gain == 0.0) return 0.0;
            if (segment == "resolve") return 0.08 * gain * ResolveFade(remainingMs);
            double lo = segment == "baseline" ? 0.06 : segment == "load" ? 0.10 : 0.08;
            double hi = segment == "baseline" ? 0.10 : 0.16;
            return (lo + (hi - lo) * Clamp01((bpm - 62.0) / 33.0)) * gain;
        }

        /// Resolve's last three seconds fade to nothing; 1 before them, 0 at and after the end.
        public static double ResolveFade(double remainingMs) =>
            remainingMs > 0.0 ? Math.Cos(Math.PI / 2.0 * Clamp01((3000.0 - remainingMs) / 3000.0)) : 0.0;

        /// The share of a full-strength pulse the rules allow for one beat in one segment: the
        /// 95 to 120 bpm taper, times resolve's final fade. 0 outside the four running segments.
        /// The controller tap uses this, so it gives way exactly as the picture does.
        public static double Taper(string segment, double bpm, double remainingMs)
        {
            if (!ActiveSegment(segment)) return 0.0;
            return VisualRateGain(bpm) * (segment == "resolve" ? ResolveFade(remainingMs) : 1.0);
        }

        public const double MaxHapticStrength = 0.5;   // "gentle", whatever the Inspector says
        public const double MinHapticStrength = 0.02;  // under this nothing is sent at all

        /// The strength of one controller tap, 0 for none. A tap exists only at the start of a
        /// pulse the visual rules allowed (beatStarted), while really linked to the laptop,
        /// scaled by that pulse's taper. Nothing here can create a beat.
        public static double HapticAmplitude(bool beatStarted, bool linked, double taper, double strength)
        {
            if (!beatStarted || !linked || !Finite(taper) || !Finite(strength)) return 0.0;
            double amplitude = Math.Max(0.0, Math.Min(MaxHapticStrength, strength)) * Clamp01(taper);
            return amplitude >= MinHapticStrength ? amplitude : 0.0;
        }

        /// Why a pulse that started gave no tap, for the log; null when HapticAmplitude gives one.
        public static string HapticBlock(bool linked, double taper, double strength)
        {
            if (HapticAmplitude(true, linked, taper, strength) > 0.0) return null;
            if (!linked) return "not linked to the laptop";
            if (!Finite(strength) || Math.Min(MaxHapticStrength, strength) < MinHapticStrength) return "strength set to nothing";
            return "tapered away (heart rate toward 120 bpm, or the session's last fade)";
        }

        /// What may actually be shown. The state can be two seconds older than the beat on screen,
        /// so the amplitude is capped by BOTH the state's and the active beat's tapered amplitude:
        /// the smaller of the two, never their product. No active beat means no pulse.
        public static double DisplayAmplitude(double stateAmplitude, string segment, double activeBeatBpm,
                                              double remainingMs)
        {
            if (!Finite(stateAmplitude) || stateAmplitude <= 0.0) return 0.0;
            double beat = PulseAmplitude(segment, activeBeatBpm, remainingMs);
            return Math.Max(0.0, Math.Min(MaxAmplitude, Math.Min(stateAmplitude, beat)));
        }

        /// 90 ms raised-cosine rise, 180 ms fall, zero outside those 270 ms.
        public static double Envelope(double ageMs)
        {
            if (!Finite(ageMs) || ageMs <= 0.0 || ageMs >= DurationMs) return 0.0;
            if (ageMs < RiseMs) return 0.5 - 0.5 * Math.Cos(Math.PI * ageMs / RiseMs);
            return 0.5 + 0.5 * Math.Cos(Math.PI * (ageMs - RiseMs) / FallMs);
        }

        /// The one number a shader may brighten by: amplitude times envelope, at most 0.22.
        public static double PulseGain(double amplitude, double envelope)
        {
            if (!Finite(amplitude) || !Finite(envelope)) return 0.0;
            return Math.Max(0.0, Math.Min(MaxAmplitude, amplitude)) * Clamp01(envelope);
        }

        /// The brightening a local light may add to its own unpulsed luminance (linear light).
        /// PrismSpace.shader applies exactly this to its glow.
        public static double LuminanceIncrease(double restLuminance, double pulseGain)
        {
            if (!Finite(restLuminance) || restLuminance <= 0.0) return 0.0;
            double gain = Math.Max(0.0, Math.Min(MaxAmplitude, Finite(pulseGain) ? pulseGain : 0.0));
            return Math.Min(restLuminance * gain, MaxAbsoluteLuminance);
        }
    }

    /// The pulse half of the team's field_view.js: holds what the laptop last reported, refuses
    /// beats while frozen or outside a session, and gives the envelope and amplitude to draw.
    /// PrismField only passes values in and out, so these decisions can be tested without Unity.
    public sealed class PrismPulseView
    {
        readonly PrismBeatGate gate = new PrismBeatGate();
        string segment = "";
        double remainingMs, statePulse, stateBpm = double.NaN;
        double shownBeat = double.NaN;
        bool frozen;

        /// Heartbeat envelope now, 0..1.
        public double Envelope { get; private set; }

        /// The amplitude allowed on screen now, 0..0.22.
        public double Amplitude { get; private set; }

        /// Amplitude times envelope, at most 0.22: the only number a shader may brighten by.
        public double Gain => PrismPulseSafety.PulseGain(Amplitude, Envelope);

        /// 0..1: the taper of the beat on screen (95 to 120 bpm, resolve's final fade), and 0
        /// whenever no pulse is allowed on screen. For the controller tap.
        public double Taper { get; private set; }

        /// Goes up by one each time a pulse starts.
        public int BeatCount { get; private set; }

        public PrismBeatGate Gate => gate;

        /// Link lost: hold the picture as it is and drop every queued beat. Nothing queued
        /// before a freeze is ever replayed after it.
        public bool Frozen
        {
            get => frozen;
            set { frozen = value; if (value) gate.Clear(); }
        }

        /// What the laptop's latest state says. statePulse is that state's own amplitude,
        /// before any easing. A new session forgets the old one's beats; outside the four
        /// running segments nothing may flash.
        /// stateHeartRate is that state's hr_bpm; it only tapers the controller tap, which
        /// must give way to the state's rate as well as the beat's, as the amplitude does.
        public void OnState(string session, string stateSegment, double stateRemainingMs, double stateAmplitude,
                            double stateHeartRate = double.NaN)
        {
            if (session != gate.Session) gate.Reset(session);
            segment = stateSegment ?? "";
            remainingMs = stateRemainingMs;
            statePulse = stateAmplitude;
            stateBpm = stateHeartRate;
            if (!PrismPulseSafety.ActiveSegment(segment)) gate.Clear();
        }

        /// Offer one beat message. Times are local milliseconds.
        public bool OnBeat(string session, long seq, string quality, double rrMs, double hrBpm,
                           double localPlayMs, double nowMs)
        {
            if (frozen || !PrismPulseSafety.ActiveSegment(segment)) return false;
            return gate.Schedule(session, seq, quality, rrMs, hrBpm, localPlayMs, nowMs);
        }

        /// Once per frame. easedPulse is the amplitude being eased toward the latest state; it
        /// may lag a decrease, so it is capped by the state's own amplitude and then by the
        /// active beat's (the smaller wins, never their product).
        public void Tick(double nowMs, double easedPulse)
        {
            if (frozen) return;
            Envelope = gate.Value(nowMs);
            double start = gate.ActiveStart;
            if (!double.IsNaN(start) && start != shownBeat) BeatCount++;
            shownBeat = start;
            Amplitude = PrismPulseSafety.DisplayAmplitude(
                Math.Min(easedPulse, statePulse), segment, gate.ActiveRate, remainingMs);
            // The smaller of the beat's taper and the state's, never their product: the same
            // rule as the amplitude above. A state with no rate given tapers by the beat alone.
            double taper = PrismPulseSafety.Taper(segment, gate.ActiveRate, remainingMs);
            if (!double.IsNaN(stateBpm))
                taper = Math.Min(taper, PrismPulseSafety.Taper(segment, stateBpm, remainingMs));
            Taper = Amplitude > 0.0 ? taper : 0.0;
        }
    }

    /// The attendant's controller check: one tap, once per run of the app, when the right
    /// controller is first found, so it can be felt to work before a visitor puts the headset
    /// on. It is not a beat and must never be felt in a session, so it can fire in one place
    /// only: while handling a state message from a really linked laptop that says "idle" and
    /// was stamped by the laptop under half a second ago (a state held up by a WiFi stall may
    /// say "idle" after the session has begun). A state from any of the four running segments
    /// cancels the check for the rest of the run; it is never kept for later. Not linked,
    /// "reset", an old or undated state, or anything unrecognised waits and fires nothing.
    /// The laptop also says "idle" during the attendant's countdown to a session.
    public sealed class PrismTestTap
    {
        public enum Verdict { Nothing, Wait, Fire, Drop }

        public const double MaxStateAgeMs = 500.0;

        bool present, done;

        public bool Pending { get; private set; }

        /// Once per frame: is the right controller there now? True on the frame it appears.
        /// A controller that goes away before its check takes the check along, and may ask
        /// again; once the one check has fired or been cancelled, nothing asks again.
        public bool Seen(bool controllerPresent)
        {
            bool found = controllerPresent && !present;
            present = controllerPresent;
            Pending = controllerPresent && !done && (Pending || found);
            return found;
        }

        /// For each state message, as it arrives and never later. linked is PrismLink.IsLinked;
        /// stateAgeMs is how long ago the laptop stamped the state (NaN when not known).
        public Verdict OnState(bool linked, string segment, double stateAgeMs)
        {
            if (!Pending) return Verdict.Nothing;
            if (PrismPulseSafety.ActiveSegment(segment)) { Pending = false; done = true; return Verdict.Drop; }
            bool fresh = stateAgeMs >= -MaxStateAgeMs && stateAgeMs <= MaxStateAgeMs;   // false for NaN
            if (!linked || segment != "idle" || !fresh) return Verdict.Wait;
            Pending = false;
            done = true;
            return Verdict.Fire;
        }
    }

    /// Decides which scheduled beats may flash, and gives the one envelope on screen.
    /// No timer invents a beat. Times are milliseconds on the local clock.
    public sealed class PrismBeatGate
    {
        struct Planned { public double time, bpm; }

        readonly Queue<Planned> queue = new Queue<Planned>();
        string session;
        long lastSeq;
        bool haveActive, havePlanned;
        Planned active;
        double lastPlanned;

        public int Late { get; private set; }
        public int Rejected { get; private set; }
        public int Duplicate { get; private set; }
        public int RateLimited { get; private set; }
        public int Overflow { get; private set; }
        public int WrongSession { get; private set; }
        public int Queued => queue.Count;
        public string Session => session;

        /// A new session: forget everything, including the last sequence number.
        public void Reset(string newSession)
        {
            session = newSession;
            lastSeq = 0;
            Late = Rejected = Duplicate = RateLimited = Overflow = WrongSession = 0;
            Clear();
        }

        /// Link lost, reconnected or outside a session: nothing queued may play later.
        public void Clear()
        {
            queue.Clear();
            haveActive = false;
            havePlanned = false;
        }

        /// True if this beat will flash at localPlayMs. Every refusal is final: a suppressed
        /// beat is never replayed, and never suppresses the sound, which the laptop plays.
        public bool Schedule(string beatSession, long seq, string quality, double rrMs, double hrBpm,
                             double localPlayMs, double receivedAtMs)
        {
            if (string.IsNullOrEmpty(session) || beatSession != session) { WrongSession++; return false; }
            if (seq < 1 || !(rrMs > 0.0) || !(hrBpm > 0.0) || double.IsInfinity(rrMs) || double.IsInfinity(hrBpm) ||
                double.IsNaN(localPlayMs) || double.IsInfinity(localPlayMs) ||
                double.IsNaN(receivedAtMs) || double.IsInfinity(receivedAtMs) ||
                (quality != "ok" && quality != "interpolated" && quality != "rejected")) return false;
            if (seq <= lastSeq) { Duplicate++; return false; }
            lastSeq = seq;
            if (quality == "rejected") { Rejected++; return false; }
            if (localPlayMs < receivedAtMs) { Late++; return false; }
            double interval = havePlanned ? localPlayMs - lastPlanned : double.PositiveInfinity;
            if (interval <= 0.0) { RateLimited++; return false; }
            // Keep EVERY eligible future candidate, even one suppressed below. Measuring from the
            // last visible beat instead would invent a slower, every-other-beat flash at high rates.
            lastPlanned = localPlayMs;
            havePlanned = true;
            double bpm = Math.Max(hrBpm, Math.Max(60000.0 / rrMs, 60000.0 / interval));
            if (bpm >= 120.0 || interval <= PrismPulseSafety.MinIntervalMs) { RateLimited++; return false; }
            if (queue.Count >= PrismPulseSafety.MaxQueue) { Overflow++; return false; }
            queue.Enqueue(new Planned { time = localPlayMs, bpm = bpm });
            return true;
        }

        /// The envelope now, 0..1. One bounded envelope; beats never add.
        public double Value(double nowMs)
        {
            if (double.IsNaN(nowMs) || double.IsInfinity(nowMs)) return 0.0;
            while (queue.Count > 0 && queue.Peek().time <= nowMs)
            {
                Planned beat = queue.Dequeue();
                if (nowMs - beat.time > PrismPulseSafety.MaxOnsetLatenessMs) { Late++; continue; }
                active = beat;
                haveActive = true;
            }
            if (!haveActive) return 0.0;
            double age = nowMs - active.time;
            if (age >= PrismPulseSafety.DurationMs) { haveActive = false; return 0.0; }
            return PrismPulseSafety.Envelope(age);
        }

        /// Rate of the beat on screen (the highest of its three rate readings), or NaN for none.
        public double ActiveRate => haveActive ? active.bpm : double.NaN;

        /// Start time of the beat on screen, or NaN for none.
        public double ActiveStart => haveActive ? active.time : double.NaN;
    }
}
