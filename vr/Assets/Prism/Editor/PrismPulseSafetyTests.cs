using System;
using System.IO;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.Rendering;

namespace Prism.Tests
{
    /// The flashing-light rules, tested as the web version tests them: every whole heart rate from
    /// 45 to 180 bpm, in all four segments. Run from Window > General > Test Runner > EditMode.
    /// The same code was also compared, value for value, with web/shared/field_mapping.js and
    /// field_pulse.js outside Unity; these tests pin the rules themselves.
    public class PrismPulseSafetyTests
    {
        const string Session = "S-20261008-0001";
        static readonly string[] Segments = { "baseline", "load", "regulate", "resolve" };

        static double Authored(string segment, double bpm)
        {
            double rate = Math.Max(0.0, Math.Min(1.0, (bpm - 62.0) / 33.0));
            switch (segment)
            {
                case "baseline": return 0.06 + 0.04 * rate;
                case "load": return 0.10 + 0.06 * rate;
                case "regulate": return 0.08 + 0.08 * rate;
                default: return 0.08;
            }
        }

        // ---- the taper ----

        [Test]
        public void AmplitudeIsAuthoredThrough95ThenTapersToZeroAt120()
        {
            foreach (string segment in Segments)
                for (int bpm = 45; bpm <= 180; bpm++)
                {
                    double a = PrismPulseSafety.PulseAmplitude(segment, bpm, 45000);
                    string at = segment + " at " + bpm + " bpm";
                    Assert.LessOrEqual(a, PrismPulseSafety.MaxAmplitude, at);
                    Assert.GreaterOrEqual(a, 0.0, at);
                    if (bpm <= 95) Assert.AreEqual(Authored(segment, bpm), a, 1e-12, at);
                    else if (bpm < 120)
                    {
                        Assert.AreEqual(Authored(segment, bpm) * (120 - bpm) / 25.0, a, 1e-12, at);
                        Assert.Less(a, PrismPulseSafety.PulseAmplitude(segment, bpm - 1, 45000), at);
                    }
                    else Assert.AreEqual(0.0, a, at);          // exactly zero from 2 Hz upward
                }
        }

        [Test]
        public void TaperIsHalfAt107Point5()
        {
            Assert.AreEqual(1.0, PrismPulseSafety.VisualRateGain(95), 0.0);
            Assert.AreEqual(0.5, PrismPulseSafety.VisualRateGain(107.5), 1e-15);
            Assert.AreEqual(0.0, PrismPulseSafety.VisualRateGain(120), 0.0);
            foreach (string segment in Segments)
                Assert.AreEqual(Authored(segment, 95) / 2.0,
                    PrismPulseSafety.PulseAmplitude(segment, 107.5, 45000), 1e-12, segment);
        }

