using System;
using System.IO;
using NUnit.Framework;
using UnityEngine;

namespace Prism.Tests
{
    /// The tunnel must be one continuous flow: nothing may move further in a frame than that
    /// frame's own travel, at any speed the session uses, across every wrap of every value.
    /// PrismWarpMotion mirrors PrismWarp.shader; the last test pins the two together.
    public class PrismWarpMotionTests
    {
        // PrismWarp's Inspector defaults (and the scene's)
        const float TunnelLength = 60f, Twist = 0.05f, CurveK = 0.045f, CurveAmp = 2.2f;
        const float SpinSpeed = 0.04f, BaselineSpeed = 4f, EaseSeconds = 3f;
        const float Dt = 1f / 72f;
        static readonly float Span = PrismWarpMotion.Span(TunnelLength);

        struct Streak { public float th, r, z0; }

        static Streak[] Streaks()
        {
            var rng = new System.Random(7);
            var list = new Streak[48];
            for (int i = 0; i < list.Length; i++)
                list[i] = new Streak
                {
                    th = (float)(rng.NextDouble() * Math.PI * 2.0),
                    r = 1.4f + (float)rng.NextDouble() * 12f,        // inner wall to the deepest scatter
                    z0 = (float)rng.NextDouble(),
                };
            return list;
        }

        /// Target speeds through a whole visit, each held long enough to cross several wraps:
        /// idle, baseline and load at their fastest (heart-rate nudge, capped at 7), regulate
        /// slowing, resolve nearly still, then back to idle.
        static float TargetSpeed(float t)
        {
            if (t < 20f) return 2.5f;
            if (t < 65f) return 5f;
            if (t < 140f) return 7f;
            if (t < 215f) return Mathf.Lerp(5f, 1.275f, (t - 140f) / 75f);
            if (t < 260f) return Mathf.Lerp(1.8f, 0.6f, (t - 215f) / 45f);
            return 2.5f;
        }

        delegate Vector3 Place(Streak s, float z, WarpPhase p);

        static Vector3 Now(Streak s, float z, WarpPhase p) =>
            PrismWarpMotion.Wall(s.th, s.r, z, p, Twist, CurveK, CurveAmp);

        /// The shader as it was before the fix: one wrapped distance for everything.
        static Vector3 Before(Streak s, float z, WarpPhase p)
        {
            float u = z + p.travel;
            float ang = s.th + Twist * u + p.spin;
            float cx = Mathf.Sin(CurveK * u) - Mathf.Sin(CurveK * p.travel);
            float cy = Mathf.Sin(CurveK * 0.73f * u + 1.7f) - Mathf.Sin(CurveK * 0.73f * p.travel + 1.7f);
            return new Vector3(Mathf.Cos(ang) * s.r + cx * CurveAmp, Mathf.Sin(ang) * s.r + cy * CurveAmp, z);
        }

        /// Runs the visit. Returns the largest step as a multiple of that frame's normal step
        /// (1 = exactly the frame's own travel) and counts every wrap that was crossed.
        static float WorstStep(Place place, out int[] wraps, out float worstRecycleFade)
        {
            Streak[] streaks = Streaks();
            var last = new Vector3[streaks.Length];
            var lastZ = new float[streaks.Length];
            var phase = new WarpPhase();
            float speed = 2.5f, worst = 0f;
            wraps = new int[5];
            worstRecycleFade = 0f;

            for (int i = 0; i < streaks.Length; i++)
            {
                lastZ[i] = PrismWarpMotion.Depth(streaks[i].z0, phase.travel, Span, TunnelLength);
                last[i] = place(streaks[i], lastZ[i], phase);
            }

            for (float t = 0f; t < 300f; t += Dt)
            {
                speed += (TargetSpeed(t) - speed) * (1f - Mathf.Exp(-Dt / EaseSeconds));   // PrismWarp's easing
                float spinRate = SpinSpeed * Mathf.Clamp01(speed / BaselineSpeed);
                WarpPhase next = PrismWarpMotion.Step(phase, speed, Dt, Span, Twist, CurveK, spinRate);
                if (next.travel < phase.travel) wraps[0]++;
                if (next.twist < phase.twist) wraps[1]++;
                if (next.bendA < phase.bendA) wraps[2]++;
                if (next.bendB < phase.bendB) wraps[3]++;
                if (next.spin < phase.spin) wraps[4]++;
                phase = next;

                float d = speed * Dt;
                for (int i = 0; i < streaks.Length; i++)
                {
                    float z = PrismWarpMotion.Depth(streaks[i].z0, phase.travel, Span, TunnelLength);
                    Vector3 at = place(streaks[i], z, phase);
                    if (Mathf.Abs(z - lastZ[i]) > Span * 0.5f)
                    {
                        // This streak was recycled from behind to far ahead: allowed only unseen.
                        worstRecycleFade = Mathf.Max(worstRecycleFade,
                            Mathf.Max(PrismWarpMotion.Fade(lastZ[i], TunnelLength), PrismWarpMotion.Fade(z, TunnelLength)));
                    }
                    else
                    {
                        // One frame's normal step: the travel itself, the winding it follows
                        // and the slow roll at this radius. Nothing else may move a streak.
                        float normal = d * (1f + CurveAmp * CurveK * (1f + PrismWarpMotion.BendRatio))
                                       + streaks[i].r * spinRate * Dt;
                        worst = Mathf.Max(worst, (at - last[i]).magnitude / normal);
                    }
                    last[i] = at;
                    lastZ[i] = z;
                }
            }
            return worst;
        }

