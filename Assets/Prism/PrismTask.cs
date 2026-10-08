using System.Collections.Generic;
using System.Globalization;
using UnityEngine;

namespace Prism
{
    /// The load-segment attention task, played with HEAD GAZE (Quest 3S has no eye tracking).
    /// A straight port of the team's web/task/task.js: same ramp, same miss penalty, same
    /// split/lock/miss/abandon rules, same task_event messages back to the laptop.
    /// The laptop decides when it runs (segment == "load"); this never starts on its own.
    /// Also shows the only two lines of text in the experience (vr-handoff §11).
    [RequireComponent(typeof(PrismLink))]
    public class PrismTask : MonoBehaviour
    {
        [Tooltip("On: one glowing dot to catch (no decoys). Off: the team's split task with decoys.")]
        public bool simpleDot = true;
        public Shader glowShader;

        [Header("Ramp (provisional, tune without recompiling)")]
        public float dwellStartMs = 900f, dwellEndMs = 420f;
        public float splitStartMs = 5500f, splitEndMs = 2200f;
        public float distractorsStart = 1f, distractorsEnd = 3f;
        public float speedStartDeg = 6f, speedEndDeg = 19f;
        public float missPenaltyMs = 400f, minSplitMs = 1800f;

        [Header("Layout")]
        public float distanceM = 2.5f;
        public float motionLimitDeg = 20f;
        [Tooltip("How close (degrees) the head ray must be to count as 'on' a target")]
        public float hitRadiusDeg = 3.2f;
        public float targetSizeM = 0.16f;

        const float LoadDurationMs = 75000f;
        // The headset cannot know in advance when the laptop will change to load (baseline is
        // held past its nominal 45 s), so the lead-in runs from the laptop's actual change:
        // these first seconds of load count down, and the task begins when they end.
        const float LeadInMs = 3000f;

        PrismLink link;
        Transform head;

        // task state (mirrors task.js)
        bool active, complete, paused;
        string session;
        int misses;
        float angleDeg = -8f, direction = 1f;
        double anchorClientMs, lastTickClientMs;
        float anchorElapsedMs, nextSplitElapsedMs;
        Round round;
        string hoveredId;
        double hoverStartMs = -1;
        float dwellProgress;

        class Decoy { public string id; public float angleDeg, yOffset; public GameObject go; }
        class Round { public float splitElapsedMs; public bool resolved; public List<Decoy> decoys = new List<Decoy>(); }

        // visuals
        Vector3 origin; Quaternion baseYaw;
        GameObject mover, reticle, reticleFill;
        TextMesh line;
        float lineUntil = -1f;
        string lastSegment = "";
        bool shownResolveLine;
        float loadTextUntil = -1f, flashUntil = -1f;

#if UNITY_EDITOR
        void Reset() => OnValidate();
        void OnValidate() { if (!glowShader) glowShader = Shader.Find("Prism/Glow"); }
#endif

        void Start()
        {
            link = GetComponent<PrismLink>();
            link.StateReceived += OnState;
            Camera cam = Camera.main;
            if (!cam) { var cams = FindObjectsByType<Camera>(FindObjectsSortMode.None); if (cams.Length > 0) cam = cams[0]; }
            head = cam ? cam.transform : transform;

            mover = MakeGlow("Prism Task Target", targetSizeM, new Color(0.85f, 0.93f, 1f));
            mover.SetActive(false);

            reticle = MakeGlow("Prism Reticle", 0.02f, new Color(1f, 1f, 1f));
            reticle.transform.SetParent(head, false);
            reticle.transform.localPosition = new Vector3(0f, 0f, 1.5f);
            reticleFill = MakeGlow("Prism Reticle Fill", 0.03f, new Color(0.7f, 0.85f, 1f));
            reticleFill.transform.SetParent(head, false);
            reticleFill.transform.localPosition = new Vector3(0f, 0f, 1.52f);
            reticle.SetActive(false); reticleFill.SetActive(false);

            var textGo = new GameObject("Prism Text");
            textGo.transform.SetParent(head, false);
            textGo.transform.localPosition = new Vector3(0f, -0.05f, 1.6f);
            line = textGo.AddComponent<TextMesh>();
            Font font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
            if (font)
            {
                line.font = font;
                textGo.GetComponent<MeshRenderer>().material = font.material;
            }
            line.anchor = TextAnchor.MiddleCenter;
            line.alignment = TextAlignment.Center;
            line.characterSize = 0.02f;
            line.fontSize = 48;
            line.color = new Color(1f, 1f, 1f, 0.9f);
            line.text = "";
        }

