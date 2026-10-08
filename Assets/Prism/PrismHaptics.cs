using UnityEngine;

namespace Prism
{
    /// Heartbeat haptics: a short, soft tap on the Quest controllers on every beat the
    /// Prism bridge sends (the same beats that drive the engine's heartbeat sound and the
    /// visual pulse). Strength follows the field's pulse size, so it fades out with it.
    [RequireComponent(typeof(PrismField))]
    public class PrismHaptics : MonoBehaviour
    {
        [Range(0f, 1f)] public float strength = 0.35f;
        [Tooltip("How long each tap lasts, seconds")]
        public float tapSeconds = 0.06f;

        PrismField field;
        int lastBeat;
        float stopAt = -1f;

        void Awake() => field = GetComponent<PrismField>();

        void Update()
        {
            if (field.BeatCount != lastBeat)
            {
                lastBeat = field.BeatCount;
                float amp = strength * Mathf.Clamp01(field.Current.pulse / 0.16f);
                if (amp > 0.01f)
                {
                    OVRInput.SetControllerVibration(0.2f, amp, OVRInput.Controller.LTouch);
                    OVRInput.SetControllerVibration(0.2f, amp, OVRInput.Controller.RTouch);
                    stopAt = Time.unscaledTime + tapSeconds;
                }
            }

            if (stopAt > 0f && Time.unscaledTime >= stopAt)
            {
                OVRInput.SetControllerVibration(0f, 0f, OVRInput.Controller.LTouch);
                OVRInput.SetControllerVibration(0f, 0f, OVRInput.Controller.RTouch);
                stopAt = -1f;
            }
        }

        void OnDisable()
        {
            OVRInput.SetControllerVibration(0f, 0f, OVRInput.Controller.LTouch);
            OVRInput.SetControllerVibration(0f, 0f, OVRInput.Controller.RTouch);
        }
    }
}