        [Test]
        public void NothingJumpsAcrossAnyWrapAtAnySessionSpeed()
        {
            float worst = WorstStep(Now, out int[] wraps, out float recycleFade);
            string[] names = { "streak distance", "spiral", "bend A", "bend B", "roll" };
            for (int i = 0; i < wraps.Length; i++)
                Assert.GreaterOrEqual(wraps[i], 1, "the run never crossed a wrap of the " + names[i]);
            Assert.GreaterOrEqual(wraps[0], 10);
            // 1.0 is exactly one frame's normal step; the margin is float rounding only.
            Assert.LessOrEqual(worst, 1.02f, "something moved further in a frame than the frame's own travel");
            // A recycled streak lands up to one frame's travel inside the far fade, where it is
            // under a thousandth of its brightness (0.00004 at the speed ceiling): unseen.
            Assert.Less(recycleFade, 1e-3f, "a streak was recycled while visible");
        }

        [Test]
        public void TheSameCheckCatchesTheOldSingleWrappedDistance()
        {
            // Control: before the fix every streak jumped by metres each time the distance
            // wrapped. If this passes with a small number, the test above proves nothing.
            float worst = WorstStep(Before, out int[] wraps, out _);
            Assert.GreaterOrEqual(wraps[0], 10);
            Assert.Greater(worst, 20f);
        }

        [Test]
        public void TheOldJumpWasTheSpiralAndTheBendsNotRepeatingAtTheWrap()
        {
            // 72 m of spiral is 3.6 rad and 72 m of winding 3.24 and 2.37 rad: none is a whole
            // turn, so wrapping the distance turned and reshaped the whole tunnel.
            foreach (float perMetre in new[] { Twist, CurveK, CurveK * PrismWarpMotion.BendRatio })
            {
                float turns = perMetre * Span / (2f * Mathf.PI);
                Assert.Greater(Mathf.Abs(turns - Mathf.Round(turns)), 0.05f);
            }
        }

        [Test]
        public void NoValueGrowsThroughALongSession()
        {
            var phase = new WarpPhase();
            for (int i = 0; i < 72 * 60 * 30; i++)          // half an hour at the speed ceiling
                phase = PrismWarpMotion.Step(phase, 7f, Dt, Span, Twist, CurveK, SpinSpeed);
            Assert.That(phase.travel, Is.InRange(0f, Span));
            foreach (float angle in new[] { phase.twist, phase.bendA, phase.bendB, phase.spin })
                Assert.That(angle, Is.InRange(0f, 2f * Mathf.PI));
        }

        [Test]
        public void ThinningTheTunnelFadesStreaksAndNeverSwitchesThem()
        {
            // density eases over seconds; no streak's brightness may step by more than that
            // frame's share of the fade band
            float worst = 0f;
            for (float seed = 0f; seed <= 1f; seed += 0.01f)
                for (float density = 0f; density < 1f; density += 0.001f)
                    worst = Mathf.Max(worst, Mathf.Abs(
                        PrismWarpMotion.Visible(seed, density + 0.001f) - PrismWarpMotion.Visible(seed, density)));
            Assert.LessOrEqual(worst, 0.001f / PrismWarpMotion.DensityBand + 1e-4f);
            Assert.AreEqual(0f, PrismWarpMotion.Visible(0.5f, 0f));      // resolve ends with none
            Assert.AreEqual(1f, PrismWarpMotion.Visible(0.5f, 1f));
        }

