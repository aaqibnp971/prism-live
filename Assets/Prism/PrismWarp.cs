using System.Collections.Generic;
using UnityEngine;

namespace Prism
{
    /// The calm "travelling through a tunnel of light" world (inspired by the reference reel), driven by the
    /// Prism engine: the laptop's segment decides the pace, the live heart rate nudges the speed,
    /// the mood colour tints the light, and every heartbeat gently lifts the brightness.
    ///   baseline : steady, continuous calm drift
    ///   load     : a little faster and fewer streaks, so the game dot stands out
    ///   regulate : slows down and softens, to help the heart rate settle
    ///   resolve  : almost still, streaks fade away to finish
    /// Comfort: speeds are low, changes ease over seconds, and fixed stars give the eyes an anchor.
    [RequireComponent(typeof(PrismField))]
    public class PrismWarp : MonoBehaviour
    {
        public Shader warpShader, spaceShader;

        [Header("Tunnel")]
        public int streakCount = 1800;
        public float tunnelLength = 60f;
        [Tooltip("Radius of the tunnel wall, metres")]
        public float tunnelRadius = 4f;
        [Tooltip("How far streaks scatter in and out of the wall")]
        public float wallThickness = 1.4f;
        public float streakWidth = 0.014f;
        [Tooltip("Spiral twist along the tunnel (0 = straight lines)")]
        public float twist = 0.05f;
        [Tooltip("Slow roll of the whole tunnel, radians per second (keep small)")]
        public float spinSpeed = 0.04f;
        [Tooltip("How much the tunnel winds, metres (0 = straight)")]
        public float curveAmount = 2.2f;
        [Tooltip("How long each bend is (smaller = longer, gentler bends)")]
        public float curveFrequency = 0.045f;

        [Header("Rings")]
        public int ringCount = 12;
        public int ringSegments = 48;
        [Range(0f, 1f)] public float ringBrightness = 0.35f;

        [Header("Speeds, metres per second")]
        public float idleSpeed = 2.5f;
        public float baselineSpeed = 4f;
        public float loadSpeed = 5.5f;
        public float regulateStartSpeed = 4f, regulateEndSpeed = 1.5f;
        public float resolveSpeed = 0.6f;
        [Tooltip("Hard ceiling. Lower it if anyone feels queasy.")]
        public float maxSpeed = 7f;
        [Tooltip("How gently changes happen, seconds")]
        public float easeSeconds = 3f;

        [Header("Read-only, for watching")]
        public string segment = "idle";
        public float speed, density, brightness;

        const float Behind = 12f;

        PrismField field;
        PrismLink link;
        Material warpMat, spaceMat;
        float travel, spin;
        double segElapsedMs, segNominalMs;
        float segStamp;

#if UNITY_EDITOR
        void Reset() => OnValidate();
        void OnValidate()
        {
            if (!warpShader) warpShader = Shader.Find("Prism/Warp");
            if (!spaceShader) spaceShader = Shader.Find("Prism/Space");
        }
#endif

        void Start()
        {
            field = GetComponent<PrismField>();
            link = GetComponent<PrismLink>();
            if (link) link.StateReceived += OnState;

            if (!warpShader || !spaceShader)
            {
                Debug.LogError("[Prism] Warp: PrismWarp.shader / PrismSpace.shader missing from Assets.");
                enabled = false;
                return;
            }
            warpMat = new Material(warpShader);
            spaceMat = new Material(spaceShader);
            RenderSettings.skybox = spaceMat;          // replaces the fog field as the background

            BuildMesh();
            speed = idleSpeed; density = 0.6f; brightness = 0.6f;
            Debug.Log("[Prism] Warp running.");
        }

        void OnDestroy() { if (link) link.StateReceived -= OnState; }

        void OnState(LinkMsg m)
        {
            segment = string.IsNullOrEmpty(m.segment) ? "idle" : m.segment;
            segElapsedMs = m.segment_elapsed_ms;
            segNominalMs = m.segment_nominal_ms;
            segStamp = Time.unscaledTime;
        }

        float Progress()
        {
            if (segNominalMs <= 0) return 0f;
            double el = segElapsedMs + (Time.unscaledTime - segStamp) * 1000.0;
            return Mathf.Clamp01((float)(el / segNominalMs));
        }

        void Update()
        {
            float p = Progress();
            float s, d, b;
            switch (segment)
            {
                case "baseline": s = baselineSpeed; d = 1f; b = 0.8f; break;
                case "load": s = loadSpeed; d = 0.6f; b = 1f; break;
                case "regulate":
                    s = Mathf.Lerp(regulateStartSpeed, regulateEndSpeed, p);
                    d = Mathf.Lerp(0.9f, 0.6f, p); b = Mathf.Lerp(0.8f, 0.5f, p); break;
                case "resolve":
                    s = Mathf.Lerp(resolveSpeed * 3f, resolveSpeed, p);
                    d = Mathf.Lerp(0.6f, 0f, Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(0.4f, 1f, p)));
                    b = Mathf.Lerp(0.5f, 0.2f, p); break;
                default: s = idleSpeed; d = 0.6f; b = 0.6f; break;
            }

            // the live heart rate nudges the pace (only while a session is running)
            if (link && link.heartRate > 0f && (segment == "baseline" || segment == "load" || segment == "regulate"))
                s *= Mathf.Lerp(0.85f, 1.25f, Mathf.InverseLerp(60f, 100f, link.heartRate));

            s = Mathf.Min(s, maxSpeed);
            if (field.Frozen && segment != "idle") s = Mathf.Min(s, idleSpeed);   // link lost mid-session: calm drift, never a stop

