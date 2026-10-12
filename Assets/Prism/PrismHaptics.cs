using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR;

namespace Prism
{
    /// The visitor's heartbeat as a short, soft tap in the RIGHT controller only. The armband
    /// is on the left forearm and that hand stays still in the lap, so nothing here ever
    /// drives a left controller.
    ///
    /// It never makes a beat of its own. It taps when PrismField starts a visual pulse, which
    /// happens only for a beat the laptop scheduled, at that beat's own time, and only while
    /// the flashing-light rules allow the pulse (PrismPulseSafety.cs): a real laptop link, a
    /// running session, that session's beats, not frozen. The tap is full to 95 bpm and gone
    /// by 120, like the picture. All of that is decided in PrismPulseSafety.HapticAmplitude,
    /// which is tested; this class only carries the result to a controller.
    ///
    /// One other tap exists: the attendant's controller check, once per run of the app, when
    /// the right controller is first found while the laptop says "idle" (PrismTestTap, also
    /// tested). Never in a session.
    ///
    /// A controller that is off, asleep or absent is skipped and looked for again. A sleeping
    /// controller is woken by pressing any of its buttons; nothing here can wake it.
    ///
    /// Every tap is logged with its result (adb logcat -s Unity). The headset keeps only about
    /// a minute of that, so the counts for the last session are also written to
    /// haptics-last.txt in the app's storage when the session ends: no beat times, no heart rate.
    [RequireComponent(typeof(PrismField), typeof(PrismLink))]
    [DefaultExecutionOrder(100)]                  // after PrismField has started this frame's pulse
    public class PrismHaptics : MonoBehaviour
    {
        public const string RecordFile = "haptics-last.txt";

        [Tooltip("How strong each tap is. Kept gentle: never above 0.5 whatever is set here.")]
        [Range(0f, 0.5f)] public float strength = 0.35f;
        [Tooltip("How long each tap lasts, seconds")]
        [Range(0.02f, 0.1f)] public float tapSeconds = 0.06f;

        const float RetrySeconds = 1f;
        const int MaxEvents = 200;

        PrismField field;
        PrismLink link;
        int lastBeat;
        InputDevice right;
        float lookAgainAt;
        float ovrStopAt = -1f;
        readonly List<InputDevice> found = new List<InputDevice>();
        readonly PrismTestTap check = new PrismTestTap();

        // the record of one session
        readonly SortedDictionary<string, int> counts = new SortedDictionary<string, int>();
        readonly List<string> events = new List<string>();
        string session = "";
        bool inSession, presentAtStart, wasPresent;
        int taps, controllerChanges;

        void Awake()
        {
            field = GetComponent<PrismField>();
            link = GetComponent<PrismLink>();
        }

        void OnEnable()
        {
            if (field) lastBeat = field.BeatCount;     // nothing from before now
            if (link) link.StateReceived += OnState;
        }

        void Update()
        {
            bool present = Present(out string how);
            if (check.Seen(present)) Event("right controller found (" + how + ")");
            else if (!present && wasPresent) Event("right controller lost (off, asleep or out of range)");
            if (present != wasPresent) { wasPresent = present; controllerChanges++; }

            int beat = field.BeatCount;
            bool started = beat != lastBeat;
            lastBeat = beat;

            float amp = (float)PrismPulseSafety.HapticAmplitude(started, link.IsLinked, field.PulseTaper, strength);
            if (amp > 0f)
            {
                string result = Send(amp);
                Count("tap " + result);
                Debug.Log($"[PrismHaptics] tap {++taps} {result}, strength {amp:F2}");
            }
            else if (started)
            {
                string why = PrismPulseSafety.HapticBlock(link.IsLinked, field.PulseTaper, strength);
                Count("no tap: " + why);
                Debug.Log("[PrismHaptics] no tap for this beat: " + why);
            }

            if (ovrStopAt > 0f && (Time.unscaledTime >= ovrStopAt || !link.IsLinked)) StopOvr();
        }

        /// Each state message from the laptop, as it arrives. The controller check can fire
        /// here and nowhere else, and only on a fresh state that says "idle" (PrismTestTap).
        void OnState(LinkMsg m)
        {
            bool running = link.IsLinked && PrismPulseSafety.ActiveSegment(m.segment);
            if (running && (!inSession || m.session != session)) Begin(m.session);
            else if (inSession && link.IsLinked && (m.segment == "idle" || m.segment == "reset")) End();

            switch (check.OnState(link.IsLinked, m.segment, link.StateAgeMs(m)))
            {
                case PrismTestTap.Verdict.Fire:
                    float amp = Mathf.Min(strength, (float)PrismPulseSafety.MaxHapticStrength);
                    Event("controller check tap " + (amp > 0f ? Send(amp) : "not sent: strength set to nothing"));
                    break;
                case PrismTestTap.Verdict.Drop:
                    Event("controller check tap cancelled: a session is running (" + m.segment + ")");
                    break;
            }
        }

