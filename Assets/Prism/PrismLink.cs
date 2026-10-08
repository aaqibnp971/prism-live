using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace Prism
{
    [Serializable] public class Dims { public float arousal, valence, cognitive_load, readiness; }

    /// Every field any laptop message can carry (message contract v1). Unused ones stay default.
    [Serializable]
    public class LinkMsg
    {
        public string type, session, quality, segment, role;
        public int v;
        public double t_play, t_engine, t_client_sent, segment_elapsed_ms, segment_nominal_ms;
        public float rr_ms, hr_bpm;
        public Dims psv, confidence, authority;
    }

    /// Connects to the laptop (ws://<ip>:8787/live), follows its state and heartbeat.
    /// Never decides anything itself: segment, timing and authority all come from the laptop.
    /// If the link drops, the picture freezes and a small red dot appears below your view.
    [RequireComponent(typeof(PrismField))]
    public class PrismLink : MonoBehaviour
    {
        [Tooltip("The laptop's WiFi IPv4 address, from ipconfig (e.g. 192.168.1.20)")]
        public string laptopIp = "192.168.1.20";
        public int port = 8787;

        [Header("Offline preview (testing only)")]
        [Tooltip("Plays a pretend 4-minute session with no laptop, so you can see the colours, " +
                 "countdown and game in Unity Play mode or on the Quest. Untick for real sessions.")]
        public bool offlinePreview = false;
        [Range(1f, 4f)] public float previewSpeed = 1f;

        [Header("Read-only, for watching in the Inspector")]
        public string status = "starting";
        public string segment = "-";
        public float heartRate;
        public float clockOffsetMs;

        /// Raised on the main thread for every state message (the attention task listens).
        public event Action<LinkMsg> StateReceived;
        public bool IsOpen => open;
        public static double ClientNowMs => Time.realtimeSinceStartupAsDouble * 1000.0;

        /// Send one JSON message to the laptop (used for task_event).
        public void Send(string json) => SendText(json);

        async void SendText(string json)
        {
            var socket = ws;
            if (socket == null || socket.State != WebSocketState.Open) return;
            var bytes = new ArraySegment<byte>(Encoding.UTF8.GetBytes(json));
            await sendLock.WaitAsync();
            try { await socket.SendAsync(bytes, WebSocketMessageType.Text, true, cts.Token); }
            catch (Exception) { }
            finally { sendLock.Release(); }
        }

        const float EaseSeconds = 0.25f;   // smooth the 2 s state steps, never extrapolate

        PrismField field;
        ClientWebSocket ws;                               // built into .NET, no extra package
        CancellationTokenSource cts;
        readonly SemaphoreSlim sendLock = new SemaphoreSlim(1, 1);
        bool open;
        float nextPing, reconnectAt = -1f;
        GameObject marker;

        // clock sync (contract §2 clock): median of last 9, discard rtt > max(2 × median rtt, 4 ms)
        readonly List<double> rtts = new List<double>(), offsets = new List<double>();
        bool haveOffset;
        double offset;

        // state
        string lastSegment = "", lastSession = "";
        float lastAuthA, lastAuthC, entryAuthA, entryAuthC;
        FieldTokens resolveEntry;
        bool haveResolveEntry;

        // easing toward each new report
        FieldTokens fromTokens, toTokens;
        float fromBlend, toBlend = 1f, easeStart = -10f;

        string Url => $"ws://{laptopIp}:{port}/live";
        static double NowMs => Time.realtimeSinceStartupAsDouble * 1000.0;

        void Start()
        {
            field = GetComponent<PrismField>();
            fromTokens = toTokens = field.Current;
            MakeMarker();
            if (offlinePreview)
            {
                open = true;
                field.Frozen = false;
                status = "OFFLINE PREVIEW (not connected to the laptop)";
            }
            else Connect();
        }

        async void Connect()
        {
            rtts.Clear(); offsets.Clear(); haveOffset = false;   // an estimate belongs to one connection
            status = "connecting to " + Url;
            cts?.Cancel();
            cts = new CancellationTokenSource();
            ws = new ClientWebSocket();
            var socket = ws;

            try { await socket.ConnectAsync(new Uri(Url), cts.Token); }
            catch (Exception e) { status = "error: " + e.Message; Lost(); return; }

            open = true;
            field.Frozen = false;
            status = "connected";
            SendText("{\"type\":\"hello\",\"v\":1,\"client\":\"quest\",\"build\":\"" + Application.version + "\"}");
            SendPing();

            // receive loop; awaits resume on Unity's main thread, so Handle runs there
            var buffer = new byte[64 * 1024];
            var text = new StringBuilder();
            try
            {
                while (socket.State == WebSocketState.Open)
                {
                    var r = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), cts.Token);
                    if (r.MessageType == WebSocketMessageType.Close) break;
                    text.Append(Encoding.UTF8.GetString(buffer, 0, r.Count));
                    if (r.EndOfMessage) { Handle(text.ToString()); text.Clear(); }
                }
            }
            catch (Exception e) { status = "error: " + e.Message; }
            Lost();
        }

        void Lost()
        {
            if (reconnectAt > 0f) return;
            open = false;
            field.Frozen = true;                          // freeze the last picture, drop future beats
            if (!status.StartsWith("error")) status = "disconnected";
            reconnectAt = Time.unscaledTime + 2f;
        }

        void Update()
        {
            if (offlinePreview) Preview();
            if (marker) marker.SetActive(!open);

            if (open && !offlinePreview && Time.unscaledTime >= nextPing) SendPing();

            if (!open && reconnectAt > 0f && Time.unscaledTime >= reconnectAt)
            {
                reconnectAt = -1f;
                Connect();
            }

            float k = Mathf.Clamp01((Time.unscaledTime - easeStart) / EaseSeconds);
            field.SetTokens(LerpTokens(fromTokens, toTokens, k));
            field.SetBlend(Mathf.Lerp(fromBlend, toBlend, k));
        }

        void SendPing()
        {
            nextPing = Time.unscaledTime + 2f;
            SendText("{\"type\":\"clock\",\"v\":1,\"role\":\"ping\",\"t_client_sent\":" +
                        NowMs.ToString("F3", CultureInfo.InvariantCulture) + "}");
        }

        void Handle(string json)
        {
            LinkMsg m;
            try { m = JsonUtility.FromJson<LinkMsg>(json.Replace(":null", ":0").Replace(": null", ": 0")); }
            catch { return; }
            if (m == null || m.v != 1) return;

            switch (m.type)
            {
                case "clock": if (m.role == "pong") Pong(m); break;
                case "beat": Beat(m); break;
                case "state": State(m); StateReceived?.Invoke(m); break;
            }
        }

        void Pong(LinkMsg m)
        {
            double now = NowMs;
            double rtt = now - m.t_client_sent;
            if (rtt < 0) return;

            rtts.Add(rtt);
            if (rtts.Count > 9) rtts.RemoveAt(0);
            if (rtt > Math.Max(2.0 * Median(rtts), 4.0)) return;

            offsets.Add(m.t_engine + rtt / 2.0 - now);
            if (offsets.Count > 9) offsets.RemoveAt(0);
            offset = Median(offsets);
            haveOffset = true;
            clockOffsetMs = (float)offset;
        }

        void Beat(LinkMsg m)
        {
            if (!haveOffset || m.quality == "rejected") return;
            double tLocalMs = m.t_play - offset;          // t_play on the laptop clock → our clock
            field.ScheduleBeat(tLocalMs / 1000.0);        // dropped inside if already late
        }

        void State(LinkMsg m)
        {
            segment = m.segment;
            heartRate = m.hr_bpm;

            if (m.session != lastSession) { lastSession = m.session; haveResolveEntry = false; }

            if (m.segment != lastSegment)
            {
                if (m.segment == "resolve" && lastSegment == "regulate")
                {
                    resolveEntry = toTokens;
                    entryAuthA = lastAuthA;
                    entryAuthC = lastAuthC;
                    haveResolveEntry = true;
                }
                if (lastSegment != "")
                {
                    field.SetTokens(toTokens);
                    field.BeginDissolve();
                    fromTokens = toTokens;
                    fromBlend = 0f;
                    toBlend = 0f;
                }
                lastSegment = m.segment;
            }

            float aA = m.authority != null ? m.authority.arousal : 0f;
            float aC = m.authority != null ? m.authority.cognitive_load : 0f;
            float A = 0.5f + ((m.psv != null ? m.psv.arousal : 0.5f) - 0.5f) * aA;
            float C = 0.5f + ((m.psv != null ? m.psv.cognitive_load : 0.5f) - 0.5f) * aC;
            float hr = m.hr_bpm;                          // null arrives as 0 = unknown = no pulse

            FieldTokens t;
            switch (m.segment)
            {
                case "baseline":
                    float mean = m.confidence == null ? 0f
                        : (m.confidence.arousal + m.confidence.cognitive_load + m.confidence.readiness) / 3f;
                    t = PrismMapping.Baseline(Mathf.Clamp01(mean / 0.393f), hr);
                    break;
                case "load":
                    t = PrismMapping.Load(A, C, hr);
                    break;
                case "regulate":
                    t = PrismMapping.Regulate(A, C, hr);
                    lastAuthA = aA; lastAuthC = aC;
                    break;
                case "resolve":
                    t = Resolve(aA, aC, hr, (float)(m.segment_nominal_ms - m.segment_elapsed_ms));
                    break;
                default:                                   // idle / reset: calm, no pulse
                    t = PrismMapping.Baseline(0f, 0f);
                    break;
            }

            // ease from what is on screen now to the new report over 250 ms
            float k = Mathf.Clamp01((Time.unscaledTime - easeStart) / EaseSeconds);
            fromTokens = LerpTokens(fromTokens, toTokens, k);
            fromBlend = Mathf.Lerp(fromBlend, toBlend, k);
            toTokens = t;
            toBlend = Mathf.Clamp01((float)(m.segment_elapsed_ms / 10000.0));
            easeStart = Time.unscaledTime;
        }

        FieldTokens Resolve(float aA, float aC, float hr, float remainingMs)
        {
            float rA = haveResolveEntry && entryAuthA > 0f ? Mathf.Clamp01(aA / entryAuthA) : 0f;
            float rC = haveResolveEntry && entryAuthC > 0f ? Mathf.Clamp01(aC / entryAuthC) : 0f;
            FieldTokens e = haveResolveEntry ? resolveEntry : new FieldTokens(.44f, .50f, 30f, .14f, .44f, 0f, .006f);

            float fade = Mathf.Cos(Mathf.PI / 2f * Mathf.Clamp01((3000f - remainingMs) / 3000f));
            return new FieldTokens(
                .44f + (e.fog - .44f) * rA,
                .50f + (e.light - .50f) * rA,
                30f + (e.hue - 30f) * rA,
                .14f + (e.sat - .14f) * rC,
                Mathf.Lerp(.44f, .58f, Mathf.Clamp01((20000f - remainingMs) / 8000f)),
                hr > 0f && remainingMs > 0f ? .08f * fade : 0f,
                .006f + (e.motion - .006f) * rA);
        }

        static FieldTokens LerpTokens(FieldTokens a, FieldTokens b, float k) => new FieldTokens(
            Mathf.Lerp(a.fog, b.fog, k), Mathf.Lerp(a.light, b.light, k), Mathf.Lerp(a.hue, b.hue, k),
            Mathf.Lerp(a.sat, b.sat, k), Mathf.Lerp(a.horizon, b.horizon, k),
            Mathf.Lerp(a.pulse, b.pulse, k), Mathf.Lerp(a.motion, b.motion, k));

        // ---- offline preview: a pretend session, same path as real laptop messages ----
        static readonly string[] PSeg = { "idle", "baseline", "load", "regulate", "resolve" };
        static readonly float[] PLen = { 4f, 45f, 75f, 75f, 45f };
        float pClock, pNextState;
        int pSession;
        double pNextBeat;

        void Preview()
        {
            pClock += Time.deltaTime * previewSpeed;
            float total = 0f; foreach (var l in PLen) total += l;
            if (pClock >= total) { pClock -= total; pSession++; }

            int i = 0; float t = pClock;
            while (t >= PLen[i]) { t -= PLen[i]; i++; }
            float p = t / PLen[i];

            float hr = 72f, A = 0.5f, C = 0.5f, auth = 0f, conf = 0f;
            switch (PSeg[i])
            {
                case "baseline": conf = Mathf.SmoothStep(0f, 0.45f, p); break;
                case "load": hr = Mathf.Lerp(72f, 96f, p); A = Mathf.Lerp(0.55f, 0.92f, p); C = Mathf.Lerp(0.45f, 0.9f, p); auth = Mathf.Clamp01(p * 4f); break;
                case "regulate": hr = Mathf.Lerp(96f, 64f, p); A = Mathf.Lerp(0.85f, 0.2f, Mathf.SmoothStep(0f, 1f, p)); C = Mathf.Lerp(0.8f, 0.3f, p); auth = 1f; break;
                case "resolve": hr = 64f; A = 0.3f; C = 0.3f; auth = Mathf.Clamp01(1f - p * 1.4f); break;
                case "idle": hr = 0f; break;
            }

            if (Time.unscaledTime >= pNextState)
            {
                pNextState = Time.unscaledTime + 0.5f / previewSpeed;
                var m = new LinkMsg
                {
                    type = "state", v = 1, session = "PREVIEW-" + pSession, segment = PSeg[i],
                    segment_elapsed_ms = t * 1000.0, segment_nominal_ms = PLen[i] * 1000.0, hr_bpm = hr,
                    psv = new Dims { arousal = A, cognitive_load = C, valence = 0.5f, readiness = 0.5f },
                    confidence = new Dims { arousal = conf, cognitive_load = conf, valence = conf, readiness = conf },
                    authority = new Dims { arousal = auth, cognitive_load = auth, valence = auth, readiness = auth },
                };
                State(m);
                StateReceived?.Invoke(m);
            }

            // heartbeat, in real time, scheduled a little ahead like the laptop does
            double now = Time.realtimeSinceStartupAsDouble;
            if (hr <= 0f) { pNextBeat = now + 0.3; return; }
            if (pNextBeat < now) pNextBeat = now + 0.3;
            if (pNextBeat - now <= 0.3) { field.ScheduleBeat(pNextBeat); pNextBeat += 60.0 / hr; }
        }

        static double Median(List<double> xs)
        {
            var s = new List<double>(xs);
            s.Sort();
            int n = s.Count;
            return n == 0 ? 0 : (n % 2 == 1 ? s[n / 2] : 0.5 * (s[n / 2 - 1] + s[n / 2]));
        }

        void MakeMarker()
        {
            Camera cam = Camera.main;
            if (!cam)
            {
                var cams = FindObjectsByType<Camera>(FindObjectsSortMode.None);
                if (cams.Length > 0) cam = cams[0];
            }
            if (!cam) return;

            marker = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            marker.name = "Prism Disconnected Marker";
            Destroy(marker.GetComponent<Collider>());
            marker.transform.SetParent(cam.transform, false);
            marker.transform.localPosition = new Vector3(0f, -0.18f, 0.8f);
            marker.transform.localScale = Vector3.one * 0.025f;
            var mat = marker.GetComponent<Renderer>().material;
            mat.color = Color.red;
            mat.SetColor("_BaseColor", Color.red);
        }

        void OnDestroy()
        {
            try { cts?.Cancel(); ws?.Abort(); ws?.Dispose(); } catch (Exception) { }
        }
    }
}