        [Test]
        public void UnknownHeartRateAndIdleNeverPulse()
        {
            foreach (double bpm in new[] { 0.0, -1.0, double.NaN, double.PositiveInfinity, double.NegativeInfinity })
                foreach (string segment in Segments)
                    Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude(segment, bpm, 45000), segment + " " + bpm);
            foreach (string segment in new[] { "idle", "reset", "", null, "unknown" })
                Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude(segment, 70, 45000));
        }

        [Test]
        public void ResolveFadesOverItsLastThreeSecondsOnTopOfTheTaper()
        {
            for (int bpm = 45; bpm <= 180; bpm++)
            {
                double gain = PrismPulseSafety.VisualRateGain(bpm);
                Assert.AreEqual(0.08 * gain, PrismPulseSafety.PulseAmplitude("resolve", bpm, 3000), 1e-12);
                Assert.AreEqual(0.08 * gain * Math.Cos(Math.PI / 4.0),
                    PrismPulseSafety.PulseAmplitude("resolve", bpm, 1500), 1e-12);
                Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude("resolve", bpm, 0), 0.0);
                Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude("resolve", bpm, -200), 0.0);
                Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude("resolve", bpm, double.NaN), 0.0);
            }
        }

        // ---- the envelope ----

        [Test]
        public void EnvelopeRisesOver90MsFallsOver180AndIsZeroOutside()
        {
            Assert.AreEqual(0.0, PrismPulseSafety.Envelope(0));
            Assert.AreEqual(0.0, PrismPulseSafety.Envelope(-5));
            Assert.AreEqual(0.0, PrismPulseSafety.Envelope(270));
            Assert.AreEqual(0.0, PrismPulseSafety.Envelope(double.NaN));
            Assert.AreEqual(0.5, PrismPulseSafety.Envelope(45), 1e-12);
            Assert.AreEqual(1.0, PrismPulseSafety.Envelope(90), 1e-12);
            Assert.AreEqual(0.5, PrismPulseSafety.Envelope(180), 1e-12);
            double previous = 0.0;
            for (double age = 0.5; age <= 90; age += 0.5)
            {
                double v = PrismPulseSafety.Envelope(age);
                Assert.Greater(v, previous, "rise at " + age);
                if (age < 90) Assert.Less(v, 1.0, "no full brightness before 90 ms, at " + age);
                previous = v;
            }
            for (double age = 90.5; age < 270; age += 0.5)
            {
                double v = PrismPulseSafety.Envelope(age);
                Assert.Less(v, previous, "fall at " + age);
                previous = v;
            }
        }

        // ---- which beats may flash ----

        /// Feed a steady train at one rate, 400 ms ahead as the laptop does, and count what shows.
        static void RunTrain(int bpm, out int flashes, out double peakGain, out double peakEnvelope)
        {
            peakEnvelope = 0;
            var gate = new PrismBeatGate();
            gate.Reset(Session);
            double rr = 60000.0 / bpm, play = 1400;
            int seq = 1;
            double shown = double.NaN;
            flashes = 0; peakGain = 0;
            for (double now = 1000; now < 21000; now += 1)
            {
                for (; play - 400 <= now; play += rr) gate.Schedule(Session, seq++, "ok", rr, bpm, play, now);
                double envelope = gate.Value(now);
                Assert.LessOrEqual(envelope, 1.0, "envelopes never add, at " + bpm + " bpm");
                peakEnvelope = Math.Max(peakEnvelope, envelope);
                // 90 ms rise: within 45 ms of its onset no beat is past half brightness
                if (!double.IsNaN(gate.ActiveStart) && now - gate.ActiveStart <= 45)
                    Assert.LessOrEqual(envelope, 0.5 + 1e-12, "rise too fast at " + bpm + " bpm");
                double start = gate.ActiveStart;
                if (!double.IsNaN(start) && start != shown) flashes++;
                shown = start;
                foreach (string segment in Segments)
                {
                    double amplitude = PrismPulseSafety.DisplayAmplitude(
                        PrismPulseSafety.PulseAmplitude(segment, bpm, 45000), segment, gate.ActiveRate, 45000);
                    peakGain = Math.Max(peakGain, PrismPulseSafety.PulseGain(amplitude, envelope));
                }
            }
        }

        [Test]
        public void EveryRateFrom45To180FlashesOncePerBeatOrNotAtAll()
        {
            for (int bpm = 45; bpm <= 180; bpm++)
            {
                RunTrain(bpm, out int flashes, out double peakGain, out double peakEnvelope);
                if (bpm >= 120)
                {
                    Assert.AreEqual(0, flashes, bpm + " bpm must not flash: no every-other-beat substitute");
                    Assert.AreEqual(0.0, peakGain, bpm + " bpm");
                    Assert.AreEqual(0.0, peakEnvelope, bpm + " bpm");
                }
                else
                {
                    int expected = (int)Math.Floor(20.0 * bpm / 60.0);
                    Assert.That(flashes, Is.InRange(expected - 2, expected + 1), bpm + " bpm");
                    // load has the largest authored amplitude at every rate
                    double most = Authored("load", bpm) * PrismPulseSafety.VisualRateGain(bpm);
                    Assert.LessOrEqual(peakGain, most + 1e-12, bpm + " bpm");
                    // and the pulse is really there: full envelope, full allowed amplitude
                    Assert.Greater(peakEnvelope, 0.999, bpm + " bpm");
                    Assert.Greater(peakGain, 0.999 * most, bpm + " bpm");
                    Assert.LessOrEqual(peakGain, PrismPulseSafety.MaxAmplitude, bpm + " bpm");
                }
            }
        }

        [Test]
        public void TheFastestOfABeatsThreeRateReadingsDecides()
        {
            // heart rate field says slow, the interval it closed says fast
            var gate = new PrismBeatGate(); gate.Reset(Session);
            Assert.IsFalse(gate.Schedule(Session, 1, "ok", 400, 60, 2000, 1000));
            // both fields say slow, but it lands 450 ms after the previous candidate
            gate.Reset(Session);
            Assert.IsTrue(gate.Schedule(Session, 1, "ok", 1000, 60, 2000, 1000));
            Assert.IsFalse(gate.Schedule(Session, 2, "ok", 1000, 60, 2450, 1000));
            // exactly 500 ms and exactly 120 bpm are both refused
            gate.Reset(Session);
            Assert.IsTrue(gate.Schedule(Session, 1, "ok", 1000, 60, 2000, 1000));
            Assert.IsFalse(gate.Schedule(Session, 2, "ok", 1000, 60, 2500, 1000));
            gate.Reset(Session);
            Assert.IsFalse(gate.Schedule(Session, 1, "ok", 500, 120, 2000, 1000));
            Assert.IsFalse(gate.Schedule(Session, 2, "ok", 1000, 120, 4000, 1000));
            // the reading kept with the beat is the fastest of the three
            gate.Reset(Session);
            Assert.IsTrue(gate.Schedule(Session, 1, "ok", 600, 70, 2000, 1000));
            gate.Value(2010);
            Assert.AreEqual(100.0, gate.ActiveRate, 1e-9);
        }

        [Test]
        public void ASuppressedBeatStillCountsAsThePreviousOnset()
        {
            // 150 bpm: measuring from the last VISIBLE beat would show every other one at 75 bpm.
            var gate = new PrismBeatGate(); gate.Reset(Session);
            int shown = 0;
            for (int i = 0; i < 40; i++)
                if (gate.Schedule(Session, i + 1, "ok", 1000, 60, 2000 + i * 400, 1000)) shown++;
            Assert.AreEqual(1, shown, "only the first, which has no previous onset, may show");
        }

        [Test]
        public void LateRejectedDuplicateAndWrongSessionBeatsNeverFlash()
        {
            var gate = new PrismBeatGate(); gate.Reset(Session);
            Assert.IsFalse(gate.Schedule(Session, 1, "ok", 900, 66, 900, 1000), "already late");
            Assert.IsFalse(gate.Schedule(Session, 2, "rejected", 900, 66, 3000, 1000), "rejected");
            Assert.IsTrue(gate.Schedule(Session, 3, "interpolated", 900, 66, 3000, 1000));
            Assert.IsFalse(gate.Schedule(Session, 3, "ok", 900, 66, 4000, 1000), "duplicate");
            Assert.IsFalse(gate.Schedule(Session, 2, "ok", 900, 66, 4000, 1000), "out of order");
            Assert.IsFalse(gate.Schedule("S-20261008-0002", 4, "ok", 900, 66, 4000, 1000), "wrong session");
            Assert.IsFalse(gate.Schedule(Session, 4, "ok", 0, 66, 4000, 1000), "no interval");
            Assert.IsFalse(gate.Schedule(Session, 5, "ok", 900, 0, 4000, 1000), "no rate");
            Assert.IsFalse(gate.Schedule(Session, 6, "nonsense", 900, 66, 4000, 1000), "unknown quality");
            Assert.AreEqual(1, gate.Queued);
            Assert.IsFalse(new PrismBeatGate().Schedule(Session, 1, "ok", 900, 66, 3000, 1000), "no session yet");

            // an onset first seen more than 45 ms late is dropped, never started late
            gate.Reset(Session);
            Assert.IsTrue(gate.Schedule(Session, 1, "ok", 900, 66, 3000, 1000));
            Assert.AreEqual(0.0, gate.Value(3046));
            Assert.AreEqual(0.0, gate.Value(3100));
            // a cleared gate (link lost, outside a session) plays nothing that was queued
            Assert.IsTrue(gate.Schedule(Session, 2, "ok", 900, 66, 5000, 4000));
            gate.Clear();
            Assert.AreEqual(0.0, gate.Value(5010));
        }

        [Test]
        public void ANewSessionDropsQueuedBeatsAndASlowBeatAfterAFastRunFlashesOnce()
        {
            var gate = new PrismBeatGate(); gate.Reset(Session);
            Assert.IsTrue(gate.Schedule(Session, 1, "ok", 900, 66, 3000, 1000));
            gate.Reset("S-20261008-0002");
            Assert.AreEqual(0.0, gate.Value(3010), "the old session's beat must not play in the new one");
            Assert.IsTrue(gate.Schedule("S-20261008-0002", 1, "ok", 900, 66, 5000, 3100), "sequence restarts");

            // 150 bpm for ten seconds, then the heart slows to 60: nothing is replayed, and the
            // first slow beat a full second after the last fast one flashes exactly once
            gate.Reset(Session);
            int seq = 1, shown = 0;
            double play = 2000;
            for (; play < 12000; play += 400)
                if (gate.Schedule(Session, seq++, "ok", 400, 150, play, play - 400) && play > 2000) shown++;
            Assert.AreEqual(0, shown);
            double slow = play - 400 + 1000;
            Assert.IsTrue(gate.Schedule(Session, seq++, "ok", 1000, 60, slow, slow - 400));
            int starts = 0; double last = double.NaN;
            for (double now = slow - 300; now < slow + 900; now += 1)
            {
                gate.Value(now);
                if (!double.IsNaN(gate.ActiveStart) && gate.ActiveStart != last) starts++;
                last = gate.ActiveStart;
            }
            Assert.AreEqual(1, starts);
        }

        // ---- the view: what PrismField actually draws from ----

        /// Frames every 5 ms, as a display would: a single late look would drop the onset.
        static void Advance(PrismPulseView view, double from, double to, double eased)
        {
            for (double now = from; now <= to; now += 5) view.Tick(now, eased);
        }

        static PrismPulseView LoadView(double stateBpm)
        {
            var view = new PrismPulseView();
            view.OnState(Session, "load", 60000, PrismPulseSafety.PulseAmplitude("load", stateBpm, 60000));
            return view;
        }

        [Test]
        public void ViewShowsABeatAtItsScheduledTimeAndNotBefore()
        {
            PrismPulseView view = LoadView(70);
            double expected = PrismPulseSafety.PulseAmplitude("load", 70, 60000);
            Assert.IsTrue(view.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            view.Tick(1990, expected);
            Assert.AreEqual(0.0, view.Gain, "nothing before t_play");
            Advance(view, 1995, 2090, expected);
            Assert.AreEqual(expected, view.Gain, 1e-12, "full amplitude at the top of the rise");
            Assert.AreEqual(1, view.BeatCount);
            view.Tick(2400, expected);
            Assert.AreEqual(0.0, view.Gain);
        }

        [Test]
        public void ViewNeverPulsesOutsideASessionOrWhileFrozen()
        {
            foreach (string segment in new[] { "idle", "reset", "", null })
            {
                var view = new PrismPulseView();
                view.OnState(Session, segment, 0, 0.16);
                Assert.IsFalse(view.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600), "segment " + segment);
                Advance(view, 1995, 2090, 0.16);
                Assert.AreEqual(0.0, view.Gain);
            }
            Assert.IsFalse(new PrismPulseView().OnBeat(Session, 1, "ok", 857, 70, 2000, 1600), "before any state");

            // a beat queued in load is dropped the moment the laptop reports reset
            PrismPulseView running = LoadView(70);
            Assert.IsTrue(running.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            running.OnState(Session, "reset", 3000, 0);
            Advance(running, 1995, 2090, 0.16);
            Assert.AreEqual(0.0, running.Gain);

            // frozen: queued beats are dropped, new ones refused, and nothing queued before
            // the freeze plays after a fresh state ends it
            PrismPulseView frozen = LoadView(70);
            Assert.IsTrue(frozen.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            frozen.Frozen = true;
            Assert.IsFalse(frozen.OnBeat(Session, 2, "ok", 857, 70, 2857, 1700));
            frozen.OnState(Session, "load", 58000, 0.11);
            frozen.Frozen = false;
            Advance(frozen, 1995, 2090, 0.11);
            Assert.AreEqual(0.0, frozen.Gain);
            Assert.AreEqual(0, frozen.BeatCount);
        }

        [Test]
        public void ViewCapsAStaleSlowStateByAFastBeatAndAnEasedAmplitudeByTheLatestState()
        {
            // state two seconds old says 70 bpm; the beat on screen is at 110, then one at 130
            PrismPulseView view = LoadView(70);
            double slow = PrismPulseSafety.PulseAmplitude("load", 70, 60000);
            Assert.IsTrue(view.OnBeat(Session, 1, "ok", 60000.0 / 110, 110, 2000, 1600));
            Advance(view, 1995, 2090, slow);
            Assert.AreEqual(PrismPulseSafety.PulseAmplitude("load", 110, 60000), view.Amplitude, 1e-9);
            Assert.Less(view.Amplitude, slow);
            Assert.IsFalse(view.OnBeat(Session, 2, "ok", 461, 130, 2461, 2100), "130 bpm never flashes");

            // the eased amplitude is still high, but the newest state says unknown heart rate
            // (0), a rate past 120 (0) or less than before: the drop is immediate, not eased
            foreach (double latest in new[] { 0.0, 0.05 })
            {
                PrismPulseView dropping = LoadView(90);
                double before = PrismPulseSafety.PulseAmplitude("load", 90, 60000);
                Assert.IsTrue(dropping.OnBeat(Session, 1, "ok", 667, 90, 2000, 1600));
                dropping.OnState(Session, "load", 58000, latest);
                Advance(dropping, 1995, 2090, before);
                Assert.AreEqual(latest, dropping.Amplitude, 1e-12);
            }

            // a state in resolve's last moment: nothing left to show even with a beat on screen
            var ending = new PrismPulseView();
            ending.OnState(Session, "resolve", 5000, PrismPulseSafety.PulseAmplitude("resolve", 70, 5000));
            Assert.IsTrue(ending.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            ending.OnState(Session, "resolve", 0, PrismPulseSafety.PulseAmplitude("resolve", 70, 0));
            Advance(ending, 1995, 2090, 0.08);
            Assert.AreEqual(0.0, ending.Gain);

            // every state rate against every beat rate, through the view
            for (int stateBpm = 45; stateBpm <= 180; stateBpm += 3)
                for (int beatBpm = 45; beatBpm <= 180; beatBpm += 3)
                {
                    PrismPulseView each = LoadView(stateBpm);
                    double state = PrismPulseSafety.PulseAmplitude("load", stateBpm, 60000);
                    bool shown = each.OnBeat(Session, 1, "ok", 60000.0 / beatBpm, beatBpm, 2000, 1600);
                    Advance(each, 1995, 2090, 0.22);            // an easing value that is far too high
                    Assert.AreEqual(beatBpm < 120, shown);
                    double expected = shown ? Math.Min(state, PrismPulseSafety.PulseAmplitude("load", beatBpm, 60000)) : 0.0;
                    Assert.AreEqual(expected, each.Gain, 1e-12, stateBpm + " / " + beatBpm);
                }
        }

        // ---- a stale state cannot keep a fast beat bright ----

        [Test]
        public void DisplayedAmplitudeIsTheSmallerOfStateAndActiveBeatAtEveryRate()
        {
            foreach (string segment in Segments)
                for (int stateBpm = 45; stateBpm <= 180; stateBpm++)
                {
                    double state = PrismPulseSafety.PulseAmplitude(segment, stateBpm, 45000);
                    for (int beatBpm = 45; beatBpm <= 180; beatBpm += 5)
                    {
                        double beat = PrismPulseSafety.PulseAmplitude(segment, beatBpm, 45000);
                        double shown = PrismPulseSafety.DisplayAmplitude(state, segment, beatBpm, 45000);
                        Assert.AreEqual(Math.Min(state, beat), shown, 1e-15);       // min, not product
                        if (beatBpm >= 120 || stateBpm >= 120) Assert.AreEqual(0.0, shown);
                    }
                    Assert.AreEqual(0.0, PrismPulseSafety.DisplayAmplitude(state, segment, double.NaN, 45000),
                        "no active beat, no pulse");
                }
            // two-second-old state at 70 bpm, beat arriving at 110: the beat's taper wins
            double slowState = PrismPulseSafety.PulseAmplitude("load", 70, 45000);
            Assert.AreEqual(PrismPulseSafety.PulseAmplitude("load", 110, 45000),
                PrismPulseSafety.DisplayAmplitude(slowState, "load", 110, 45000), 1e-15);
            Assert.AreEqual(0.0, PrismPulseSafety.DisplayAmplitude(slowState, "load", 130, 45000));
            // a wild state amplitude can never pass the hard rail
            Assert.LessOrEqual(PrismPulseSafety.DisplayAmplitude(5.0, "regulate", 95, 45000), 0.16 + 1e-15);
            Assert.AreEqual(0.0, PrismPulseSafety.DisplayAmplitude(double.NaN, "regulate", 95, 45000));
            Assert.AreEqual(0.22, PrismPulseSafety.PulseGain(9.0, 7.0), 1e-15);
            Assert.AreEqual(0.0, PrismPulseSafety.PulseGain(double.NaN, 1.0));
        }

        [Test]
        public void LuminanceIncreaseHonoursBothCaps()
        {
            for (double rest = 0.0; rest <= 3.0; rest += 0.01)
                for (double gain = -0.1; gain <= 0.5; gain += 0.01)
                {
                    double add = PrismPulseSafety.LuminanceIncrease(rest, gain);
                    Assert.GreaterOrEqual(add, 0.0);
                    Assert.LessOrEqual(add, 0.22 * rest + 1e-15);
                    Assert.LessOrEqual(add, 0.09);
                }
        }

        // ---- the shaders, measured on rendered pixels ----

        static float Lum(Color c) => 0.2126f * c.r + 0.7152f * c.g + 0.0722f * c.b;

        /// Draw a sky material from the inside of a cube into a linear float texture. No camera,
        /// scene or render pipeline is involved, so nothing but the shader shapes the pixels.
        static Color[] Render(Material material, Vector3 look)
        {
            const int size = 128;
            var target = new RenderTexture(size, size, 0, RenderTextureFormat.ARGBFloat, RenderTextureReadWrite.Linear);
            target.Create();
            Vector3 up = Mathf.Abs(look.y) > 0.9f ? Vector3.back : Vector3.up;
            Matrix4x4 view = Matrix4x4.Scale(new Vector3(1f, 1f, -1f)) *
                Matrix4x4.TRS(Vector3.zero, Quaternion.LookRotation(look, up), Vector3.one).inverse;
            var commands = new CommandBuffer();
            commands.SetRenderTarget(target);
            commands.ClearRenderTarget(true, true, Color.clear);
            commands.SetViewProjectionMatrices(view, Matrix4x4.Perspective(90f, 1f, 0.01f, 100f));
            commands.DrawMesh(Resources.GetBuiltinResource<Mesh>("Cube.fbx"), Matrix4x4.identity, material, 0, 0);
            Graphics.ExecuteCommandBuffer(commands);
            commands.Release();

            RenderTexture previous = RenderTexture.active;
            RenderTexture.active = target;
            var texture = new Texture2D(size, size, TextureFormat.RGBAFloat, false, true);
            texture.ReadPixels(new Rect(0, 0, size, size), 0, 0);
            texture.Apply();
            RenderTexture.active = previous;
            Color[] pixels = texture.GetPixels();
            UnityEngine.Object.DestroyImmediate(texture);
            target.Release();
            UnityEngine.Object.DestroyImmediate(target);
            return pixels;
        }

        static Material Load(string shaderName)
        {
            Shader shader = Shader.Find(shaderName);
            Assert.IsNotNull(shader, shaderName + " not found");
            Assert.IsTrue(shader.isSupported, shaderName + " does not compile on this machine");
            return new Material(shader);
        }

        /// Returns the largest brightening and how many pixels did not change at all.
        static void Compare(Color[] rest, Color[] pulsed, double relative, double tolerance, string what,
                            out double largest, out int unchanged)
        {
            largest = 0; unchanged = 0;
            double brightest = 0;
            for (int i = 0; i < rest.Length; i++)
            {
                double r = Lum(rest[i]), p = Lum(pulsed[i]), d = p - r;
                brightest = Math.Max(brightest, r);
                Assert.GreaterOrEqual(d, -tolerance, what + ": a pulse never darkens, pixel " + i);
                Assert.LessOrEqual(d, relative * r + tolerance, what + ": over the relative rail, pixel " + i);
                Assert.LessOrEqual(d, PrismPulseSafety.MaxAbsoluteLuminance + tolerance,
                    what + ": over 0.09 linear luminance, pixel " + i);
                if (rest[i] == pulsed[i]) unchanged++;
                largest = Math.Max(largest, d);
            }
            Assert.Greater(brightest, 0.001, what + ": the render is blank, so it proves nothing");
        }

        [Test]
        public void SpaceGlowPulseStaysUnderBothCapsAndLeavesTheRestOfTheSkyAlone()
        {
            Material space = Load("Prism/Space");
            try
            {
                space.SetVector("_Tint", Vector4.one);                   // white: the brightest tint
                // brightness 1 is the most PrismWarp sets; 3 is only here to drive the 0.09 cap
                foreach (float bright in new[] { 0.2f, 1f, 3f })
                {
                    space.SetFloat("_Bright", bright);
                    space.SetFloat("_PulseGain", 0f);
                    Color[] restAhead = Render(space, Vector3.forward), restBehind = Render(space, Vector3.back);
                    // 5 stands for a caller that forgot the rail: the shader must clamp it to 0.22
                    foreach (float gain in new[] { 0.08f, 0.16f, 0.22f, 5f })
                    {
                        space.SetFloat("_PulseGain", gain);
                        string what = "Space, brightness " + bright + ", gain " + gain;
                        Compare(restAhead, Render(space, Vector3.forward), Math.Min(gain, 0.22), 1e-4, what,
                            out double largest, out int _);
                        Assert.Greater(largest, 0.001, what + ": no visible pulse, so the test proves nothing");
                        if (bright >= 3f && gain >= 0.22f)
                            Assert.Greater(largest, 0.085, what + ": the absolute cap was not reached");
                        Compare(restBehind, Render(space, Vector3.back), 0.0, 0.0, what + ", behind",
                            out double _, out int unchanged);
                        Assert.AreEqual(restBehind.Length, unchanged, what + ": the sky behind must not change");
                    }
                }
            }
            finally { UnityEngine.Object.DestroyImmediate(space); }
        }

        static void SetSky(Material sky, Vector4 a0, Vector4 b0, Vector4 a1, Vector4 b1, float envelope)
        {
            sky.SetVector("_A0", a0); sky.SetVector("_B0", b0);
            sky.SetVector("_A1", a1); sky.SetVector("_B1", b1);
            sky.SetFloat("_Env", envelope);
            sky.SetFloat("_DriftPx", 0f);
            sky.SetVector("_Size", new Vector4(1909.93f, 1080f, 0f, 0f));
            sky.SetVector("_Fov", new Vector4(100f, 90f, 0f, 0f));
        }

        [Test]
        public void FogFieldPulseStaysUnderBothCapsAndIsNotFullField()
        {
            Material sky = Load("Prism/Sky");
            try
            {
                // fog, light, hue, sat / horizon, motion: one palette per segment, brightest ends
                var palettes = new (Vector4 a, float horizon, float motion)[]
                {
                    (new Vector4(.26f, .62f, 208f, .12f), .50f, .008f),    // baseline
                    (new Vector4(.20f, 1.05f, 202f, .30f), .50f, .010f),   // load
                    (new Vector4(.34f, .95f, 46f, .30f), .56f, .030f),     // regulate
                    (new Vector4(.44f, .50f, 30f, .14f), .58f, .006f),     // resolve
                };
                // 5 stands for a caller that forgot the rail: the shader must hold 0.22 by itself
                foreach (float pulse in new[] { 0.08f, 0.16f, 0.22f, 5f })
                    for (int i = 0; i < palettes.Length; i++)
                    {
                        // whole palettes, and the middle of a dissolve from the previous segment
                        foreach (float blend in new[] { 1f, 0.5f })
                        {
                            var from = palettes[(i + palettes.Length - 1) % palettes.Length];
                            var to = palettes[i];
                            Vector4 b0 = new Vector4(from.horizon, pulse, from.motion, 1f - blend);
                            Vector4 b1 = new Vector4(to.horizon, pulse, to.motion, blend);
                            string what = "Sky palette " + i + ", pulse " + pulse + ", blend " + blend;
                            foreach (Vector3 look in new[] { Vector3.forward, Vector3.left, Vector3.back, Vector3.up, Vector3.down })
                            {
                                SetSky(sky, from.a, b0, to.a, b1, 0f);
                                Color[] rest = Render(sky, look);
                                double largest = 0; int unchanged = 0;
                                foreach (float envelope in new[] { 0.25f, 0.5f, 1f })
                                {
                                    SetSky(sky, from.a, b0, to.a, b1, envelope);
                                    // The shader rounds its output DOWN to the 8-bit grid, so only
                                    // float error is allowed here. The rail is checked against the
                                    // unpulsed pixel; the shader itself budgets against the darker
                                    // base gradient, which this test cannot see.
                                    Compare(rest, Render(sky, look), Math.Min(pulse, 0.22), 1e-3, what, out largest, out unchanged);
                                }
                                if (look == Vector3.forward)
                                    Assert.Greater(largest, 0.004, what + ": no visible pulse, so the test proves nothing");
                                if (look == Vector3.up || look == Vector3.down)
                                    Assert.Greater(unchanged, rest.Length / 4, what + ": straight up and down must mostly stay still");
                            }
                        }
                    }
            }
            finally { UnityEngine.Object.DestroyImmediate(sky); }
        }

        [Test]
        public void StreaksCarryNoHeartbeatAndOnlyTheSpaceGlowDoes()
        {
            string folder = Path.Combine(Application.dataPath, "Prism");
            string warp = File.ReadAllText(Path.Combine(folder, "PrismWarp.shader"));
            string space = File.ReadAllText(Path.Combine(folder, "PrismSpace.shader"));
            string driver = File.ReadAllText(Path.Combine(folder, "PrismWarp.cs"));
            foreach (string input in new[] { "_Env", "_Pulse", "PulseGain" })
                StringAssert.DoesNotContain(input, warp, "the streak shader must have no heartbeat input");
            StringAssert.DoesNotContain("_Env", space);
            StringAssert.Contains("min(glowL * gain, 0.09)", space);
            StringAssert.Contains("clamp(_PulseGain, 0.0, 0.22)", space);
            StringAssert.DoesNotContain("warpMat.SetFloat(\"_Pulse", driver);
            StringAssert.DoesNotContain("warpMat.SetFloat(\"_Env", driver);
            StringAssert.DoesNotContain(".pulse", driver, "use PrismField.PulseGain, never the raw token");
            // the driver touches the heartbeat in exactly one place: the space glow's gain
            foreach (string beat in new[] { "PulseGain", "PulseEnvelope", "PulseAmplitude", "BeatCount" })
            {
                int uses = 0;
                foreach (string line in driver.Split('\n'))
                    if (line.Contains("field." + beat))
                    {
                        uses++;
                        StringAssert.Contains("spaceMat.SetFloat(\"_PulseGain\", field.PulseGain)", line);
                    }
                Assert.AreEqual(beat == "PulseGain" ? 1 : 0, uses, beat);
            }
        }

        [Test]
        public void ProjectRendersInLinearColourSpace()
        {
            // every luminance rule above is stated in linear light; the shaders assume it
            Assert.AreEqual(UnityEngine.ColorSpace.Linear, UnityEditor.PlayerSettings.colorSpace);
        }
    }
}