            float dt = Mathf.Min(Time.deltaTime, 0.1f);
            float k = 1f - Mathf.Exp(-dt / Mathf.Max(0.1f, easeSeconds));
            speed += (s - speed) * k;
            density += (d - density) * k;
            brightness += (b - brightness) * k;

            float span = tunnelLength + Behind;
            travel = Mathf.Repeat(travel + speed * dt, span);
            spin = Mathf.Repeat(spin + spinSpeed * dt * Mathf.Clamp01(speed / Mathf.Max(0.1f, baselineSpeed)), Mathf.PI * 2f);

            FieldTokens c = field.Current;
            float h = Mathf.Repeat(c.hue, 360f) / 360f;
            Color tint = Color.HSVToRGB(h, Mathf.Clamp01(c.sat * 0.6f), 1f).linear;

            warpMat.SetFloat("_Travel", travel);
            warpMat.SetFloat("_Span", span);
            warpMat.SetFloat("_Far", tunnelLength);
            warpMat.SetFloat("_Len", 0.3f + speed * 0.45f);        // faster = longer streaks
            warpMat.SetFloat("_Width", streakWidth);
            warpMat.SetFloat("_Density", density);
            warpMat.SetFloat("_Env", field.PulseEnvelope);
            warpMat.SetFloat("_Pulse", c.pulse);
            warpMat.SetFloat("_Twist", twist);
            warpMat.SetFloat("_Spin", spin);
            warpMat.SetFloat("_CurveAmp", curveAmount);
            warpMat.SetFloat("_CurveK", curveFrequency);
            warpMat.SetFloat("_RingArc", Mathf.PI * 2f / Mathf.Max(8, ringSegments) * 0.7f);
            warpMat.SetFloat("_RingBright", ringBrightness);
            warpMat.SetVector("_Color", new Vector4(tint.r, tint.g, tint.b, 1f) * (0.8f * brightness));

            spaceMat.SetVector("_Tint", new Vector4(tint.r, tint.g, tint.b, 1f));
            spaceMat.SetFloat("_Bright", brightness);
            spaceMat.SetFloat("_Env", field.PulseEnvelope);
            spaceMat.SetFloat("_Pulse", c.pulse);
        }

        void BuildMesh()
        {
            int ns = Mathf.Max(1, streakCount);
            int nr = Mathf.Max(0, ringCount) * Mathf.Max(8, ringSegments);
            int n = ns + nr;
            var verts = new Vector3[n * 4];
            var uv = new List<Vector2>(n * 4);
            var prm = new List<Vector4>(n * 4);
            var kind = new List<Vector2>(n * 4);
            var tris = new int[n * 6];
            var rng = new System.Random(1234);

            System.Action<int, Vector4, float> Add = (i, p, k) =>
            {
                int v = i * 4;
                uv.Add(new Vector2(-1f, 0f)); uv.Add(new Vector2(1f, 0f));
                uv.Add(new Vector2(-1f, 1f)); uv.Add(new Vector2(1f, 1f));
                for (int j = 0; j < 4; j++) { prm.Add(p); kind.Add(new Vector2(k, 0f)); }
                int t = i * 6;
                tris[t] = v; tris[t + 1] = v + 2; tris[t + 2] = v + 1;
                tris[t + 3] = v + 1; tris[t + 4] = v + 2; tris[t + 5] = v + 3;
            };

            // streaks on the tunnel wall (a few scattered deeper for depth)
            for (int i = 0; i < ns; i++)
            {
                float th = (float)(rng.NextDouble() * Mathf.PI * 2.0);
                float jitter = (float)(rng.NextDouble() + rng.NextDouble() - 1.0);   // -1..1, centred
                float r = Mathf.Max(1.4f, tunnelRadius + jitter * wallThickness);
                if (rng.NextDouble() < 0.15) r = tunnelRadius + wallThickness + (float)rng.NextDouble() * 8f;
                Add(i, new Vector4(th, r, (float)rng.NextDouble(), (float)rng.NextDouble()), 0f);
            }

            // rings of short arcs, evenly spaced along the tunnel
            int segs = Mathf.Max(8, ringSegments);
            for (int ring = 0; ring < ringCount; ring++)
            {
                float z0 = (ring + 0.5f) / ringCount;
                for (int sgm = 0; sgm < segs; sgm++)
                {
                    float th = sgm * Mathf.PI * 2f / segs;
                    Add(ns + ring * segs + sgm, new Vector4(th, tunnelRadius, z0, (float)rng.NextDouble()), 1f);
                }
            }

            var mesh = new Mesh { name = "Prism Streaks" };
            mesh.indexFormat = UnityEngine.Rendering.IndexFormat.UInt32;
            mesh.vertices = verts;
            mesh.SetUVs(0, uv);
            mesh.SetUVs(1, prm);
            mesh.SetUVs(2, kind);
            mesh.triangles = tris;
            mesh.bounds = new Bounds(Vector3.zero, Vector3.one * (tunnelLength * 3f + curveAmount * 4f));   // never culled

            var go = new GameObject("Prism Streaks");
            // centred on where the person's head starts, facing the same way as the rig
            Camera cam = Camera.main;
            go.transform.position = cam ? cam.transform.position : Vector3.zero;
            go.AddComponent<MeshFilter>().sharedMesh = mesh;
            var mr = go.AddComponent<MeshRenderer>();
            mr.sharedMaterial = warpMat;
            mr.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            mr.receiveShadows = false;
        }
    }
}
