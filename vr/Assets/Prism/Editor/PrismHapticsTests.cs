using System.IO;
using NUnit.Framework;
using UnityEngine;

namespace Prism.Tests
{
    /// The controller tap: only at the start of a pulse the visual rules allowed, only while
    /// really linked to the laptop, right controller only, tapered like the picture.
    public class PrismHapticsTests
    {
        const string Session = "S-20261010-0001";

        static PrismPulseView View(string segment, double stateBpm, double remainingMs = 60000)
        {
            var view = new PrismPulseView();
            view.OnState(Session, segment, remainingMs, PrismPulseSafety.PulseAmplitude(segment, stateBpm, remainingMs));
            return view;
        }

        /// Ticks every 5 ms and returns the strongest tap offered, counting pulse starts.
        static double Run(PrismPulseView view, double from, double to, bool linked, out int taps, double strength = 0.3)
        {
            double strongest = 0.0;
            taps = 0;
            int last = view.BeatCount;
            for (double now = from; now <= to; now += 5)
            {
                view.Tick(now, 0.16);
                bool started = view.BeatCount != last;
                last = view.BeatCount;
                double amp = PrismPulseSafety.HapticAmplitude(started, linked, view.Taper, strength);
                if (amp > 0.0) taps++;
                strongest = System.Math.Max(strongest, amp);
            }
            return strongest;
        }

        [Test]
        public void OneTapAtTheBeatsOwnMomentAndNoneBeforeOrAfter()
        {
            PrismPulseView view = View("load", 70);
            Assert.IsTrue(view.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            Run(view, 1600, 1995, true, out int early);
            Assert.AreEqual(0, early, "tapped before t_play");
            double amp = Run(view, 2000, 2004, true, out int taps);
            Assert.AreEqual(1, taps);
            Assert.AreEqual(0.3, amp, 1e-9);
            Run(view, 2005, 3000, true, out int late);
            Assert.AreEqual(0, late, "one beat, one tap: nothing repeats it");
        }

        [Test]
        public void TheTapTapersExactlyAsThePictureDoes()
        {
            for (int bpm = 45; bpm <= 180; bpm++)
            {
                PrismPulseView view = View("regulate", bpm);
                double rr = 60000.0 / bpm;
                bool scheduled = view.OnBeat(Session, 1, "ok", rr, bpm, 2000, 1600);
                double amp = Run(view, 1990, 2010, true, out int taps);
                double expected = 0.3 * PrismPulseSafety.VisualRateGain(bpm);
                if (expected < PrismPulseSafety.MinHapticStrength) expected = 0.0;
                Assert.AreEqual(expected, amp, 1e-9, bpm + " bpm");
                Assert.AreEqual(expected > 0 ? 1 : 0, taps, bpm + " bpm");
                if (bpm <= 95) Assert.AreEqual(0.3, amp, 1e-9, "full to 95 bpm");
                if (bpm >= 120) { Assert.AreEqual(0.0, amp); Assert.IsFalse(scheduled); }
            }
            Assert.AreEqual(0.15, 0.3 * PrismPulseSafety.Taper("load", 107.5, 60000), 1e-9);
        }

        [Test]
        public void TheTapGivesWayToTheStatesRateAsWellAsTheBeats()
        {
            // The state can be two seconds older or newer than the beat. The picture takes the
            // smaller of the two amplitudes; the tap takes the smaller of the two tapers.
            foreach (var rates in new[] { (115.0, 100.0), (118.0, 96.0), (100.0, 115.0), (90.0, 80.0), (96.0, 96.0) })
            {
                double stateBpm = rates.Item1, beatBpm = rates.Item2;
                var view = new PrismPulseView();
                view.OnState(Session, "regulate", 60000,
                    PrismPulseSafety.PulseAmplitude("regulate", stateBpm, 60000), stateBpm);
                Assert.IsTrue(view.OnBeat(Session, 1, "ok", 60000.0 / beatBpm, beatBpm, 2000, 1600));
                double amp = Run(view, 1990, 2010, true, out _);
                double expected = 0.3 * System.Math.Min(
                    PrismPulseSafety.VisualRateGain(stateBpm), PrismPulseSafety.VisualRateGain(beatBpm));
                if (expected < PrismPulseSafety.MinHapticStrength) expected = 0.0;
                Assert.AreEqual(expected, amp, 1e-9, $"state {stateBpm}, beat {beatBpm}");
            }
        }

        [Test]
        public void NoTapOutsideASessionWhileFrozenOrForAnotherSessionsBeats()
        {
            foreach (string segment in new[] { "idle", "reset", "", null })
            {
                PrismPulseView outside = View(segment, 70);
                outside.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600);
                Run(outside, 1600, 2300, true, out int taps);
                Assert.AreEqual(0, taps, "segment " + segment);
            }

            // a beat queued before the session started (idle, the countdown) never plays in it
            PrismPulseView starting = View("idle", 70);
            starting.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600);
            starting.OnState(Session, "baseline", 45000, 0.07);
            Run(starting, 1600, 2300, true, out int fromBefore);
            Assert.AreEqual(0, fromBefore);

