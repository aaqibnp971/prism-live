using System.Collections.Generic;
using UnityEngine;

namespace Prism
{
    /// The seven numbers that control everything the person sees.
    [System.Serializable]
    public struct FieldTokens
    {
        public float fog;      // 0.18 – 0.62
        public float light;    // 0.35 – 1.10
        public float hue;      // degrees, 18 – 216
        public float sat;      // 0.08 – 0.36
        public float horizon;  // 0.38 – 0.62, measured from top, 0.50 = eye level
        public float pulse;    // 0.00 – 0.22
        public float motion;   // 0.004 – 0.045

        public FieldTokens(float fog, float light, float hue, float sat, float horizon, float pulse, float motion)
        {
            this.fog = fog; this.light = light; this.hue = hue; this.sat = sat;
            this.horizon = horizon; this.pulse = pulse; this.motion = motion;
        }
    }

    /// Applies seven numbers to the world. Knows nothing about networking or segments.
    public class PrismField : MonoBehaviour
    {
        [Tooltip("Optional: a material using the Prism/Sky shader. Left empty, one is made automatically.")]
        public Material skyTemplate;
        [Tooltip("Filled in automatically. Keeps the Prism/Sky shader included in the build.")]
        public Shader skyShader;
        [Tooltip("Optional: a directional light, for the task objects later")]
        public Light diffuseLight;
        [Tooltip("How wide (degrees) the renderer's screen is when wrapped around you")]
        public float viewWidthDegrees = 100f;
        [Tooltip("How tall (degrees) the renderer's screen is; 0.50 = eye level")]
        public float viewHeightDegrees = 90f;

        // reference canvas in pixels; this width makes the fog stripes wrap seamlessly behind you
        const float RefHeight = 1080f;
        const float RefWidth = 1909.93f;
        const float Rise = 0.09f, Fall = 0.18f;
        const float MaxPulse = 0.22f;
        const double LateOnset = 0.045;     // onsets first seen later than this never flash
        const double MinBeatGap = 60.0 / 180.0;

        Material sky;
        FieldTokens current, previous;
        float blend = 1f;                   // weight of `current`
        float drift, env;
        double beatStart = double.NegativeInfinity;
        readonly List<double> beats = new List<double>();
        bool frozen;

        public FieldTokens Current => current;

        /// Goes up by one each time a heartbeat pulse starts (PrismSound listens to this).
        public int BeatCount { get; private set; }

        /// Current heartbeat envelope 0..1 (other visuals pulse with it).
        public float PulseEnvelope => env;

        /// Freeze the last picture (connection lost). Clears future beats.
        public bool Frozen
        {
            get => frozen;
            set { frozen = value; if (value) beats.Clear(); }
        }

#if UNITY_EDITOR
        void Reset() => OnValidate();
        void OnValidate()
        {
            if (!skyShader) skyShader = Shader.Find("Prism/Sky");
        }
#endif

        void Awake()
        {
            if (skyTemplate) sky = new Material(skyTemplate);
            else if (skyShader) sky = new Material(skyShader);
            else
            {
                Debug.LogError("[Prism] No Prism/Sky shader found. Is PrismSky.shader in the Assets folder?");
                enabled = false;
                return;
            }
            if (!sky.shader.isSupported) Debug.LogError("[Prism] Prism/Sky shader is not supported on this device.");
            RenderSettings.skybox = sky;

            // Make every camera draw the sky (some VR templates default to a solid colour)
            foreach (var cam in FindObjectsByType<Camera>(FindObjectsSortMode.None))
                cam.clearFlags = CameraClearFlags.Skybox;

            // Hide any leftover floor / grid objects so only the Prism world is visible
            foreach (var r in FindObjectsByType<MeshRenderer>(FindObjectsSortMode.None))
                if (r.gameObject.name.ToLower().Contains("floor") || r.gameObject.name.ToLower().Contains("plane"))
                    r.enabled = false;

            Debug.Log("[Prism] Field running.");
            current = previous = PrismMapping.Baseline(0f, 0f);
            Push();
        }

        /// Update the current palette (within a segment).
        public void SetTokens(FieldTokens t) => current = t;

        /// Call at a segment boundary: the old palette is kept and dissolved away.
        public void BeginDissolve()
        {
            previous = current;
            blend = 0f;
        }

        /// 0 → old palette, 1 → new palette. Host-driven: segment_elapsed_ms / 10000.
        public void SetBlend(float w) => blend = Mathf.Clamp01(w);

        /// Schedule a pulse at a local time (Time.realtimeSinceStartupAsDouble seconds).
        public void ScheduleBeat(double localTime)
        {
            if (frozen) return;
            double now = Time.realtimeSinceStartupAsDouble;
            if (localTime < now) return;                                    // already late: drop
            if (beats.Count > 0 && localTime - beats[beats.Count - 1] < MinBeatGap) return;
            if (localTime - beatStart < MinBeatGap) return;
            beats.Add(localTime);
        }