        /// Carries one tap to the right controller and says what happened. "sent" means the
        /// headset's software accepted it; only a hand can say it was felt.
        string Send(float amp)
        {
            float seconds = Mathf.Clamp(tapSeconds, 0.02f, 0.1f);
            try
            {
                // Meta's own input layer first: it is what the app as delivered used on this
                // headset. Right Touch controller only. If it refuses, the XR route is tried.
                bool ovr = OVRInput.IsControllerConnected(OVRInput.Controller.RTouch);
                if (ovr)
                {
                    // Stopped after the tap's length whatever the call reports back.
                    ovrStopAt = Time.unscaledTime + seconds;
                    if (OVRPlugin.SetControllerVibration((uint)OVRPlugin.Controller.RTouch, 0.2f, amp)) return "sent (OVR)";
                }
                if (RightController() && right.SendHapticImpulse(0u, amp, seconds)) return "sent (XR input)";
                return ovr || right.isValid ? "not sent: the controller refused it" : "not sent: no right controller";
            }
            catch (System.Exception error)
            {
                // A haptics fault must never reach the session: say so and carry on.
                return "not sent: " + error.GetType().Name;
            }
        }

        /// Is the right controller there now, and by which route it was seen.
        bool Present(out string how)
        {
            how = "";
            try
            {
                if (OVRInput.IsControllerConnected(OVRInput.Controller.RTouch)) { how = "OVR"; return true; }
                if (RightController()) { how = "XR input"; return true; }
            }
            catch (System.Exception) { }
            return false;
        }

        /// The right-hand controller, if it is awake and can take an impulse. A missing one is
        /// looked for again at most once a second, never every frame.
        bool RightController()
        {
            if (right.isValid) return true;
            if (Time.unscaledTime < lookAgainAt) return false;
            lookAgainAt = Time.unscaledTime + RetrySeconds;
            found.Clear();
            InputDevices.GetDevicesWithCharacteristics(
                InputDeviceCharacteristics.Right | InputDeviceCharacteristics.Controller, found);
            foreach (InputDevice device in found)
                if (device.isValid && device.TryGetHapticCapabilities(out HapticCapabilities can) && can.supportsImpulse)
                {
                    right = device;
                    return true;
                }
            return false;
        }

        void StopOvr()
        {
            ovrStopAt = -1f;
            try { OVRPlugin.SetControllerVibration((uint)OVRPlugin.Controller.RTouch, 0f, 0f); }
            catch (System.Exception) { }
        }

        void Begin(string newSession)
        {
            session = newSession;
            inSession = true;
            counts.Clear();
            events.Clear();
            taps = controllerChanges = 0;
            presentAtStart = wasPresent;
            Event("session " + session + " started, right controller " + (wasPresent ? "connected" : "NOT connected"));
        }

        void End()
        {
            inSession = false;
            Event("session " + session + " ended");
            WriteRecord();
        }

        void Count(string what)
        {
            counts.TryGetValue(what, out int n);
            counts[what] = n + 1;
        }

        void Event(string what)
        {
            Debug.Log("[PrismHaptics] " + what);
            if (events.Count < MaxEvents) events.Add(System.DateTime.Now.ToString("HH:mm:ss") + " " + what);
        }

        /// The last session's counts, for adb. Written when a session ends, or when the headset
        /// comes off during one; never while a session is being shown.
        void WriteRecord()
        {
            try
            {
                var text = new System.Text.StringBuilder();
                text.AppendLine("PrismHaptics record, written " + System.DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
                text.AppendLine("session " + session + (inSession ? " (still running when written)" : ""));
                text.AppendLine("right controller at session start: " + (presentAtStart ? "connected" : "NOT connected") +
                                ", changes since: " + controllerChanges);
                if (counts.Count == 0) text.AppendLine("no pulse started, so no tap was due");
                foreach (KeyValuePair<string, int> count in counts) text.AppendLine(count.Value + " x " + count.Key);
                PrismBeatGate gate = field.Gate;
                text.AppendLine("beats refused before any pulse: late " + gate.Late + ", rejected " + gate.Rejected +
                                ", duplicate " + gate.Duplicate + ", too fast " + gate.RateLimited +
                                ", queue full " + gate.Overflow + ", another session's " + gate.WrongSession);
                text.AppendLine("events:");
                foreach (string line in events) text.AppendLine("  " + line);
                System.IO.File.WriteAllText(
                    System.IO.Path.Combine(Application.persistentDataPath, RecordFile), text.ToString());
            }
            catch (System.Exception error)
            {
                Debug.Log("[PrismHaptics] record not written: " + error.GetType().Name);
            }
        }

        void Quiet()
        {
            try { if (right.isValid) right.StopHaptics(); } catch (System.Exception) { }
            if (ovrStopAt > 0f) StopOvr();
        }

        void OnDisable()
        {
            if (link) link.StateReceived -= OnState;
            Quiet();
        }

        void OnApplicationPause(bool paused)
        {
            if (!paused) return;
            Quiet();
            if (inSession) WriteRecord();              // headset taken off mid-session: keep what there is
        }
    }
}
