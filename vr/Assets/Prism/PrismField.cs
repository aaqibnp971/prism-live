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
        const float MaxPulse = (float)PrismPulseSafety.MaxAmplitude;

        Material sky;
        FieldTokens current, previous;
        float blend = 1f;                   // weight of `current`
        float drift;
        // Every decision about the pulse lives in PrismPulseSafety.cs, where it is tested.
        readonly PrismPulseView pulse = new PrismPulseView();

        public FieldTokens Current => current;

        /// The palette being dissolved away since the last segment change, and how far the
        /// dissolve has gone (0 = all Previous, 1 = all Current). Another world that takes its
        /// colour from the field must mix these two, never read Current's hue alone.
        public FieldTokens Previous => previous;
        public float Blend => blend;

        /// Goes up by one each time a heartbeat pulse starts.
        public int BeatCount => pulse.BeatCount;

        /// Current heartbeat envelope 0..1.
        public float PulseEnvelope => (float)pulse.Envelope;

        /// The pulse amplitude actually allowed on screen now: the state's amplitude capped by
        /// the active beat's own rate, 0 when there is no beat. Other visuals must use this,
        /// never Current.pulse.
        public float PulseAmplitude => (float)pulse.Amplitude;

        /// Amplitude times envelope, at most 0.22: the only number a shader may brighten by.
        public float PulseGain => (float)pulse.Gain;

        /// 0..1 taper of the beat on screen, 0 when no pulse is allowed (PrismHaptics).
        public float PulseTaper => (float)pulse.Taper;

        /// The beat gate's refusal counts for the current session, to read only (PrismHaptics' record).
        public PrismBeatGate Gate => pulse.Gate;

        /// Freeze the last picture (connection lost). Clears future beats.
        public bool Frozen
        {
            get => pulse.Frozen;
            set => pulse.Frozen = value;
        }

        /// Tell the pulse what the laptop's latest state says: session, segment, time left in
        /// the segment and that state's own pulse amplitude before any easing.
        public void SetPulseContext(string session, string segment, float remainingMs, float statePulse,
                                    float stateHeartRate = float.NaN) =>
            pulse.OnState(session, segment, remainingMs, statePulse, stateHeartRate);

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

        /// Offer one beat message for a pulse at a local time (Time.realtimeSinceStartupAsDouble
        /// seconds). Late, rejected, duplicate, wrong-session and too-fast beats never flash.
        public bool ScheduleBeat(string session, long seq, string quality, double rrMs, double hrBpm,
                                 double localTime)
        {
            double nowMs = Time.realtimeSinceStartupAsDouble * 1000.0;
            return pulse.OnBeat(session, seq, quality, rrMs, hrBpm, localTime * 1000.0, nowMs);
        }

        void Update()
        {
            if (!pulse.Frozen)
            {
                pulse.Tick(Time.realtimeSinceStartupAsDouble * 1000.0, current.pulse);

                float dt = Mathf.Min(Time.unscaledDeltaTime, 0.1f);        // no catch-up jumps
                drift += Mathf.Lerp(previous.motion, current.motion, blend) * dt;
            }
            Push();
        }

        void Push()
        {
            FieldTokens a = Clamp(previous), b = Clamp(current);
            // One capped amplitude for both palettes: a dissolve never keeps the old segment's pulse.
            a.pulse = b.pulse = Mathf.Clamp(PulseAmplitude, 0f, MaxPulse);
            sky.SetVector("_A0", new Vector4(a.fog, a.light, a.hue, a.sat));
            sky.SetVector("_B0", new Vector4(a.horizon, a.pulse, a.motion, 1f - blend));
            sky.SetVector("_A1", new Vector4(b.fog, b.light, b.hue, b.sat));
            sky.SetVector("_B1", new Vector4(b.horizon, b.pulse, b.motion, blend));
            sky.SetFloat("_Env", PulseEnvelope);
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
        static FieldTokens Clamp(FieldTokens t) => Limits(t);

        public static FieldTokens Limits(FieldTokens t) => new FieldTokens(
            Mathf.Clamp(t.fog, .18f, .62f), Mathf.Clamp(t.light, .35f, 1.10f), Mathf.Clamp(t.hue, 18f, 216f),
            Mathf.Clamp(t.sat, .08f, .36f), Mathf.Clamp(t.horizon, .38f, .62f), Mathf.Clamp(t.pulse, 0f, MaxPulse),
            Mathf.Clamp(t.motion, .004f, .045f));
    }

    /// Host state → seven tokens. Ported from web/shared/field_mapping.js (docs/field-reference.md).
    public static class PrismMapping
    {
        static float L(float a, float b, float x) => Mathf.Lerp(a, b, Mathf.Clamp01(x));
        // Pulse amplitude: the authored 62..95 bpm range, then the taper to zero at 120 bpm.
        static float P(string segment, float hr) => (float)PrismPulseSafety.PulseAmplitude(segment, hr, 0.0);

        /// B = baseline confidence 0–1. hr <= 0 means unknown (no pulse).
        public static FieldTokens Baseline(float B, float hr) => new FieldTokens(
            L(.38f, .26f, B), L(.45f, .62f, B), 208f, .12f, L(.44f, .50f, B),
            P("baseline", hr), .008f);

        /// A, C = effective arousal and cognitive load (0.5 + (psv − 0.5) × authority).
        public static FieldTokens Load(float A, float C, float hr) => new FieldTokens(
            L(.20f, .30f, C), L(.70f, 1.05f, A), L(214f, 202f, A), L(.16f, .30f, A), .50f,
            P("load", hr), L(.010f, .004f, C));

        public static FieldTokens Regulate(float A, float C, float hr) => new FieldTokens(
            L(.58f, .34f, A), L(.38f, .95f, A), L(24f, 46f, A), L(.14f, .30f, C), L(.44f, .56f, C),
            P("regulate", hr), L(.008f, .030f, A));

        /// ease 0 → 1 from regulate's last tokens to the fixed resolve look.
        public static FieldTokens Resolve(FieldTokens entry, float ease, float remainingMs)
        {
            // No heart rate is known here, so no pulse: PrismLink.Resolve is the path in use.
            return new FieldTokens(
                L(entry.fog, .44f, ease), L(entry.light, .50f, ease), L(entry.hue, 30f, ease),
                L(entry.sat, .14f, ease), L(.44f, .58f, (20000f - remainingMs) / 8000f),
                0f, L(entry.motion, .006f, ease));
        }
    }
}
