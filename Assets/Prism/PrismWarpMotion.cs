using UnityEngine;

namespace Prism
{
    /// Where the tunnel is along its endless path. Every value is wrapped at its OWN exact
    /// period, so nothing grows all session and nothing jumps when a value wraps.
    public struct WarpPhase
    {
        public float travel;   // metres, wrapped at the streak span: only recycles streaks
        public float twist;    // radians, wrapped at 2 pi: the spiral
        public float bendA;    // radians, wrapped at 2 pi: the tunnel's left/right winding
        public float bendB;    // radians, wrapped at 2 pi: its up/down winding
        public float spin;     // radians, wrapped at 2 pi: the slow roll
    }

    /// The tunnel's motion, kept apart from PrismWarp so it can be tested without a scene, and
    /// a line-for-line mirror of the placement in PrismWarp.shader (Depth, Fade, Wall).
    ///
    /// The break this replaces: the shader used ONE distance, wrapped every 72 m, for three
    /// things. Recycling streaks repeats every 72 m, so that was seamless. The spiral
    /// (0.05 rad per metre) and the winding (0.045 and 0.033 rad per metre) do not repeat
    /// every 72 m, so at each wrap the whole tunnel turned by 3.6 radians and its bends
    /// snapped to a different shape: once every 18 s at baseline speed, 13 s in load.
    public static class PrismWarpMotion
    {
        public const float Behind = 12f;          // metres of tunnel kept behind the viewer
        public const float BendRatio = 0.73f;     // second winding axis, as in the shader
        public const float BendOffset = 1.7f;
        const float TwoPi = Mathf.PI * 2f;

        public static float Span(float tunnelLength) => tunnelLength + Behind;

        /// Advance by one frame. speed in metres per second; spinRate in radians per second.
        public static WarpPhase Step(WarpPhase p, float speed, float dt, float span,
                                     float twist, float curveK, float spinRate)
        {
            float d = speed * dt;
            p.travel = Mathf.Repeat(p.travel + d, span);
            p.twist = Mathf.Repeat(p.twist + twist * d, TwoPi);
            p.bendA = Mathf.Repeat(p.bendA + curveK * d, TwoPi);
            p.bendB = Mathf.Repeat(p.bendB + curveK * BendRatio * d, TwoPi);
            p.spin = Mathf.Repeat(p.spin + spinRate * dt, TwoPi);
            return p;
        }

        /// Shader: z = _Far - fmod(z0 * _Span + _Travel, _Span). Far ahead, down to Behind.
        public static float Depth(float z0, float travel, float span, float far) =>
            far - Mathf.Repeat(z0 * span + travel, span);

        /// Shader: fade. Zero at both ends of the span, so a recycled streak is never seen.
        public static float Fade(float z, float far) =>
            Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(far, far * 0.55f, z)) *
            Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(-8f, -1f, z));

        /// Shader: visible. A streak fades in and out across a narrow band of density instead
        /// of switching, so thinning the tunnel never pops single streaks.
        public static float Visible(float seed, float density) =>
            Mathf.Clamp01((density - seed) / DensityBand);

        public const float DensityBand = 0.05f;

        /// Shader: wall(). A point on the tunnel wall at depth z, in tunnel space.
        public static Vector3 Wall(float th, float r, float z, WarpPhase p,
                                   float twist, float curveK, float curveAmp)
        {
            float ang = th + twist * z + p.twist + p.spin;
            float cx = Mathf.Sin(curveK * z + p.bendA) - Mathf.Sin(p.bendA);
            float cy = Mathf.Sin(curveK * BendRatio * z + p.bendB + BendOffset) - Mathf.Sin(p.bendB + BendOffset);
            return new Vector3(Mathf.Cos(ang) * r + cx * curveAmp, Mathf.Sin(ang) * r + cy * curveAmp, z);
        }

        /// The tunnel's light colour, by the fog field's own rule: each palette inside the
        /// global token limits, and a segment change is a cross-dissolve between the two
        /// palettes' colours, weighted by the laptop's blend. Hue is never interpolated, so
        /// blue to amber cannot pass through green on the way.
        public static Color Tint(FieldTokens previous, FieldTokens current, float blend)
        {
            Color mix = Color.Lerp(PaletteColour(previous), PaletteColour(current), Mathf.Clamp01(blend));
            // Part way between a pale blue and a pale amber the plain mix puts green a little
            // ahead of red and blue. Hold it level with the larger of the two, so the way
            // across is a plain pale white and never even faintly green.
            mix.g = Mathf.Min(mix.g, Mathf.Max(mix.r, mix.b));
            return mix;
        }

        static Color PaletteColour(FieldTokens tokens)
        {
            FieldTokens t = PrismField.Limits(tokens);
            return Color.HSVToRGB(Mathf.Repeat(t.hue, 360f) / 360f, Mathf.Clamp01(t.sat * 0.6f), 1f).linear;
        }
    }
}