            // the previous session's beat, arriving in the new session
            PrismPulseView next = View("baseline", 70);
            Assert.IsFalse(next.OnBeat("S-20261010-0000", 9, "ok", 857, 70, 2000, 1600));
            Run(next, 1600, 2300, true, out int wrongSession);
            Assert.AreEqual(0, wrongSession);

            // stale feed: the freeze drops what was queued and nothing plays after it
            PrismPulseView frozen = View("load", 70);
            Assert.IsTrue(frozen.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            frozen.Frozen = true;
            Run(frozen, 1600, 2300, true, out int whileFrozen);
            frozen.Frozen = false;
            Run(frozen, 2300, 3000, true, out int afterFreeze);
            Assert.AreEqual(0, whileFrozen + afterFreeze);

            // rejected beats are never rendered, in the hand either
            PrismPulseView rejected = View("load", 70);
            Assert.IsFalse(rejected.OnBeat(Session, 1, "rejected", 857, 70, 2000, 1600));
            Run(rejected, 1600, 2300, true, out int rejectedTaps);
            Assert.AreEqual(0, rejectedTaps);
        }

        [Test]
        public void NoTapWhenTheLaptopReportsNoHeartRate()
        {
            // signal lost: hr_bpm is null, so the state's own pulse amplitude is zero
            var view = new PrismPulseView();
            view.OnState(Session, "load", 60000, PrismPulseSafety.PulseAmplitude("load", 0, 60000));
            view.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600);
            Run(view, 1600, 2300, true, out int taps);
            Assert.AreEqual(0, taps);
        }

