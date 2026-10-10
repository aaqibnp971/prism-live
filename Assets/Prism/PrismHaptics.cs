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
    /// A controller that is off, asleep or absent is skipped silently and looked for again.
    [RequireComponent(typeof(PrismField), typeof(PrismLink))]
    [DefaultExecutionOrder(100)]                  // after PrismField has started this frame's pulse
    public class PrismHaptics : MonoBehaviour
    {
        [Tooltip("How strong each tap is. Kept gentle: never above 0.5 whatever is set here.")]
        [Range(0f, 0.5f)] public float strength = 0.3f;
        [Tooltip("How long each tap lasts, seconds")]
        [Range(0.02f, 0.1f)] public float tapSeconds = 0.06f;

        const float RetrySeconds = 1f;

        PrismField field;
        PrismLink link;
        int lastBeat;
        InputDevice right;
        float lookAgainAt;
        float ovrStopAt = -1f;
        string path = "";
        readonly List<InputDevice> found = new List<InputDevice>();

        void Awake()
        {
            field = GetComponent<PrismField>();
            link = GetComponent<PrismLink>();
        }

        void OnEnable() { if (field) lastBeat = field.BeatCount; }   // nothing from before now

        void Update()
        {
            int beat = field.BeatCount;
            bool started = beat != lastBeat;
            lastBeat = beat;

            float amp = (float)PrismPulseSafety.HapticAmplitude(started, link.IsLinked, field.PulseTaper, strength);
            if (amp > 0f) Tap(amp);

            if (ovrStopAt > 0f && (Time.unscaledTime >= ovrStopAt || !link.IsLinked)) StopOvr();
        }

        void Tap(float amp)
        {
            float seconds = Mathf.Clamp(tapSeconds, 0.02f, 0.1f);
            try
            {
                // Meta's own input layer first: it is what the app as delivered used on this
                // headset. Right Touch controller only.
                if (OVRInput.IsControllerConnected(OVRInput.Controller.RTouch))
                {
                    OVRInput.SetControllerVibration(0.2f, amp, OVRInput.Controller.RTouch);
                    ovrStopAt = Time.unscaledTime + seconds;
                    Note("OVRInput");
                    return;
                }
                if (RightController() && right.SendHapticImpulse(0u, amp, seconds)) { Note("XR input"); return; }
                Note("no right controller");
            }
            catch (System.Exception error)
            {
                // A haptics fault must never reach the session: say so once and carry on.
                Note("unavailable (" + error.GetType().Name + ")");
            }
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
            try { OVRInput.SetControllerVibration(0f, 0f, OVRInput.Controller.RTouch); }
            catch (System.Exception) { }
        }

        /// One log line each time the way taps are delivered changes (adb logcat -s Unity).
        void Note(string how)
        {
            if (how == path) return;
            path = how;
            Debug.Log("[PrismHaptics] right controller: " + how);
        }

        void OnDisable()
        {
            try { if (right.isValid) right.StopHaptics(); } catch (System.Exception) { }
            if (ovrStopAt > 0f) StopOvr();
        }

        void OnApplicationPause(bool paused) { if (paused) OnDisable(); }
    }
}