        void OnDestroy() { if (link) link.StateReceived -= OnState; }

        // ---- laptop state (StateGate in task.js) ----
        void OnState(LinkMsg state)
        {
            double now = PrismLink.ClientNowMs;
            float remaining = (float)(state.segment_nominal_ms - state.segment_elapsed_ms);

            if (state.segment != lastSegment)
            {
                // shown for 3 s once the lead-in countdown has finished
                if (state.segment == "load")
                    loadTextUntil = Time.unscaledTime + Mathf.Max(0f, LeadInMs - (float)state.segment_elapsed_ms) / 1000f + 3f;
                shownResolveLine = false;
                lastSegment = state.segment;
            }
            // Text 2: at T-20 s in resolve
            if (state.segment == "resolve" && remaining <= 20000f && !shownResolveLine)
            {
                shownResolveLine = true;
                ShowLine("This is your heart rate, from the moment you sat down.", 8f);
            }

            if (state.segment != "load")
            {
                if (active || paused) StopTask();
                return;
            }
            if (complete && session == state.session) return;
            if (!active || session != state.session) StartTask(state.session, (float)state.segment_elapsed_ms, now);
            else SyncTask((float)state.segment_elapsed_ms, now);
        }

        void ShowLine(string text, float seconds)
        {
            line.text = text;
            lineUntil = Time.unscaledTime + Mathf.Max(0.5f, seconds);
        }

        void UpdateText()
        {
            float t = Time.unscaledTime;
            if (lineUntil > 0f && t >= lineUntil) lineUntil = -1f;

            string text = "";
            float size = 0.02f;
            if (lineUntil > 0f) text = line.text;
            else if (InLeadIn(PrismLink.ClientNowMs))
            {
                // 3, 2, 1 from the laptop's change to load; it ends as the task begins
                text = Mathf.Clamp(Mathf.CeilToInt((LeadInMs - ElapsedAt(PrismLink.ClientNowMs)) / 1000f), 1, 3).ToString();
                size = 0.06f;
            }
            else if (lastSegment == "load" && t < loadTextUntil) text = "Follow the light.";

            if (lineUntil < 0f) line.text = text;
            line.characterSize = size;
        }

        void StartTask(string sess, float elapsedMs, double now)
        {
            float e = Mathf.Clamp(elapsedMs, 0f, LoadDurationMs);
            active = e < LoadDurationMs; complete = !active; paused = false;
            session = sess; misses = 0; ClearRound();
            angleDeg = Mathf.Clamp(-8f, -motionLimitDeg, motionLimitDeg); direction = 1f;
            anchorClientMs = now; anchorElapsedMs = e; lastTickClientMs = now;
            // the first split comes one interval after the task begins, not after the countdown starts
            float begins = Mathf.Max(e, LeadInMs);
            nextSplitElapsedMs = Mathf.Min(LoadDurationMs, begins + Ramp(begins).split);
            ClearHover();

            // the task is laid out around where the person is facing now
            origin = head.position;
            Vector3 fwd = head.forward; fwd.y = 0f;
            baseYaw = Quaternion.LookRotation(fwd.sqrMagnitude > 1e-4f ? fwd.normalized : Vector3.forward);
        }

        void SyncTask(float elapsedMs, double now)
        {
            if (!active || complete) return;
            anchorElapsedMs = Mathf.Clamp(elapsedMs, 0f, LoadDurationMs);
            anchorClientMs = now; lastTickClientMs = now; paused = false;
        }

        void StopTask()
        {
            active = false; complete = false; paused = false; session = null;
            ClearRound(); ClearHover();
        }

        /// True for the first seconds of load, before the task's first object appears.
        bool InLeadIn(double now) => active && ElapsedAt(now) < LeadInMs;

        float ElapsedAt(double now) =>
            paused ? anchorElapsedMs
                   : Mathf.Clamp(anchorElapsedMs + (float)System.Math.Max(0, now - anchorClientMs), 0f, LoadDurationMs);