        void Update()
        {
            if (!frozen)
            {
                double now = Time.realtimeSinceStartupAsDouble;
                while (beats.Count > 0 && beats[0] <= now)
                {
                    double s = beats[0];
                    beats.RemoveAt(0);
                    if (now - s <= LateOnset) { beatStart = s; BeatCount++; }  // envelopes never add
                }
                env = Envelope((float)(now - beatStart));

                float dt = Mathf.Min(Time.unscaledDeltaTime, 0.1f);        // no catch-up jumps
                drift += Mathf.Lerp(previous.motion, current.motion, blend) * dt;
            }
            Push();
        }

        static float Envelope(float x)
        {
            if (x < 0f || x >= Rise + Fall) return 0f;
            if (x < Rise) return 0.5f - 0.5f * Mathf.Cos(Mathf.PI * x / Rise);
            return 0.5f + 0.5f * Mathf.Cos(Mathf.PI * (x - Rise) / Fall);
        }

        void Push()
        {
            FieldTokens a = Clamp(previous), b = Clamp(current);
            sky.SetVector("_A0", new Vector4(a.fog, a.light, a.hue, a.sat));
            sky.SetVector("_B0", new Vector4(a.horizon, a.pulse, a.motion, 1f - blend));
            sky.SetVector("_A1", new Vector4(b.fog, b.light, b.hue, b.sat));
            sky.SetVector("_B1", new Vector4(b.horizon, b.pulse, b.motion, blend));
            sky.SetFloat("_Env", env);
            // one drift unit = one view height, wrapped to the 40-pixel stripe period (as the web renderer)
            float px = drift * RefHeight;
            sky.SetFloat("_DriftPx", ((px % 40f) + 40f) % 40f);
            sky.SetVector("_Size", new Vector4(RefWidth, RefHeight, 0f, 0f));
            sky.SetVector("_Fov", new Vector4(viewWidthDegrees, viewHeightDegrees, 0f, 0f));

            // Unity fog + light, for any real objects (the attention task) later
            float fog = Mathf.Lerp(previous.fog, current.fog, blend);
            float light = Mathf.Lerp(previous.light, current.light, blend);
            RenderSettings.fog = true;
            RenderSettings.fogMode = FogMode.Exponential;
            RenderSettings.fogDensity = fog * 0.25f;
            RenderSettings.fogColor = Color.HSVToRGB(Mathf.Repeat(current.hue, 360f) / 360f,
                                                     current.sat * 0.45f, 0.30f + 0.44f * light + 0.2f);
            if (diffuseLight) diffuseLight.intensity = light;
        }

        // Global token limits (experience-script §4), same as web/shared/field_renderer.js
        static FieldTokens Clamp(FieldTokens t) => new FieldTokens(
            Mathf.Clamp(t.fog, .18f, .62f), Mathf.Clamp(t.light, .35f, 1.10f), Mathf.Clamp(t.hue, 18f, 216f),
            Mathf.Clamp(t.sat, .08f, .36f), Mathf.Clamp(t.horizon, .38f, .62f), Mathf.Clamp(t.pulse, 0f, MaxPulse),
            Mathf.Clamp(t.motion, .004f, .045f));
    }

    /// Host state → seven tokens. Ported from web/shared/field_mapping.js (docs/field-reference.md).
    public static class PrismMapping
    {
        static float L(float a, float b, float x) => Mathf.Lerp(a, b, Mathf.Clamp01(x));
        static float R(float hr) => Mathf.Clamp01((hr - 62f) / 33f);

        /// B = baseline confidence 0–1. hr <= 0 means unknown (no pulse).
        public static FieldTokens Baseline(float B, float hr) => new FieldTokens(
            L(.38f, .26f, B), L(.45f, .62f, B), 208f, .12f, L(.44f, .50f, B),
            hr > 0 ? L(.06f, .10f, R(hr)) : 0f, .008f);

        /// A, C = effective arousal and cognitive load (0.5 + (psv − 0.5) × authority).
        public static FieldTokens Load(float A, float C, float hr) => new FieldTokens(
            L(.20f, .30f, C), L(.70f, 1.05f, A), L(214f, 202f, A), L(.16f, .30f, A), .50f,
            hr > 0 ? L(.10f, .16f, R(hr)) : 0f, L(.010f, .004f, C));

        public static FieldTokens Regulate(float A, float C, float hr) => new FieldTokens(
            L(.58f, .34f, A), L(.38f, .95f, A), L(24f, 46f, A), L(.14f, .30f, C), L(.44f, .56f, C),
            hr > 0 ? L(.08f, .16f, R(hr)) : 0f, L(.008f, .030f, A));

        /// ease 0 → 1 from regulate's last tokens to the fixed resolve look.
        public static FieldTokens Resolve(FieldTokens entry, float ease, float remainingMs)
        {
            float fade = Mathf.Cos(Mathf.PI / 2f * Mathf.Clamp01((3000f - remainingMs) / 3000f));
            return new FieldTokens(
                L(entry.fog, .44f, ease), L(entry.light, .50f, ease), L(entry.hue, 30f, ease),
                L(entry.sat, .14f, ease), L(.44f, .58f, (20000f - remainingMs) / 8000f),
                remainingMs <= 0f ? 0f : .08f * fade, L(entry.motion, .006f, ease));
        }
    }
}
