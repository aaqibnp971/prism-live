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
        public int v, seq;
        public double t_play, t_engine, t_client_sent, segment_elapsed_ms, segment_nominal_ms;
        public float rr_ms, hr_bpm;
        public Dims psv, confidence, authority;
    }

    /// Connects to the laptop (ws://<ip>:8787/live), follows its state and heartbeat.
    /// Never decides anything itself: segment, timing and authority all come from the laptop.
    /// If the link drops, the picture freezes and a small red dot appears below your view.
    /// The red dot shows whenever the headset is not following the laptop, Offline Preview
    /// included, and Offline Preview is labelled in view for as long as it runs.
    /// Every change of link status is logged: adb logcat -s Unity | findstr PrismLink
    [RequireComponent(typeof(PrismField))]
    public class PrismLink : MonoBehaviour
    {
        [Tooltip("The laptop's address built into the app: 192.168.137.1 is the laptop's own " +
                 "Windows Mobile Hotspot. On the headset a laptop-ip.txt pushed over USB replaces " +
                 "it without a rebuild (see PrismLaptopAddress.cs).")]
        public string laptopIp = "192.168.137.1";
        public int port = 8787;

        [Header("Offline preview (testing only)")]
        [Tooltip("Plays a pretend 4-minute session with no laptop, so you can see the colours, " +
                 "countdown and game in Unity Play mode or on the Quest. Untick for real sessions.")]
        public bool offlinePreview = false;
        [Range(1f, 4f)] public float previewSpeed = 1f;
        [Tooltip("Prism/Marker, for the red not-linked dot. Set automatically in the Editor.")]
        public Shader markerShader;

        [Header("Read-only, for watching in the Inspector")]
        public string status = "starting";
        public string segment = "-";
        public float heartRate;
        public float clockOffsetMs;

        /// Raised on the main thread for every state message (the attention task listens).
        public event Action<LinkMsg> StateReceived;
        /// True while states are arriving: from the laptop, or from Offline Preview's pretend
        /// session. The attention task runs on this.
        public bool IsOpen => open && !stale;
        /// True only while a real laptop is connected and still sending states.
        public bool IsLinked => PrismLinkStatus.Linked(offlinePreview, open, haveState, stale);
        public static double ClientNowMs => Time.realtimeSinceStartupAsDouble * 1000.0;
        /// How long ago the laptop stamped this state, on the synced clock; NaN before the
        /// clock is known. A state delayed on the way (a WiFi stall) shows its age here.
        public double StateAgeMs(LinkMsg m) => haveOffset && m != null ? NowMs + offset - m.t_engine : double.NaN;

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
        // The laptop sends a state every 2 s. A socket can stay open while nothing arrives (WiFi
        // dropped, laptop stalled), so silence freezes the picture exactly as a lost link does,
        // as the browser screens do (2.5 s). A long silence drops the socket and reconnects.
        const float StateStaleSeconds = 2.5f, StaleReconnectSeconds = 8f;

        PrismField field;
        ClientWebSocket ws;                               // built into .NET, no extra package
        CancellationTokenSource cts;
        readonly SemaphoreSlim sendLock = new SemaphoreSlim(1, 1);
        bool open, stale;
        bool haveState;                                   // a state has arrived on this connection
        string build;
        float nextPing, reconnectAt = -1f, lastStateAt;
        GameObject marker, offlineLabel;
        string loggedStatus;

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

        // Decided once in Start: laptop-ip.txt, else Laptop Ip above, else the hotspot default.
        string address = PrismLaptopAddress.Default;
        string Url => $"ws://{address}:{port}/live";
        static double NowMs => Time.realtimeSinceStartupAsDouble * 1000.0;

#if UNITY_EDITOR
        void Reset() => OnValidate();
        void OnValidate() { if (!markerShader) markerShader = Shader.Find("Prism/Marker"); }
#endif

        void Start()
        {
            field = GetComponent<PrismField>();
            fromTokens = toTokens = field.Current;
            var stamp = Resources.Load<TextAsset>("PrismBuildStamp");
            build = PrismLinkStatus.BuildLabel(Application.version, stamp ? stamp.text : null);
            string addressFrom = ChooseAddress();
            Debug.Log($"[PrismLink] build {build}, scene " +
                      $"{UnityEngine.SceneManagement.SceneManager.GetActiveScene().name}, " +
                      $"offlinePreview={offlinePreview}, laptop {Url}");
            Debug.Log($"[PrismLink] laptop address {address} from {addressFrom}");
            if (offlinePreview)
            {
                open = true;
                field.Frozen = false;
                status = "OFFLINE PREVIEW (not connected to the laptop)";
            }
            else Connect();
            ShowLinkStatus();
        }

        /// Reads laptop-ip.txt from the app's own storage, if there is one, and settles the
        /// address for this run. No network is touched; any failure falls back.
        string ChooseAddress()
        {
            string text = null, problem = null, path = "";
            try
            {
                path = System.IO.Path.Combine(Application.persistentDataPath, PrismLaptopAddress.FileName);
                if (System.IO.File.Exists(path))
                {
                    // An address is a few bytes: read at most 257 and refuse anything longer,
                    // however large the file is or becomes while it is being read.
                    var bytes = new byte[4 * PrismLaptopAddress.MaxFileChars + 1];
                    int count = 0;
                    using (var stream = new System.IO.FileStream(path, System.IO.FileMode.Open,
                                                                 System.IO.FileAccess.Read, System.IO.FileShare.ReadWrite))
                    {
                        int read;
                        while (count < bytes.Length && (read = stream.Read(bytes, count, bytes.Length - count)) > 0)
                            count += read;
                    }
                    if (count == bytes.Length) problem = "too large";
                    else
                        using (var reader = new System.IO.StreamReader(new System.IO.MemoryStream(bytes, 0, count), true))
                            text = reader.ReadToEnd();       // honours a byte-order mark, as Notepad or PowerShell write
                }
            }
            catch (Exception e) { text = null; problem = e.GetType().Name; }

            PrismLaptopAddress.Choice choice =
                PrismLaptopAddress.Resolve(text, problem, laptopIp, Application.isEditor);
            address = choice.address;
            return choice.source + ", looked in " + path;
        }

        /// The red dot and the Offline Preview label, and one log line per change of status.
        void ShowLinkStatus()
        {
            if (!marker) MakeMarker();                    // the camera rig may appear after Start
            if (marker) marker.SetActive(PrismLinkStatus.ShowMarker(offlinePreview, open, haveState, stale));
            if (PrismLinkStatus.ShowOfflineLabel(offlinePreview) && !offlineLabel) MakeOfflineLabel();
            if (offlineLabel) offlineLabel.SetActive(PrismLinkStatus.ShowOfflineLabel(offlinePreview));
            if (status != loggedStatus)
            {
                loggedStatus = status;
                Debug.Log("[PrismLink] " + status);
            }
        }

        async void Connect()
        {
            rtts.Clear(); offsets.Clear(); haveOffset = false;   // an estimate belongs to one connection
            haveState = false;
            status = "connecting to " + Url;
            cts?.Cancel();
            cts = new CancellationTokenSource();
            ws = new ClientWebSocket();
            var socket = ws;

            try { await socket.ConnectAsync(new Uri(Url), cts.Token); }
            catch (Exception e) { status = "error: " + e.Message; Lost(); return; }

            open = true;                                  // the picture stays frozen until a state arrives
            stale = false;
            lastStateAt = Time.unscaledTime;
            status = "connected";
            SendText("{\"type\":\"hello\",\"v\":1,\"client\":\"quest\",\"build\":\"" + build + "\"}");
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
            // Drop the socket outright. Left half-closed after the laptop's close, the laptop
            // waited 10 s on it and refused this headset's reconnect as a second producer.
            try { socket.Abort(); } catch (Exception) { }
            if (socket != ws) return;                     // an older socket ending: not the link's news
            Lost();
        }

        void Lost()
        {
            if (reconnectAt > 0f) return;
            open = false;
            stale = false;
            haveState = false;
            field.Frozen = true;                          // freeze the last picture, drop future beats
            if (!status.StartsWith("error")) status = "disconnected";
            reconnectAt = Time.unscaledTime + 2f;
        }

        void Update()
        {
            if (offlinePreview) Preview();
            else if (open)
            {
                float quiet = Time.unscaledTime - lastStateAt;
                if (!stale && quiet > StateStaleSeconds)
                {
                    stale = true;                         // freeze, show the marker, pause the task
                    field.Frozen = true;
                    status = "frozen: no state from the laptop for " + StateStaleSeconds + " s";
                }
                if (stale && quiet > StaleReconnectSeconds)
                {
                    status = "error: the laptop went silent, reconnecting";
                    try { ws?.Abort(); } catch (Exception) { }
                    Lost();
                }
            }
            ShowLinkStatus();

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
            // Every second, not the contract's 2 s: the laptop reads its own "headset
            // connected" from these, with 2.5 s of silence meaning lost (contract v1.10).
            nextPing = Time.unscaledTime + 1f;
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
                case "state":
                    lastStateAt = Time.unscaledTime;
                    haveState = true;
                    if (stale) { stale = false; status = "connected"; }
                    State(m); StateReceived?.Invoke(m); break;
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
            if (!haveOffset) return;
            double tLocalMs = m.t_play - offset;          // t_play on the laptop clock → our clock
            // PrismField decides whether it may flash: late, rejected, duplicate, wrong-session
            // and too-fast beats never do (PrismPulseSafety.cs).
            field.ScheduleBeat(m.session, m.seq, m.quality, m.rr_ms, m.hr_bpm, tLocalMs / 1000.0);
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
            float remainingMs = (float)(m.segment_nominal_ms - m.segment_elapsed_ms);

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
                    t = Resolve(aA, aC, hr, remainingMs);
                    break;
                default:                                   // idle / reset: calm, no pulse
                    t = PrismMapping.Baseline(0f, 0f);
                    break;
            }

            // The pulse follows this state at once (its amplitude is not eased, so a drop is
            // never delayed), and only a fresh state ends a freeze: after a reconnect nothing
            // pulses on what was known before the link dropped.
            field.SetPulseContext(m.session, m.segment, remainingMs, t.pulse, hr);
            if (field.Frozen) field.Frozen = false;

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

            return new FieldTokens(
                .44f + (e.fog - .44f) * rA,
                .50f + (e.light - .50f) * rA,
                30f + (e.hue - 30f) * rA,
                .14f + (e.sat - .14f) * rC,
                Mathf.Lerp(.44f, .58f, Mathf.Clamp01((20000f - remainingMs) / 8000f)),
                (float)PrismPulseSafety.PulseAmplitude("resolve", hr, remainingMs),   // taper and final fade
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
        int pSession, pBeatSeq;
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
            if (pNextBeat - now <= 0.3)
            {
                // the pretend beats pass the same flashing-light gate as real ones
                field.ScheduleBeat("PREVIEW-" + pSession, ++pBeatSeq, "ok", 60000.0 / hr, hr, pNextBeat);
                pNextBeat += 60.0 / hr;
            }
        }

        static double Median(List<double> xs)
        {
            var s = new List<double>(xs);
            s.Sort();
            int n = s.Count;
            return n == 0 ? 0 : (n % 2 == 1 ? s[n / 2] : 0.5 * (s[n / 2 - 1] + s[n / 2]));
        }

        static Camera ViewCamera()
        {
            Camera cam = Camera.main;
            if (!cam)
            {
                var cams = FindObjectsByType<Camera>(FindObjectsSortMode.None);
                if (cams.Length > 0) cam = cams[0];
            }
            return cam;
        }

        void MakeMarker()
        {
            Camera cam = ViewCamera();
            if (!cam) return;

            // A disc from Prism/Marker: unlit, opaque and drawn over everything. Without the
            // shader, the sphere and default material this used before.
            marker = GameObject.CreatePrimitive(markerShader ? PrimitiveType.Quad : PrimitiveType.Sphere);
            marker.name = "Prism Disconnected Marker";
            Destroy(marker.GetComponent<Collider>());
            marker.transform.SetParent(cam.transform, false);
            marker.transform.localPosition = new Vector3(0f, -0.18f, 0.8f);
            marker.transform.localScale = Vector3.one * 0.025f;
            var renderer = marker.GetComponent<Renderer>();
            renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            if (markerShader)
            {
                var mat = new Material(markerShader);
                mat.SetColor("_Color", Color.red);
                renderer.material = mat;
            }
            else
            {
                Debug.LogError("[PrismLink] markerShader is not set on Prism Link: the red " +
                               "not-linked dot may not be visible in this build.");
                var mat = renderer.material;
                mat.color = Color.red;
                mat.SetColor("_BaseColor", Color.red);
            }
            marker.SetActive(false);                      // ShowLinkStatus decides, every frame
        }

        void MakeOfflineLabel()
        {
            Camera cam = ViewCamera();
            if (!cam) return;

            offlineLabel = new GameObject("Prism Offline Preview Label");
            offlineLabel.transform.SetParent(cam.transform, false);
            offlineLabel.transform.localPosition = new Vector3(0f, 0.2f, 1.6f);
            var text = offlineLabel.AddComponent<TextMesh>();
            Font font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            if (font)
            {
                text.font = font;
                offlineLabel.GetComponent<MeshRenderer>().material = font.material;
            }
            text.anchor = TextAnchor.MiddleCenter;
            text.alignment = TextAlignment.Center;
            text.characterSize = 0.02f;
            text.fontSize = 64;
            text.color = new Color(1f, 0.25f, 0.2f, 1f);
            text.text = PrismLinkStatus.OfflineLabel;
        }

        void OnDestroy()
        {
            try { cts?.Cancel(); ws?.Abort(); ws?.Dispose(); } catch (Exception) { }
        }
    }
}