        [Test]
        public void TunnelColourDissolvesBetweenPalettesAndNeverPassesThroughGreen()
        {
            // load is blue (hue 202..214), regulate amber (24..46): every pairing, every blend
            foreach (float a in new[] { 0f, 0.5f, 1f })
                foreach (float c in new[] { 0f, 0.5f, 1f })
                {
                    FieldTokens load = PrismMapping.Load(a, c, 80f), regulate = PrismMapping.Regulate(a, c, 80f);
                    FieldTokens baseline = PrismMapping.Baseline(c, 70f);
                    foreach (var pair in new[] { (load, regulate), (regulate, baseline), (baseline, load) })
                        for (float blend = 0f; blend <= 1f; blend += 0.02f)
                        {
                            Color tint = PrismWarpMotion.Tint(pair.Item1, pair.Item2, blend);
                            Assert.LessOrEqual(tint.g, Mathf.Max(tint.r, tint.b) + 1e-4f,
                                $"green leads at blend {blend}: {tint}");
                            // and it IS the dissolve between the two palettes' colours: a
                            // turned hue would sit far from this line whatever was clamped
                            Color line = Color.Lerp(PrismWarpMotion.Tint(pair.Item1, pair.Item1, 1f),
                                                    PrismWarpMotion.Tint(pair.Item2, pair.Item2, 1f), blend);
                            Assert.AreEqual(line.r, tint.r, 1e-4f);
                            Assert.AreEqual(line.b, tint.b, 1e-4f);
                            // green is the dissolve's own, held level with the larger of red
                            // and blue wherever the plain mix would put it ahead
                            Assert.AreEqual(Mathf.Min(line.g, Mathf.Max(line.r, line.b)), tint.g, 1e-4f);
                        }
                    Assert.AreEqual(PrismWarpMotion.Tint(load, load, 0f), PrismWarpMotion.Tint(load, regulate, 0f));
                    Assert.AreEqual(PrismWarpMotion.Tint(regulate, regulate, 1f), PrismWarpMotion.Tint(load, regulate, 1f));
                }
        }

        [Test]
        public void TurningTheHueInsteadWouldHaveGoneGreen()
        {
            // Control for the test above: the old tunnel read one hue that PrismLink eases as
            // a number, so 208 to 35 degrees ran through 120.
            bool green = false;
            for (float k = 0f; k <= 1f; k += 0.02f)
            {
                Color c = Color.HSVToRGB(Mathf.Lerp(208f, 35f, k) / 360f, 0.18f, 1f).linear;
                if (c.g > Mathf.Max(c.r, c.b) + 1e-4f) green = true;
            }
            Assert.IsTrue(green);
        }

        [Test]
        public void TheShaderPlacesStreaksExactlyAsTheTestedMirrorDoes()
        {
            string folder = Path.Combine(Application.dataPath, "Prism");
            string shader = File.ReadAllText(Path.Combine(folder, "PrismWarp.shader"));
            StringAssert.Contains("float z = _Far - fmod(v.p.z * _Span + _Travel, _Span);", shader);
            StringAssert.Contains("float ang = th + _Twist * z + _TwistPhase + _Spin;", shader);
            StringAssert.Contains("float2 a = float2(sin(_CurveK * z + _BendA), sin(_CurveK * 0.73 * z + _BendB + 1.7));", shader);
            StringAssert.Contains("float2 b = float2(sin(_BendA), sin(_BendB + 1.7));", shader);
            StringAssert.Contains("smoothstep(_Far, _Far * 0.55, z) * smoothstep(-8.0, -1.0, z)", shader);
            StringAssert.Contains("saturate((_Density - v.p.w) / 0.05)", shader);
            // _Travel may appear in the streak recycling line and nowhere else in the maths
            int uses = 0;
            foreach (string line in shader.Split('\n'))
                if (line.Contains("_Travel") && !line.TrimStart().StartsWith("//") && !line.Contains("float _Travel")) uses++;
            Assert.AreEqual(1, uses, "the wrapped distance must only recycle streaks");

            string driver = File.ReadAllText(Path.Combine(folder, "PrismWarp.cs"));
            foreach (string uniform in new[] { "_Travel", "_TwistPhase", "_BendA", "_BendB", "_Spin" })
                StringAssert.Contains("warpMat.SetFloat(\"" + uniform + "\", phase.", driver);
            StringAssert.Contains("PrismWarpMotion.Tint(field.Previous, field.Current, field.Blend)", driver);
            StringAssert.DoesNotContain("HSVToRGB", driver, "the tunnel's colour comes from PrismWarpMotion.Tint only");
        }
    }
}