        struct RampValues { public float difficulty, dwell, split, speed; public int distractors; }

        RampValues Ramp(float elapsedMs)
        {
            float d = Mathf.Clamp01(elapsedMs / LoadDurationMs);
            float nominal = Mathf.Lerp(splitStartMs, splitEndMs, d);
            return new RampValues
            {
                difficulty = d,
                dwell = Mathf.Lerp(dwellStartMs, dwellEndMs, d),
                split = Mathf.Max(minSplitMs, nominal - misses * missPenaltyMs),
                distractors = simpleDot ? 0 : Mathf.RoundToInt(Mathf.Lerp(distractorsStart, distractorsEnd, d)),
                speed = Mathf.Lerp(speedStartDeg, speedEndDeg, d),
            };
        }

        // ---- per frame ----
        void Update()
        {
            UpdateText();

            double now = PrismLink.ClientNowMs;

            // connection lost: freeze the task where it is (task.js freeze)
            if (active && !paused && !link.IsOpen)
            {
                anchorElapsedMs = ElapsedAt(now); anchorClientMs = now; lastTickClientMs = now;
                paused = true; ClearHover();
            }

            if (active && !paused) Tick(now);
            Draw();
        }

        void Tick(double now)
        {
            float elapsed = ElapsedAt(now);
            float deltaMs = (float)System.Math.Max(0, now - lastTickClientMs);
            lastTickClientMs = now;
            float before = Ramp(Mathf.Max(0f, elapsed - deltaMs)).speed, after = Ramp(elapsed).speed;
            AdvanceMotion(deltaMs / 1000f, (before + after) / 2f);

            if (elapsed >= LoadDurationMs) { active = false; complete = true; ClearRound(); ClearHover(); return; }
            if (elapsed < LeadInMs) return;                  // counting down: nothing to catch yet

            Interact(now, elapsed, Hovered());
            if (elapsed >= nextSplitElapsedMs)
            {
                if (round != null && !round.resolved) ResolveMiss(now, elapsed);
                Split(now, elapsed);
            }
        }

        void AdvanceMotion(float dt, float speed)
        {
            float limit = motionLimitDeg;
            if (!(dt > 0f) || !(limit > 0f)) return;
            float position = angleDeg + limit, period = 4f * limit;
            float phase = direction > 0 ? position : period - position;
            phase = (phase + speed * dt) % period;
            if (phase <= 2f * limit) { angleDeg = phase - limit; direction = 1f; }
            else { angleDeg = period - phase - limit; direction = -1f; }
        }

        void Interact(double now, float elapsed, string hovered)
        {
            if (round == null || round.resolved) { ClearHover(); return; }
            bool valid = hovered == "moving" || round.decoys.Exists(d => d.id == hovered);
            string next = valid ? hovered : null;
            if (next != hoveredId)
            {
                if (hoveredId != null && hoverStartMs >= 0)
                {
                    double held = now - hoverStartMs;
                    if (held > 0 && held < Ramp(elapsed).dwell) Emit("abandon", now, elapsed);
                }
                hoveredId = next; hoverStartMs = next == null ? -1 : now; dwellProgress = 0f;
            }
            if (hoveredId == null || hoverStartMs < 0) return;

            dwellProgress = Mathf.Clamp01((float)(now - hoverStartMs) / Ramp(elapsed).dwell);
            if (dwellProgress < 1f) return;
            if (hoveredId == "moving")
            {
                Emit("lock", now, elapsed);
                round.resolved = true; HideDecoys(); ClearHover();
                flashUntil = Time.unscaledTime + 0.3f;             // caught it: a soft bloom
            }
            else ResolveMiss(now, elapsed);
        }

        void ResolveMiss(double now, float elapsed)
        {
            Emit("miss", now, elapsed);
            misses += 1;
            if (round != null)
            {
                round.resolved = true; HideDecoys();
                nextSplitElapsedMs = Mathf.Min(nextSplitElapsedMs, round.splitElapsedMs + Ramp(elapsed).split);
            }
            ClearHover();
        }