        [Test]
        public void NoTapUnlessReallyLinkedToTheLaptop()
        {
            // Offline Preview plays pretend beats through the same pulse; they must not tap
            PrismPulseView view = View("load", 70);
            Assert.IsTrue(view.OnBeat(Session, 1, "ok", 857, 70, 2000, 1600));
            Run(view, 1600, 2300, false, out int taps);
            Assert.AreEqual(0, taps);
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(true, false, 1.0, 0.3));
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(false, true, 1.0, 0.3));
        }

        [Test]
        public void TheTapFadesWithResolvesLastThreeSeconds()
        {
            Assert.AreEqual(1.0, PrismPulseSafety.Taper("resolve", 70, 3000), 1e-12);
            Assert.AreEqual(System.Math.Cos(System.Math.PI / 4), PrismPulseSafety.Taper("resolve", 70, 1500), 1e-12);
            Assert.AreEqual(0.0, PrismPulseSafety.Taper("resolve", 70, 0), 1e-12);
            // unchanged for the picture: PulseAmplitude still equals its old formula
            Assert.AreEqual(0.08 * System.Math.Cos(System.Math.PI / 4),
                PrismPulseSafety.PulseAmplitude("resolve", 70, 1500), 1e-12);
            Assert.AreEqual(0.0, PrismPulseSafety.PulseAmplitude("resolve", 70, 0));
        }

        [Test]
        public void TheTapIsAlwaysGentleAndNeverANumberThatIsNotOne()
        {
            Assert.AreEqual(PrismPulseSafety.MaxHapticStrength, PrismPulseSafety.HapticAmplitude(true, true, 1.0, 5.0));
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(true, true, 1.0, -1.0));
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(true, true, double.NaN, 0.3));
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(true, true, 1.0, double.PositiveInfinity));
            Assert.AreEqual(0.0, PrismPulseSafety.HapticAmplitude(true, true, 0.05, 0.3), "too faint to send");
            Assert.AreEqual(0.3, PrismPulseSafety.HapticAmplitude(true, true, 7.0, 0.3), 1e-12);
        }

        [Test]
        public void OnlyTheRightControllerIsEverDriven()
        {
            string code = File.ReadAllText(Path.Combine(Application.dataPath, "Prism", "PrismHaptics.cs"));
            string logic = "";
            foreach (string line in code.Split('\n'))
                if (!line.TrimStart().StartsWith("//")) logic += line + "\n";     // comments may say "left"
            foreach (string left in new[] { "LTouch", "Controller.Touch", "Controller.All", "Controller.Active",
                                            "Characteristics.Left", "XRNode.LeftHand", "LHand" })
                StringAssert.DoesNotContain(left, logic);
            StringAssert.Contains("InputDeviceCharacteristics.Right | InputDeviceCharacteristics.Controller", logic);
            StringAssert.Contains("OVRInput.Controller.RTouch", logic);
            // the decision is the tested one, with the real-link test in it
            StringAssert.Contains("PrismPulseSafety.HapticAmplitude(started, link.IsLinked, field.PulseTaper, strength)", logic);
            // and the laptop's own heart rate reaches the taper
            string linkCode = File.ReadAllText(Path.Combine(Application.dataPath, "Prism", "PrismLink.cs"));
            StringAssert.Contains("field.SetPulseContext(m.session, m.segment, remainingMs, t.pulse, hr);", linkCode);
            // no timer, coroutine or heart-rate arithmetic that could make a beat here
            foreach (string invented in new[] { "InvokeRepeating", "StartCoroutine", "heartRate", "hr_bpm", "60f /", "60.0 /" })
                StringAssert.DoesNotContain(invented, logic);
        }

        static readonly string[] Segments = { "idle", "baseline", "load", "regulate", "resolve", "reset", "", null, "Idle", "idle ", "-" };
        static readonly double[] Ages = { 0, 40, 500, -500, 501, -501, 2400, 9000, double.NaN, double.PositiveInfinity };

        static bool Fresh(double age) => age >= -500 && age <= 500;

        [Test]
        public void TheControllerCheckFiresOnlyOnAFreshLinkedIdleState()
        {
            foreach (string segment in Segments)
                foreach (bool linked in new[] { false, true })
                    foreach (double age in Ages)
                    {
                        var check = new PrismTestTap();
                        Assert.AreEqual(PrismTestTap.Verdict.Nothing, check.OnState(linked, segment, age), "no controller yet");
                        Assert.IsTrue(check.Seen(true));
                        PrismTestTap.Verdict verdict = check.OnState(linked, segment, age);
                        string what = $"{segment} linked {linked} age {age}";
                        if (PrismPulseSafety.ActiveSegment(segment)) Assert.AreEqual(PrismTestTap.Verdict.Drop, verdict, what);
                        else if (linked && segment == "idle" && Fresh(age)) Assert.AreEqual(PrismTestTap.Verdict.Fire, verdict, what);
                        else Assert.AreEqual(PrismTestTap.Verdict.Wait, verdict, what);
                        Assert.AreEqual(verdict == PrismTestTap.Verdict.Wait, check.Pending);
                        // fired or cancelled: nothing asks again for the rest of the run
                        if (verdict != PrismTestTap.Verdict.Wait)
                        {
                            check.Seen(false);
                            Assert.IsTrue(check.Seen(true), "still reported as found, for the log");
                            Assert.IsFalse(check.Pending);
                            Assert.AreEqual(PrismTestTap.Verdict.Nothing, check.OnState(true, "idle", 0));
                        }
                    }
        }

        [Test]
        public void AControllerFoundInASessionIsNeverCheckedInItOrAfterIt()
        {
            // woken mid-session, in every running segment, linked or with the feed gone
            foreach (string segment in new[] { "baseline", "load", "regulate", "resolve" })
                foreach (bool linked in new[] { false, true })
                {
                    var check = new PrismTestTap();
                    check.Seen(true);
                    Assert.AreEqual(PrismTestTap.Verdict.Drop, check.OnState(linked, segment, 0));
                    foreach (string later in new[] { segment, "resolve", "reset", "idle", "idle" })
                        Assert.AreEqual(PrismTestTap.Verdict.Nothing, check.OnState(true, later, 0), "kept for later");
                }

            // found while the feed was gone, session still running when it comes back
            var waiting = new PrismTestTap();
            waiting.Seen(true);
            Assert.AreEqual(PrismTestTap.Verdict.Wait, waiting.OnState(false, "idle", 0));
            Assert.AreEqual(PrismTestTap.Verdict.Wait, waiting.OnState(true, "reset", 0));
            Assert.AreEqual(PrismTestTap.Verdict.Drop, waiting.OnState(true, "load", 0));
            Assert.AreEqual(PrismTestTap.Verdict.Nothing, waiting.OnState(true, "idle", 0));

            // WiFi stalls in idle, the attendant starts the session, then the backlog arrives in
            // order: the held-up "idle" is old and fires nothing, and "baseline" cancels
            var stalled = new PrismTestTap();
            stalled.Seen(true);
            Assert.AreEqual(PrismTestTap.Verdict.Wait, stalled.OnState(true, "idle", 4000));
            Assert.AreEqual(PrismTestTap.Verdict.Wait, stalled.OnState(true, "idle", 2000));
            Assert.AreEqual(PrismTestTap.Verdict.Drop, stalled.OnState(true, "baseline", 30));
            Assert.AreEqual(PrismTestTap.Verdict.Nothing, stalled.OnState(true, "idle", 0));

            // before the clock is known nothing is fresh
            var undated = new PrismTestTap();
            undated.Seen(true);
            Assert.AreEqual(PrismTestTap.Verdict.Wait, undated.OnState(true, "idle", double.NaN));
            Assert.AreEqual(PrismTestTap.Verdict.Fire, undated.OnState(true, "idle", 12));

            // a controller that goes away before its check may ask again; after it, never
            var gone = new PrismTestTap();
            gone.Seen(true);
            Assert.IsFalse(gone.Seen(false));
            Assert.AreEqual(PrismTestTap.Verdict.Nothing, gone.OnState(true, "idle", 0));
            Assert.IsTrue(gone.Seen(true));
            Assert.IsFalse(gone.Seen(true), "found once, not every frame");
            Assert.AreEqual(PrismTestTap.Verdict.Fire, gone.OnState(true, "idle", 0));
            for (int wake = 0; wake < 5; wake++)
            {
                gone.Seen(false);
                gone.Seen(true);
                Assert.AreEqual(PrismTestTap.Verdict.Nothing, gone.OnState(true, "idle", 0), "one check per run");
            }
        }

        [Test]
        public void NoRunOfStatesEverFiresTheCheckTwiceOrOnAnythingButFreshLinkedIdle()
        {
            var random = new System.Random(20261012);
            for (int run = 0; run < 2000; run++)
            {
                var check = new PrismTestTap();
                bool over = false;
                for (int step = 0; step < 40; step++)
                {
                    if (random.Next(4) == 0) check.Seen(random.Next(3) != 0);
                    string segment = Segments[random.Next(Segments.Length)];
                    double age = Ages[random.Next(Ages.Length)];
                    bool linked = random.Next(3) != 0;
                    bool pending = check.Pending;
                    PrismTestTap.Verdict verdict = check.OnState(linked, segment, age);
                    if (over) Assert.AreEqual(PrismTestTap.Verdict.Nothing, verdict, "after the one check or its cancelling");
                    if (verdict == PrismTestTap.Verdict.Fire)
                        Assert.IsTrue(pending && linked && segment == "idle" && Fresh(age));
                    if (pending && PrismPulseSafety.ActiveSegment(segment))
                        Assert.AreEqual(PrismTestTap.Verdict.Drop, verdict);
                    over |= verdict == PrismTestTap.Verdict.Fire || verdict == PrismTestTap.Verdict.Drop;
                }
            }
        }

        [Test]
        public void TheCheckTapHasOneWayOutAndTheBeatTapItsOwn()
        {
            string code = File.ReadAllText(Path.Combine(Application.dataPath, "Prism", "PrismHaptics.cs")).Replace("\r\n", "\n");
            // two places send a tap: the gated beat, and the check inside the state handler
            int sends = 0, at = 0;
            while ((at = code.IndexOf(" Send(amp)", at, System.StringComparison.Ordinal)) >= 0) { sends++; at++; }
            Assert.AreEqual(2, sends);
            int handler = code.IndexOf("void OnState(LinkMsg m)", System.StringComparison.Ordinal);
            int fire = code.IndexOf("case PrismTestTap.Verdict.Fire:", System.StringComparison.Ordinal);
            int handlerEnd = code.IndexOf("string Send(float amp)", System.StringComparison.Ordinal);
            Assert.IsTrue(handler > 0 && fire > handler && fire < handlerEnd);
            Assert.IsTrue(code.IndexOf(" Send(amp)", fire, System.StringComparison.Ordinal) < handlerEnd);
            StringAssert.Contains("switch (check.OnState(link.IsLinked, m.segment, link.StateAgeMs(m)))", code);
            string linkCode = File.ReadAllText(Path.Combine(Application.dataPath, "Prism", "PrismLink.cs"));
            StringAssert.Contains("haveOffset && m != null ? NowMs + offset - m.t_engine : double.NaN", linkCode);
            StringAssert.Contains("link.StateReceived += OnState;", code);
            // nothing else reaches a controller
            int vibrations = 0; at = 0;
            while ((at = code.IndexOf("SetControllerVibration(", at, System.StringComparison.Ordinal)) >= 0) { vibrations++; at++; }
            Assert.AreEqual(2, vibrations, "one tap call and its stop");
            int impulses = 0; at = 0;
            while ((at = code.IndexOf("SendHapticImpulse(", at, System.StringComparison.Ordinal)) >= 0) { impulses++; at++; }
            Assert.AreEqual(1, impulses);
        }

        [Test]
        public void EveryBeatWithoutATapHasAReasonAndEveryTapHasNone()
        {
            foreach (bool linked in new[] { false, true })
                foreach (double taper in new[] { 0.0, 0.03, 0.5, 1.0, double.NaN })
                    foreach (double strength in new[] { 0.0, 0.01, 0.35, 5.0, double.NaN })
                    {
                        bool tap = PrismPulseSafety.HapticAmplitude(true, linked, taper, strength) > 0.0;
                        string why = PrismPulseSafety.HapticBlock(linked, taper, strength);
                        Assert.AreEqual(tap, why == null, $"linked {linked}, taper {taper}, strength {strength}");
                    }
            StringAssert.Contains("not linked", PrismPulseSafety.HapticBlock(false, 1.0, 0.35));
            StringAssert.Contains("tapered", PrismPulseSafety.HapticBlock(true, 0.0, 0.35));
        }

        [Test]
        public void TheAppIsControllersOnlySoTheHeadsetDoesNotSleepTheController()
        {
            // With hand tracking allowed, the headset put the still right controller to sleep
            // ("sent to sleep as hand tracking is enabled and controllers are not in use").
            string config = File.ReadAllText("Assets/Oculus/OculusProjectConfig.asset");
            StringAssert.Contains("handTrackingSupport: 0", config);
            string manifest = File.ReadAllText("Assets/Plugins/Android/AndroidManifest.xml");
            StringAssert.DoesNotContain("handtracking", manifest.ToLowerInvariant());
            StringAssert.DoesNotContain("hand_tracking", manifest.ToLowerInvariant());
            string scene = File.ReadAllText("Assets/Scenes/SampleScene.unity").Replace("\r\n", "\n");
            StringAssert.Contains("  strength: 0.35\n  tapSeconds: 0.06\n", scene);
        }

        [Test]
        public void TheSceneCarriesTheHapticsComponentOnce()
        {
            string guid = UnityEditor.AssetDatabase.AssetPathToGUID("Assets/Prism/PrismHaptics.cs");
            Assert.IsNotEmpty(guid);
            string scene = File.ReadAllText("Assets/Scenes/SampleScene.unity");
            int count = 0, at = 0;
            while ((at = scene.IndexOf(guid, at, System.StringComparison.Ordinal)) >= 0) { count++; at += guid.Length; }
            Assert.AreEqual(1, count);
        }
    }
}