        void Split(double now, float elapsed)
        {
            var r = Ramp(elapsed);
            ClearRound();
            round = new Round { splitElapsedMs = elapsed };
            for (int i = 0; i < r.distractors; i++)
            {
                float side = i % 2 == 0 ? -1f : 1f;
                int rank = i / 2;
                float offset = side * (2.6f + rank * 1.5f + Random.value * 0.6f);
                var d = new Decoy
                {
                    id = "decoy-" + i,
                    angleDeg = Mathf.Clamp(angleDeg + offset, -motionLimitDeg, motionLimitDeg),
                    yOffset = (i - (r.distractors - 1) / 2f) * 0.14f,
                };
                d.go = MakeGlow("Prism Decoy", targetSizeM * 0.72f, new Color(0.85f, 0.93f, 1f));
                round.decoys.Add(d);
            }
            ClearHover();
            Emit("split", now, elapsed);
            nextSplitElapsedMs = Mathf.Min(LoadDurationMs, elapsed + r.split);
        }

        void Emit(string evt, double now, float elapsed)
        {
            var r = Ramp(elapsed);
            var ci = CultureInfo.InvariantCulture;
            link.Send("{\"type\":\"task_event\",\"v\":1,\"session\":\"" + session +
                      "\",\"t_client\":" + now.ToString("F3", ci) +
                      ",\"event\":\"" + evt +
                      "\",\"dwell_ms\":" + r.dwell.ToString("F3", ci) +
                      ",\"split_interval_ms\":" + r.split.ToString("F3", ci) +
                      ",\"difficulty\":" + r.difficulty.ToString("F3", ci) + "}");
        }

        // ---- head gaze ----
        Vector3 WorldPoint(float az, float yOffset)
        {
            float elev = -yOffset * 20f;   // screen-height fraction → degrees
            return origin + baseYaw * (Quaternion.Euler(elev, az, 0f) * Vector3.forward) * distanceM;
        }

        string Hovered()
        {
            string best = null; float bestAngle = hitRadiusDeg;
            Check("moving", WorldPoint(angleDeg, 0f), ref best, ref bestAngle);
            if (round != null && !round.resolved)
                foreach (var d in round.decoys) Check(d.id, WorldPoint(d.angleDeg, d.yOffset), ref best, ref bestAngle);
            return best;
        }

        void Check(string id, Vector3 p, ref string best, ref float bestAngle)
        {
            float a = Vector3.Angle(head.forward, p - head.position);
            if (a < bestAngle) { bestAngle = a; best = id; }
        }

        void Draw()
        {
            bool show = active && !paused && !InLeadIn(PrismLink.ClientNowMs);
            mover.SetActive(show);
            reticle.SetActive(show);
            reticleFill.SetActive(show && dwellProgress > 0f);
            if (!show) return;

            bool splitNow = round != null && !round.resolved && round.decoys.Count > 0;
            float flash = flashUntil > Time.unscaledTime ? 1.6f : 1f;
            mover.transform.position = WorldPoint(angleDeg, 0f);
            mover.transform.localScale = Vector3.one * (splitNow ? targetSizeM * 0.72f : targetSizeM) * flash;
            Face(mover);
            if (splitNow)
                foreach (var d in round.decoys) { d.go.transform.position = WorldPoint(d.angleDeg, d.yOffset); Face(d.go); }

            reticleFill.transform.localScale = Vector3.one * (0.02f + 0.06f * dwellProgress);
        }

        // ---- helpers ----
        void ClearHover() { hoveredId = null; hoverStartMs = -1; dwellProgress = 0f; }
        void HideDecoys() { if (round != null) foreach (var d in round.decoys) if (d.go) Destroy(d.go); }
        void ClearRound() { HideDecoys(); round = null; }

        void Face(GameObject go) =>
            go.transform.rotation = Quaternion.LookRotation(go.transform.position - head.position);

        GameObject MakeGlow(string name, float size, Color c)
        {
            var go = GameObject.CreatePrimitive(glowShader ? PrimitiveType.Quad : PrimitiveType.Sphere);
            go.name = name;
            Destroy(go.GetComponent<Collider>());
            go.transform.localScale = Vector3.one * size;
            var r = go.GetComponent<Renderer>();
            r.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            if (glowShader)
            {
                var mat = new Material(glowShader);
                mat.SetColor("_Color", c);
                mat.SetFloat("_Intensity", 1.2f);
                r.material = mat;
            }
            else
            {
                var mat = r.material;
                mat.color = c;
                mat.SetColor("_BaseColor", c);
            }
            return go;
        }
    }
}
